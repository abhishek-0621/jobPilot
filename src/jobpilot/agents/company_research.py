"""COMPANY_RESEARCH: a short, grounded brief of a company's recent products, projects and
initiatives, cached per company for ``FRESH_DAYS`` in ``company_brief``.

Sources are public and polite only (see ``jobpilot.research.fetch``): the job posting's
own text, the user's research URLs for the company, the company homepage and a few likely
news pages on its own domain. The model (route ``company_research``) extracts at most
``MAX_ITEMS`` items from the untrusted page text; code keeps only items grounded in the
page they cite (``jobpilot.research.brief``). Never run by scheduled discovery.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import AppConfig
from jobpilot.contracts.models import AgentOutput
from jobpilot.data.db import utcnow
from jobpilot.data.tables import ApplicationRow, CompanyBriefRow, JobRow, TaskRow
from jobpilot.discovery.normalize import html_to_text, require_http
from jobpilot.llm.prompts import build_messages
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.engine import (
    AgentContext,
    FollowUp,
    OutputRejected,
    TaskInput,
    TaskKind,
    TaskStatus,
    add_task,
)
from jobpilot.outreach.contacts import company_domain
from jobpilot.profile.preferences import (
    CAREERS_URL_KEY,
    RESEARCH_URLS_KEY,
    CompanyBoard,
    load_companies,
    research_urls_of,
)
from jobpilot.research.brief import (
    FRESH_DAYS,
    MAX_ITEMS,
    BriefItem,
    BriefSource,
    brief_hash,
    company_key,
    ground_items,
)
from jobpilot.research.fetch import ResearchFetcher, candidate_urls, fetch_pages
from jobpilot.util.hashing import canonical_json

PROMPT_VERSION = "company_research_v1"
ROUTE = "company_research"
_ACTOR = "orchestrator"
PAGE_CHARS = 4000
JOB_CHARS = 6000
ENTITY_APPLICATION = "application"  # automatic: uses a fresh cached brief
ENTITY_COMPANY = "company"          # user "Refresh": always fetches again
_ACTIVE = (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)


class _ItemLLM(BaseModel):
    kind: str
    name: str
    one_line: str
    date_if_stated: str | None = None
    source_url: str


class _BriefLLM(BaseModel):
    items: list[_ItemLLM] = Field(default_factory=list, max_length=MAX_ITEMS * 2)


class CompanyBrief(AgentOutput):
    company_key: str
    company: str
    skipped: bool = False  # a fresh brief was already cached
    fetched_at: datetime
    sources: list[BriefSource] = []
    items: list[BriefItem] = []
    content_hash: str = "none"


# ---- stored briefs --------------------------------------------------------------------


def load_brief(s: Session, key: str | None) -> CompanyBriefRow | None:
    return None if not key else s.get(CompanyBriefRow, key)


def brief_items(row: CompanyBriefRow | None) -> list[BriefItem]:
    if row is None:
        return []
    try:
        raw = json.loads(row.items_json)
        return [BriefItem.model_validate(r) for r in raw if isinstance(r, dict)]
    except ValueError:
        return []


def brief_sources(row: CompanyBriefRow | None) -> list[BriefSource]:
    if row is None:
        return []
    try:
        return [BriefSource.model_validate(r) for r in json.loads(row.sources_json)
                if isinstance(r, dict)]
    except ValueError:
        return []


def is_fresh(row: CompanyBriefRow | None, now: datetime) -> bool:
    if row is None:
        return False
    fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
    return now - fetched < timedelta(days=FRESH_DAYS)


def research_pending(s: Session, application_id: str | None, key: str | None) -> bool:
    """A COMPANY_RESEARCH task for this application or company is queued or running."""
    conds = []
    if application_id:
        conds.append((TaskRow.entity_type == ENTITY_APPLICATION)
                     & (TaskRow.entity_id == application_id))
    if key:
        conds.append((TaskRow.entity_type == ENTITY_COMPANY) & (TaskRow.entity_id == key))
    if not conds:
        return False
    return s.execute(select(TaskRow.task_id).where(
        TaskRow.kind == TaskKind.COMPANY_RESEARCH.value, TaskRow.status.in_(_ACTIVE),
        or_(*conds))).first() is not None


def queue_company_research(s: Session, application_id: str,
                           now: datetime | None = None) -> str | None:
    """When an application starts: queue COMPANY_RESEARCH unless a fresh brief exists or
    research for it is already queued. No commit."""
    app = s.get(ApplicationRow, application_id)
    job = None if app is None else s.get(JobRow, app.job_id)
    key = None if job is None else company_key(job.company)
    if key is None or is_fresh(load_brief(s, key), now or utcnow()) \
            or research_pending(s, application_id, key):
        return None
    return add_task(s, TaskKind.COMPANY_RESEARCH, ENTITY_APPLICATION, application_id)


def request_refresh(s: Session, key: str) -> str | None:
    """User "Refresh": queue a forced COMPANY_RESEARCH (None if one is already queued).
    No commit."""
    if research_pending(s, None, key):
        return None
    return add_task(s, TaskKind.COMPANY_RESEARCH, ENTITY_COMPANY, key)


# ---- loader -------------------------------------------------------------------------


def _job_target(job: JobRow, boards: list[CompanyBoard]) -> dict[str, Any]:
    key = company_key(job.company)
    board = next((b for b in boards if company_key(b.name) == key), None)
    extra = board.extra if board is not None else {}
    careers = extra.get(CAREERS_URL_KEY)
    return {"company": job.company, "company_key": key,
            "domain": company_domain(job.canonical_url, job.application_url, careers),
            "research_urls": research_urls_of(extra.get(RESEARCH_URLS_KEY)),
            "job_url": require_http(job.canonical_url) or require_http(job.application_url),
            "job_text": job.description_text or ""}


def make_research_target(
    session_factory: sessionmaker[Session], cfg: AppConfig,
    boards: Callable[[], list[CompanyBoard]] | None = None,
) -> Callable[[str, str], dict[str, Any] | None]:
    """What research needs: company, key, own domain, the user's research URLs, the job
    text and URL, and whether a fresh brief is cached. ``company`` entities use the most
    recent job of that company."""
    def companies() -> list[CompanyBoard]:
        if boards is not None:
            return boards()
        try:
            return load_companies(cfg)
        except Exception:  # a broken companies file only loses the extra URLs
            return []

    def research_target(entity_type: str, entity_id: str) -> dict[str, Any] | None:
        with session_factory() as s:
            job: JobRow | None = None
            if entity_type == ENTITY_APPLICATION:
                app = s.get(ApplicationRow, entity_id)
                job = None if app is None else s.get(JobRow, app.job_id)
            elif entity_type == ENTITY_COMPANY:
                for j in s.execute(select(JobRow).order_by(
                        JobRow.job_id.desc())).scalars():
                    if company_key(j.company) == entity_id:
                        job = j
                        break
            if job is None or company_key(job.company) is None:
                return None
            target = _job_target(job, companies())
            target["fresh"] = is_fresh(load_brief(s, target["company_key"]), utcnow())
            return target
    return research_target


# ---- agent --------------------------------------------------------------------------


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts")
            / "company_research_v1.md").read_text("utf-8")


def _text(html: str, cap: int) -> str:
    return html_to_text(html, cap) if "<" in html and ">" in html else html[:cap]


class CompanyResearchAgent:
    name = "company_research"
    prompt_version = PROMPT_VERSION
    output_model = CompanyBrief
    tools = ("research_target", "research_fetcher", "clock")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> CompanyBrief:
        load: Callable[[str, str], dict | None] = ctx.tools["research_target"]
        fetcher: ResearchFetcher = ctx.tools["research_fetcher"]
        clock: Callable[[], datetime] = ctx.tools.get("clock") or utcnow
        target = load(inp.entity_type, inp.entity_id)
        if target is None:
            raise OutputRejected("company_not_found")
        now = clock()
        base = {"task_id": ctx.task_id, "created_at": datetime.now(UTC),
                "company_key": target["company_key"], "company": target["company"],
                "fetched_at": now}
        if inp.entity_type == ENTITY_APPLICATION and target.get("fresh"):
            return CompanyBrief(**base, skipped=True)
        if ctx.gateway is None:
            raise OutputRejected("no_model_gateway")
        pages = await fetch_pages(fetcher, candidate_urls(target["domain"],
                                                          target["research_urls"]))
        texts: dict[str, str] = {p.url: _text(p.html, PAGE_CHARS) for p in pages}
        sources = [BriefSource(url=p.url, title=p.title, fetched_at=now) for p in pages]
        job_url, job_text = target.get("job_url"), _text(target.get("job_text") or "",
                                                          JOB_CHARS)
        if job_url and job_text.strip() and job_url not in texts:
            texts[job_url] = job_text
            sources.insert(0, BriefSource(url=job_url, title="Job posting", fetched_at=now))
        texts = {u: t for u, t in texts.items() if t.strip()}
        sources = [src for src in sources if src.url in texts]
        if not texts:
            return CompanyBrief(**base, sources=[], items=[])
        block = "\n\n".join(f"Source URL: {u}\n{t}" for u, t in texts.items())
        instructions = (
            f"Extract up to {MAX_ITEMS} recent products, projects, research, initiatives, "
            f"the mission or values of {target['company']} from the untrusted pages. "
            f"source_url must be one of: {canonical_json(list(texts))}. Today is "
            f"{now.date().isoformat()}.")
        messages = build_messages(_system_prompt(), instructions, untrusted=block)
        raw, _meta = await ctx.gateway.generate(ROUTE, messages, _BriefLLM, PROMPT_VERSION)
        items = ground_items([i.model_dump() for i in raw.items], texts, now)
        warnings = [] if len(items) == len(raw.items) else [
            f"dropped_ungrounded:{len(raw.items) - len(items)}"]
        return CompanyBrief(**base, warnings=warnings, sources=sources, items=items,
                            content_hash=brief_hash(items))


def company_research_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
    """Single writer for ``company_brief``; a brief whose content changed sends why-company
    answers built on the old one back to the user."""
    if not isinstance(out, CompanyBrief):
        raise OutputRejected("company research output mismatch")
    if task.entity_type == ENTITY_APPLICATION:
        app = s.get(ApplicationRow, task.entity_id)
        job = None if app is None else s.get(JobRow, app.job_id)
        key = None if job is None else company_key(job.company)
    else:
        key = task.entity_id
    if key is None or out.company_key != key or company_key(out.company) != key:
        raise OutputRejected("company_key_mismatch")
    if out.skipped:
        return []
    urls = {src.url for src in out.sources}
    if any(i.source_url not in urls for i in out.items) or len(out.items) > MAX_ITEMS:
        raise OutputRejected("brief_item_source_unknown")
    if out.content_hash != brief_hash(list(out.items)):
        raise OutputRejected("brief_hash_mismatch")
    row = s.get(CompanyBriefRow, key)
    old = None if row is None else row.content_hash
    if row is None:
        row = CompanyBriefRow(company_key=key, company=out.company, content_hash="none")
        s.add(row)
    row.company = out.company
    row.fetched_at = out.fetched_at
    row.sources_json = canonical_json([x.model_dump(mode="json") for x in out.sources])
    row.items_json = canonical_json([x.model_dump(mode="json") for x in out.items])
    row.content_hash = out.content_hash
    s.flush()
    record_event(s, entity_type="company", entity_id=key, actor=_ACTOR,
                 action="company_brief_updated", outcome="ok",
                 details={"items": len(out.items), "sources": len(out.sources),
                          "changed": old != out.content_hash})
    if old is not None and old != out.content_hash:
        from jobpilot.data.mutations import invalidate_brief_answers
        invalidate_brief_answers(s, key, out.content_hash, actor=_ACTOR)
    return []
