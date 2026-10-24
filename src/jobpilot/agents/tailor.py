"""TAILOR: evidence-grounded resume tailoring. The model picks, orders and rewords
verified facts; code validates every claim, renders one page and records versions."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import ApplicationStatus
from jobpilot.contracts.models import AgentOutput
from jobpilot.data import mutations
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import CandidateFactRow, TaskRow
from jobpilot.documents import store
from jobpilot.documents.diff import DiffRow, bullet_diff
from jobpilot.documents.model import Bullet, ResumeDoc, base_resume
from jobpilot.documents.render import fit_one_page, render_resume_docx
from jobpilot.documents.validate import validate_rewrite
from jobpilot.llm.prompts import build_messages
from jobpilot.orchestrator.engine import (
    AgentContext,
    FollowUp,
    Handler,
    OutputRejected,
    TaskInput,
    TaskKind,
)
from jobpilot.orchestrator.state_machine import transition
from jobpilot.profile.schema import ProfileFile
from jobpilot.util.hashing import canonical_json

PROMPT_VERSION = "tailor_v1"
_ROUTE = "resume_tailoring"
_ACTOR = "orchestrator"
_SKILL_PREFIX = "fact_skill_"
TAILORABLE = (ApplicationStatus.NOT_STARTED.value, ApplicationStatus.DOCUMENTS_PENDING.value)
_VERSION_RE = re.compile(r"^resume_v(\d+)\.pdf$")


class BulletRewrite(BaseModel):
    fact_id: str
    text: str


class _TailorLLM(BaseModel):
    """What the model is asked for: selection, ordering and wording only."""

    summary: str = Field(max_length=450)
    selected_fact_ids: list[str] = Field(max_length=22)
    bullet_rewrites: list[BulletRewrite] = Field(max_length=22)
    skills_order: list[str] = Field(max_length=30)
    priority: list[str] = Field(max_length=40)  # fact_ids most -> least important


class TailoringPlan(AgentOutput):
    application_id: str
    profile_version: int
    summary: str
    selected_fact_ids: list[str]
    bullet_rewrites: list[BulletRewrite]
    skills_order: list[str]
    priority: list[str]
    rewrites_rejected: list[str]
    unsupported_claims: list[str]
    pdf_path: str
    docx_path: str
    diff: list[DiffRow]
    dropped_fact_ids: list[str]
    used_fact_ids: list[str]


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts") / "tailor_v1.md").read_text("utf-8")


def _fact_rows(version: int, facts: list[dict]) -> list[CandidateFactRow]:
    """Transient rows for ``base_resume``; a skill fact's field is its group name."""
    return [CandidateFactRow(
        fact_id=f["fact_id"], profile_version=version, category=f["category"],
        field=f["fact_id"].removeprefix(_SKILL_PREFIX) if f["category"] == "skill"
        else f["category"], value=f["value"], verified_by_user=True) for f in facts]


def _verified_skills(facts: list[dict]) -> list[str]:
    return [item.strip() for f in facts if f["category"] == "skill"
            for item in f["value"].split(",") if item.strip()]


def fallback_summary(headline: str, years: float, required: list[str],
                     skills: list[str]) -> str:
    """``"{headline}. {years:.0f}+ years; {top 3 required skills held}."``"""
    held = {s.casefold(): s for s in skills}
    top = list(dict.fromkeys(held[r.casefold()] for r in required if r.casefold() in held))[:3]
    return f"{headline}. {years:.0f}+ years" + (f"; {', '.join(top)}." if top else ".")


def _order_skills(doc: ResumeDoc, order: list[str]) -> None:
    rank = {s.casefold(): i for i, s in enumerate(order)}
    big = len(order)
    groups = [(g, sorted(items, key=lambda x: rank.get(x.casefold(), big)))
              for g, items in doc.skills]
    groups.sort(key=lambda g: min((rank.get(x.casefold(), big) for x in g[1]), default=big))
    doc.skills = groups


def _select_bullets(base: ResumeDoc, selected: list[str], priority: list[str],
                    texts: dict[str, str]) -> ResumeDoc:
    """Keep selected bullets (selected order) plus each block's first bullet."""
    pos = {fid: i for i, fid in enumerate(selected)}
    prio = {fid: i for i, fid in enumerate(priority)}
    doc = base.model_copy(deep=True)

    def pick(bullets: list[Bullet], keep_first: bool) -> list[Bullet]:
        chosen = [b for b in bullets if b.fact_id in pos]
        if keep_first and bullets and not chosen:
            chosen = [bullets[0]]
        chosen.sort(key=lambda b: pos.get(b.fact_id, -1))
        return [Bullet(fact_id=b.fact_id, text=texts.get(b.fact_id, b.text),
                       priority=prio.get(b.fact_id, len(prio) + pos.get(b.fact_id, 0)))
                for b in chosen]

    for block in doc.roles + doc.projects:
        block.bullets = pick(block.bullets, keep_first=True)
    doc.achievements = pick(doc.achievements, keep_first=False)
    return doc


def _bullet_ids(doc: ResumeDoc) -> list[str]:
    return [b.fact_id for r in doc.roles + doc.projects for b in r.bullets] + \
        [b.fact_id for b in doc.achievements]


def _next_file_version(directory: Path) -> int:
    found = [int(m.group(1)) for p in directory.iterdir() if (m := _VERSION_RE.match(p.name))]
    return max(found, default=0) + 1


class TailorAgent:
    name = "resume_tailor"
    prompt_version = PROMPT_VERSION
    output_model = TailoringPlan
    tools = ("application_loader", "profile_loader", "profile_file", "artifact_dir",
             "tech_terms")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> TailoringPlan:
        if ctx.gateway is None:
            raise OutputRejected("no_model_gateway")
        load_app: Callable[[str], dict | None] = ctx.tools["application_loader"]
        load_profile: Callable[[], tuple[int, list[dict], float]] = ctx.tools["profile_loader"]
        profile_file: Callable[[], ProfileFile] = ctx.tools["profile_file"]
        artifact_dir: Callable[[str], Path] = ctx.tools["artifact_dir"]
        tech_terms: Callable[[], set[str]] = ctx.tools["tech_terms"]

        app = load_app(inp.entity_id)
        if app is None:
            raise OutputRejected("application_not_found")
        if app["status"] not in TAILORABLE:
            raise OutputRejected("not_tailorable")
        version, facts, years = load_profile()
        if not facts:
            raise OutputRejected("no_verified_facts")
        verified = {f["fact_id"]: f["value"] for f in facts}
        pf = profile_file()
        base = base_resume(pf, _fact_rows(version, facts))
        evidence = {k: v for k, v in (app.get("match_evidence") or {}).items()
                    if k in verified}
        req = app.get("requirements") or {}

        instructions = (
            "Tailor the resume to the job described in the untrusted content block.\n"
            f"Candidate facts (verified): {canonical_json(facts)}\n"
            f"Match evidence fact_ids: {canonical_json(sorted(evidence))}"
        )
        job_block = (
            f"Title: {app.get('title') or 'unknown'}\n"
            f"Company: {app.get('company') or 'unknown'}\n"
            f"Requirements: {canonical_json(req)}\n"
            f"Summary: {app.get('job_summary') or ''}"
        )
        messages = build_messages(_system_prompt(), instructions, untrusted=job_block)
        raw, _meta = await ctx.gateway.generate(_ROUTE, messages, _TailorLLM, PROMPT_VERSION)

        cited = raw.selected_fact_ids + raw.priority + [r.fact_id for r in raw.bullet_rewrites]
        if any(fid not in verified for fid in cited):
            raise OutputRejected("unverified_fact")

        skills = _verified_skills(facts)
        terms = tech_terms()
        allowed = set(skills)
        bullet_ids = set(_bullet_ids(base))
        texts: dict[str, str] = {}
        accepted: list[BulletRewrite] = []
        rejected: list[str] = []
        for rw in raw.bullet_rewrites:
            text = rw.text.strip()
            if rw.fact_id not in bullet_ids or \
                    validate_rewrite(verified[rw.fact_id], text, allowed, terms):
                rejected.append(rw.fact_id)
                continue
            texts[rw.fact_id] = text
            accepted.append(BulletRewrite(fact_id=rw.fact_id, text=text))

        canon = {s.casefold(): s for s in skills}
        skills_order = list(dict.fromkeys(canon[s.strip().casefold()] for s in raw.skills_order
                                          if s.strip().casefold() in canon))
        unsupported = [s for s in raw.skills_order if s.strip().casefold() not in canon]

        selected = list(dict.fromkeys(f for f in raw.selected_fact_ids if f in bullet_ids))
        priority = list(dict.fromkeys(raw.priority))
        tailored = _select_bullets(base, selected, priority, texts)
        _order_skills(tailored, skills_order)
        source = " ".join(verified[f] for f in _bullet_ids(tailored))
        summary = raw.summary.strip()
        if not summary or validate_rewrite(source, summary, allowed, terms):
            summary = fallback_summary(base.headline, years,
                                       list(req.get("required_skills") or []), skills)
        tailored.summary = summary

        out_dir = artifact_dir(inp.entity_id)
        n = _next_file_version(out_dir)
        pdf_path = out_dir / f"resume_v{n}.pdf"
        docx_path = out_dir / f"resume_v{n}.docx"
        fitted, fit_dropped = await fit_one_page(tailored, pdf_path)
        render_resume_docx(fitted, docx_path)

        kept = set(_bullet_ids(fitted))
        dropped = [f for f in _bullet_ids(base) if f not in kept]
        used = _bullet_ids(fitted) + [e.fact_id for e in pf.education if e.fact_id in verified]
        used += [f["fact_id"] for f in facts if f["category"] == "skill"]
        diff = bullet_diff(base, fitted, set(rejected), set(dropped))
        return TailoringPlan(
            task_id=ctx.task_id, created_at=datetime.now(UTC),
            warnings=[f"fit_dropped:{f}" for f in fit_dropped],
            application_id=inp.entity_id, profile_version=version, summary=summary,
            selected_fact_ids=selected, bullet_rewrites=accepted, skills_order=skills_order,
            priority=priority, rewrites_rejected=rejected, unsupported_claims=unsupported,
            pdf_path=str(pdf_path), docx_path=str(docx_path), diff=diff,
            dropped_fact_ids=dropped, used_fact_ids=list(dict.fromkeys(used)))


def make_tailor_handler(storage_path: Path) -> Handler:
    """Records the agent's rendered files as versioned artifacts; renders nothing."""
    def tailor_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
        if not isinstance(out, TailoringPlan) or out.application_id != task.entity_id:
            raise OutputRejected("tailor output does not match task application")
        app = get_application(s, out.application_id)
        if app.status not in TAILORABLE:
            raise OutputRejected("not_tailorable")
        verified = {f.fact_id for f in s.query(CandidateFactRow).filter(
            CandidateFactRow.profile_version == out.profile_version,
            CandidateFactRow.verified_by_user.is_(True))}
        if any(fid not in verified for fid in out.used_fact_ids):
            raise OutputRejected("unverified_fact")
        root = storage_path / "artifacts" / out.application_id
        pdf, docx = Path(out.pdf_path), Path(out.docx_path)
        for p in (pdf, docx):
            if not p.resolve().is_relative_to(root.resolve()) or not p.is_file():
                raise OutputRejected("artifact_path_invalid")
        if app.status == ApplicationStatus.NOT_STARTED.value:
            transition(s, app, ApplicationStatus.DOCUMENTS_PENDING, actor=_ACTOR,
                       reason="tailoring")
        diff_path = pdf.with_name(f"{pdf.stem}_diff.json")
        diff_path.write_text(canonical_json([r.model_dump(mode="json") for r in out.diff]),
                             encoding="utf-8")
        ids = out.used_fact_ids
        pdf_row = store.write_artifact(s, app.application_id, "resume", pdf, ids,
                                       root=storage_path)
        store.write_artifact(s, app.application_id, "resume_docx", docx, ids,
                             root=storage_path)
        store.write_artifact(s, app.application_id, "resume_diff", diff_path, ids,
                             root=storage_path)
        mutations.set_artifact(s, app.application_id, "resume", pdf_row.artifact_id,
                               actor=_ACTOR)
        return [(TaskKind.COVER_LETTER, "application", app.application_id)]
    return tailor_handler
