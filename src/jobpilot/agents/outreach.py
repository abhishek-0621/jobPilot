"""OUTREACH_RESEARCH: find public recruiting contacts and draft one short, verified note.

JobPilot never sends it: the draft goes to the Outreach review queue and the user sends
it themselves. The model writes 1-3 sentences grounded in verified facts; code writes the
opening (role, company and the recorded application date only), greeting and sign-off,
validates, retries once and otherwise falls back to a deterministic template.
"""

import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents.cover_letter import _LETTER_WORDS, _mask
from jobpilot.config import AppConfig
from jobpilot.contracts.enums import OutreachStatus
from jobpilot.contracts.models import AgentOutput
from jobpilot.data import mutations
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import (
    ApplicationRow,
    CandidateFactRow,
    DocumentArtifactRow,
    JobRow,
    TaskRow,
)
from jobpilot.discovery.normalize import html_to_text
from jobpilot.documents.model import split_skills
from jobpilot.documents.validate import _PLACEHOLDER_RE, validate_rewrite
from jobpilot.llm.prompts import build_messages
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.engine import (
    AgentContext,
    FollowUp,
    Handler,
    OutputRejected,
    TaskInput,
)
from jobpilot.orchestrator.state_machine import transition
from jobpilot.outreach.contacts import (
    MAX_POSTING_CHARS,
    FoundContact,
    company_domain,
    extract_from_posting,
    rank,
)
from jobpilot.outreach.state import (
    applied_for_real,
    company_key,
    existing_draft,
    recorded_applied_on,
)
from jobpilot.profile.preferences import CAREERS_URL_KEY, CompanyBoard, load_companies
from jobpilot.util.hashing import canonical_json
from jobpilot.util.textmatch import has_phrase

PROMPT_VERSION = "outreach_v1"
ROUTE = "outreach_draft"
MAX_WORDS = 120
GREETING = "Hello,"
CLOSING = "Best regards,"
POLITE_CLOSE = "Thank you for your time, and I would be glad to share more if it helps."
_ACTOR = "orchestrator"
NO_PUBLIC_CONTACT = "no_public_contact"
DUPLICATE_EVENT = "outreach_duplicate_company"
_CONTACT_RE = re.compile(r"@|https?://|www\.|\+?\d[\d\s().-]{7,}\d")


class _OutreachLLM(BaseModel):
    sentences: list[str] = Field(min_length=1, max_length=3)
    evidence_fact_ids: list[str] = Field(min_length=1, max_length=2)


class OutreachResearch(AgentOutput):
    application_id: str
    company_key: str
    campaign_id: str
    profile_version: int | None
    contacts: list[FoundContact]
    message_text: str
    evidence_fact_ids: list[str]
    used_fallback: bool
    duplicate: bool = False


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts") / "outreach_v1.md").read_text("utf-8")


def _day(applied_on: str | None) -> str | None:
    if not applied_on:
        return None
    try:
        d = date.fromisoformat(applied_on[:10])
    except ValueError:
        return None
    return f"{d.day} {d.strftime('%B')} {d.year}"


def _clean(text: str) -> bool:
    return bool(text.strip()) and not _PLACEHOLDER_RE.search(text) \
        and not _CONTACT_RE.search(text)


def opening(role: str, company: str, applied_on: str | None) -> str:
    """The first sentence; a date appears only when one was recorded. The job's untrusted
    title/company are replaced by neutral words when they carry placeholders or contact
    details."""
    what = f"the {role.strip()} role" if _clean(role) else "the role"
    where = company.strip() if _clean(company) else "the company"
    day = _day(applied_on)
    if day is None:
        return f"I recently applied for {what} at {where}."
    return f"I applied for {what} at {where} on {day}."


def compose(first: str, sentences: list[str], full_name: str) -> str:
    body = " ".join([first, *(s.strip() for s in sentences)])
    return f"{GREETING}\n\n{body}\n\n{CLOSING}\n{full_name}"


def fallback_message(role: str, company: str, applied_on: str | None,
                     evidence_values: list[str], full_name: str) -> str:
    """Deterministic note from verified values only; shortest evidence first, ≤ MAX_WORDS."""
    first = opening(role, company, applied_on)
    chosen: list[str] = []
    for value in sorted(evidence_values, key=len)[:2]:
        v = value.strip().rstrip(".")
        sentence = f"Relevant background: {v}."
        if not v or _PLACEHOLDER_RE.search(v):
            continue
        if len(compose(first, [*chosen, sentence, POLITE_CLOSE], full_name).split()) \
                <= MAX_WORDS:
            chosen.append(sentence)
    return compose(first, [*chosen, POLITE_CLOSE], full_name)


def violations(raw: _OutreachLLM, verified: dict[str, str], company: str, role: str,
               first: str, full_name: str, banned: list[str], skills: list[str],
               terms: set[str]) -> list[str]:
    ids = list(dict.fromkeys(raw.evidence_fact_ids))
    codes = [f"unverified_evidence:{i}" for i in ids if i not in verified]
    sentences = [s.strip() for s in raw.sentences]
    if any(not s for s in sentences):
        codes.append("empty_sentence")
    text = compose(first, sentences, full_name)
    if len(text.split()) > MAX_WORDS:
        codes.append("too_long")
    if not 2 <= len(sentences) + 1 <= 4:
        codes.append("sentence_count")
    body = " ".join(sentences)
    if _PLACEHOLDER_RE.search(body):
        codes.append("placeholder")
    if _CONTACT_RE.search(body):
        codes.append("contact_details")
    codes += [f"banned:{p}" for p in banned if has_phrase(body, p)]
    # company/role strings are untrusted: masked, never added to the source text
    source = " ".join(verified[i] for i in ids if i in verified)
    allowed = set(skills) | _LETTER_WORDS
    for n, s in enumerate(sentences):
        codes += [f"s{n + 1}:{c}" for c in validate_rewrite(source, _mask(s, company, role),
                                                            allowed, terms)
                  if c != "length"]
    return list(dict.fromkeys(codes))


CareersInbox = Callable[[str | None, str | None], Awaitable[list[FoundContact]]]


class OutreachAgent:
    name = "outreach"
    prompt_version = PROMPT_VERSION
    output_model = OutreachResearch
    tools = ("outreach_context", "careers_inbox", "profile_loader", "profile_file",
             "tech_terms", "cover_letter_banned")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> OutreachResearch:
        load: Callable[[str], dict[str, Any] | None] = ctx.tools["outreach_context"]
        careers: CareersInbox = ctx.tools["careers_inbox"]
        app = load(inp.entity_id)
        if app is None:
            raise OutputRejected("application_not_found")
        if not app["applied"]:
            raise OutputRejected("not_applied")
        common = {"task_id": ctx.task_id, "created_at": datetime.now(UTC),
                  "application_id": inp.entity_id, "company_key": app["company_key"],
                  "campaign_id": app["campaign_id"]}
        if app["duplicate"]:
            return OutreachResearch(**common, profile_version=None, contacts=[],
                                    message_text="", evidence_fact_ids=[],
                                    used_fallback=False, duplicate=True)
        company, role = app.get("company") or "", app.get("title") or ""
        if not company or not role:
            raise OutputRejected("job_missing_company_or_title")
        posting = html_to_text(app.get("description_text") or "", MAX_POSTING_CHARS) \
            if "<" in (app.get("description_text") or "") \
            else (app.get("description_text") or "")[:MAX_POSTING_CHARS]
        domain = company_domain(app.get("canonical_url"), app.get("application_url"),
                                app.get("careers_url"))
        found = extract_from_posting(posting, domain, company=company,
                                     source_url=app.get("canonical_url") or "")
        found += await careers(app.get("careers_url"), domain)
        contacts = rank(found)

        version, facts, _years = ctx.tools["profile_loader"]()
        evidence = [f for f in facts if f["category"] != "skill"]
        verified = {f["fact_id"]: f["value"] for f in evidence}
        skills = [s for f in facts if f["category"] == "skill" for s in split_skills(f["value"])]
        preferred = [i for i in app.get("resume_fact_ids") or [] if i in verified]
        full_name = ctx.tools["profile_file"]().identity.full_name
        first = opening(role, company, app.get("applied_on"))
        text, ids, used_fallback = await self._draft(
            ctx, posting, company, role, first, full_name, evidence, verified, skills,
            preferred)
        if used_fallback:
            pick = preferred or list(verified)
            ids = sorted(pick, key=lambda i: len(verified[i]))[:2]
            text = fallback_message(role, company, app.get("applied_on"),
                                    [verified[i] for i in ids], full_name)
            ids = [i for i in ids if verified[i].strip().rstrip(".") in text]
        return OutreachResearch(**common, profile_version=version, contacts=contacts,
                                message_text=text, evidence_fact_ids=ids,
                                used_fallback=used_fallback)

    async def _draft(self, ctx: AgentContext, posting: str, company: str, role: str,
                     first: str, full_name: str, evidence: list[dict], verified: dict[str, str],
                     skills: list[str], preferred: list[str]) -> tuple[str, list[str], bool]:
        if ctx.gateway is None or not verified:
            return "", [], True
        terms, banned = ctx.tools["tech_terms"](), ctx.tools["cover_letter_banned"]()
        base = (
            "Write the short note for the role and company given in the untrusted job block.\n"
            f"Opening sentence (added for you): {first}\n"
            f"Candidate facts (verified): {canonical_json(evidence)}\n"
            f"Verified skills: {canonical_json(skills)}\n"
            f"Preferred evidence fact_ids: {canonical_json(preferred)}")
        job_block = f"Title: {role}\nCompany: {company}\nPosting:\n{posting}"
        instructions = base
        for attempt in (1, 2):
            raw, _meta = await ctx.gateway.generate(
                ROUTE, build_messages(_system_prompt(), instructions, untrusted=job_block),
                _OutreachLLM, PROMPT_VERSION)
            codes = violations(raw, verified, company, role, first, full_name, banned,
                               skills, terms)
            if not codes:
                sentences = [s.strip() for s in raw.sentences]
                return (compose(first, sentences, full_name),
                        list(dict.fromkeys(raw.evidence_fact_ids)), False)
            if attempt == 1:
                instructions = (base + "\nYour previous attempt was rejected for: "
                                + ", ".join(codes) + ". Fix every one of these.")
        return "", [], True


def careers_url_for(boards: list[CompanyBoard], company: str) -> str | None:
    key = company_key(company)
    for b in boards:
        if company_key(b.name) == key and b.extra.get(CAREERS_URL_KEY):
            return b.extra[CAREERS_URL_KEY]
    return None


def make_outreach_loader(
    session_factory: sessionmaker[Session], cfg: AppConfig,
    boards: Callable[[], list[CompanyBoard]] | None = None,
) -> Callable[[str], dict[str, Any] | None]:
    """What research needs about one application: job text and URLs, whether it was
    really applied (and the recorded date), the careers URL and the dedupe state."""
    def companies() -> list[CompanyBoard]:
        if boards is not None:
            return boards()
        try:
            return load_companies(cfg)
        except Exception:  # a broken companies file only loses the careers lookup
            return []

    def outreach_context(application_id: str) -> dict[str, Any] | None:
        with session_factory() as s:
            app = s.get(ApplicationRow, application_id)
            job = None if app is None else s.get(JobRow, app.job_id)
            if app is None or job is None:
                return None
            key = company_key(job.company)
            resume_ids: list[str] = []
            if app.resume_artifact_id is not None:
                art = s.get(DocumentArtifactRow, app.resume_artifact_id)
                try:
                    resume_ids = list(json.loads(art.source_fact_ids_json)) if art else []
                except ValueError:
                    resume_ids = []
            return {
                "application_id": application_id, "status": app.status,
                "applied": applied_for_real(s, app),
                "applied_on": recorded_applied_on(s, app),
                "company": job.company, "title": job.title,
                "canonical_url": job.canonical_url, "application_url": job.application_url,
                "description_text": job.description_text,
                "careers_url": careers_url_for(companies(), job.company),
                "company_key": key, "campaign_id": cfg.outreach.campaign,
                "duplicate": existing_draft(s, key, cfg.outreach.campaign) is not None,
                "resume_fact_ids": [str(i) for i in resume_ids],
            }
    return outreach_context


def make_outreach_handler(cfg: AppConfig) -> Handler:
    """Persists contacts and the one draft per company/campaign, then moves it to review
    (or SKIPPED when no public contact was found). Nothing is ever sent."""
    def outreach_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
        if not isinstance(out, OutreachResearch) or out.application_id != task.entity_id:
            raise OutputRejected("outreach output does not match task application")
        app = get_application(s, out.application_id)
        job = s.get(JobRow, app.job_id)
        if job is None or not applied_for_real(s, app):
            raise OutputRejected("not_applied")
        key, campaign = company_key(job.company), cfg.outreach.campaign
        if out.company_key != key or out.campaign_id != campaign:
            raise OutputRejected("outreach_company_or_campaign_mismatch")
        prior = existing_draft(s, key, campaign)
        if out.duplicate or prior is not None:
            record_event(s, entity_type="application", entity_id=app.application_id,
                         actor=_ACTOR, action=DUPLICATE_EVENT, outcome="skipped",
                         details={"application_id": app.application_id,
                                  "outreach_id": prior.outreach_id if prior else None})
            return []
        if not out.message_text.strip():
            raise OutputRejected("empty_message")
        if out.evidence_fact_ids:
            verified = {f.fact_id for f in s.query(CandidateFactRow).filter(
                CandidateFactRow.profile_version == out.profile_version,
                CandidateFactRow.verified_by_user.is_(True))}
            if any(i not in verified for i in out.evidence_fact_ids):
                raise OutputRejected("unverified_fact")
        try:
            with s.begin_nested():
                draft = mutations.create_outreach_draft(
                    s, application_id=app.application_id, company=job.company,
                    company_key=key, campaign_id=campaign, contacts=out.contacts,
                    message_text=out.message_text, actor=_ACTOR)
        except IntegrityError:  # (company_key, campaign_id) taken: one draft per company
            record_event(s, entity_type="application", entity_id=app.application_id,
                         actor=_ACTOR, action=DUPLICATE_EVENT, outcome="skipped",
                         details={"application_id": app.application_id,
                                  "outreach_id": None})
            return []
        transition(s, draft, OutreachStatus.CONTACT_RESEARCH, actor=_ACTOR,
                   reason="research_started")
        if not out.contacts:
            draft.skip_reason = NO_PUBLIC_CONTACT
            transition(s, draft, OutreachStatus.SKIPPED, actor=_ACTOR,
                       reason=NO_PUBLIC_CONTACT)
        else:
            transition(s, draft, OutreachStatus.DRAFT_READY, actor=_ACTOR,
                       reason="draft_ready")
            transition(s, draft, OutreachStatus.NEEDS_REVIEW, actor=_ACTOR,
                       reason="needs_review")
        record_event(s, entity_type="outreach", entity_id=draft.outreach_id, actor=_ACTOR,
                     action="outreach_drafted", outcome="ok",
                     details={"application_id": app.application_id,
                              "contacts_n": len(out.contacts),
                              "used_fallback": out.used_fallback,
                              "evidence_fact_ids": out.evidence_fact_ids})
        return []
    return outreach_handler
