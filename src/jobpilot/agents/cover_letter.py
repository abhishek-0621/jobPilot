"""COVER_LETTER: a short letter grounded in 2-3 verified facts. The model writes the
paragraphs; code adds greeting/sign-off, validates, retries once and renders one page."""

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
from jobpilot.data.tables import CandidateFactRow, JobRow, TaskRow
from jobpilot.documents import store
from jobpilot.documents.hygiene import avoid_note, clean_text, text_issues
from jobpilot.documents.model import split_skills
from jobpilot.documents.render import DocRenderError, html_to_pdf, render_cover_letter_html
from jobpilot.documents.validate import validate_cover_letter, validate_rewrite
from jobpilot.llm.prompts import build_messages
from jobpilot.orchestrator.documents_flow import current_artifact_version, stale_follow_up
from jobpilot.orchestrator.engine import (
    AgentContext,
    FollowUp,
    Handler,
    OutputRejected,
    TaskInput,
    TaskKind,
)
from jobpilot.orchestrator.state_machine import transition
from jobpilot.portals.registry import apply_mode
from jobpilot.profile.schema import ProfileFile
from jobpilot.util.hashing import canonical_json
from jobpilot.util.textmatch import has_phrase

PROMPT_VERSION = "cover_letter_v1"
_ROUTE = "cover_letter"
_ACTOR = "orchestrator"
GREETING = "Dear Hiring Team,"
CLOSING = "Sincerely,"
_VERSION_RE = re.compile(r"^cover_letter_v(\d+)\.pdf$")
# Ordinary capitalized sentence openers in letter prose; not proper nouns.
_LETTER_WORDS = frozenset({
    "i", "i'm", "i've", "my", "this", "that", "these", "there", "here", "it", "we", "our",
    "your", "you", "most", "recently", "currently", "previously", "today", "since", "during",
    "before", "after", "beyond", "both", "each", "also", "together", "thank", "thanks",
    "please", "outside", "alongside", "working", "building",
})


class _CoverLLM(BaseModel):
    paragraphs: list[str] = Field(min_length=2, max_length=4)
    evidence_fact_ids: list[str] = Field(min_length=2, max_length=3)


class CoverLetter(AgentOutput):
    application_id: str
    profile_version: int
    company: str
    role: str
    paragraphs: list[str]
    evidence_fact_ids: list[str]
    text: str
    word_count: int
    pdf_path: str
    txt_path: str


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts") / "cover_letter_v1.md").read_text("utf-8")


def compose(paragraphs: list[str], full_name: str) -> str:
    body = "\n\n".join(p.strip() for p in paragraphs)
    return f"{GREETING}\n\n{body}\n\n{CLOSING}\n{full_name}"


def _mask(paragraph: str, company: str, role: str) -> str:
    """Replace exact (case-insensitive) company/role occurrences, longest first.

    Partial role mentions are deliberately NOT masked: masking sub-phrases of an untrusted
    title would let numbers or proper nouns inside them pass as source (fails safe)."""
    for text, token in sorted(((company, "the employer"), (role, "the position")),
                              key=lambda t: -len(t[0])):
        paragraph = re.sub(re.escape(text), token, paragraph, flags=re.IGNORECASE)
    return paragraph


# Spelled-out forms of abbreviations a verified fact may use; allowed only when the
# abbreviation itself appears in the facts or skills.
_SPELLED_OUT = {
    "ai": ("artificial intelligence",), "ml": ("machine learning",),
    "genai": ("generative ai",), "nlp": ("natural language processing",),
    "llm": ("large language model", "large language models"),
    "llms": ("large language models",), "it": ("information technology",),
}
# Generic job-title words: a letter may say "Senior Engineer" for "Senior GenAI Engineer".
# Anything else from the untrusted title (names, numbers) still has to be in the facts.
_TITLE_WORDS = frozenset({
    "senior", "sr", "junior", "staff", "lead", "principal", "head", "chief", "founding",
    "engineer", "engineering", "developer", "scientist", "architect", "manager", "analyst",
    "specialist", "consultant", "researcher", "associate", "intern", "applied", "software",
    "machine", "learning", "full", "stack", "platform", "data", "research", "product",
    "forward", "deployed", "solutions", "backend", "frontend", "infrastructure", "member",
    "technical", "team", "agentic", "generative", "ai", "ml", "genai", "llm", "nlp",
    "mlops",
})
_JOIN_WORDS = frozenset({"for", "and", "of", "the", "in", "on", "at", "to", "with", "&"})


def _with_opening(paragraphs: list[str], company: str, role: str) -> list[str]:
    """Name the exact role and company in the first paragraph when the model left them out;
    the wording is fixed, so this cannot introduce a claim."""
    text = " ".join(paragraphs)
    if not paragraphs or (has_phrase(text, role) and has_phrase(text, company)):
        return paragraphs
    return [f"I am applying for the {role} role at {company}. {paragraphs[0]}".strip(),
            *paragraphs[1:]]


def _made_of_allowed_words(code: str, words_ok: set[str]) -> bool:
    """A multi-word proper noun passes when every word comes from the verified facts or the
    allowed vocabulary: "Senior Engineer", or a reworded award name built from a fact."""
    kind, _, noun = code.partition(":")
    words = [w for w in noun.split() if w not in _JOIN_WORDS]
    return kind == "new_proper_noun" and len(noun.split()) > 1 and bool(words) \
        and all(w in words_ok for w in words)


def _allowed_extras(source: str, skills: list[str], role: str) -> set[str]:
    words = set(re.findall(r"[a-z]+", " ".join([source, *skills]).casefold()))
    extra = {full for abbr, forms in _SPELLED_OUT.items() if abbr in words for full in forms}
    extra |= {w for w in re.findall(r"[a-z]+", role.casefold()) if w in _TITLE_WORDS}
    return extra


def _violations(raw: _CoverLLM, verified: dict[str, str], company: str, role: str,
                full_name: str, banned: list[str], others: list[str], skills: list[str],
                terms: set[str], extra_source: str = "") -> list[str]:
    ids = list(dict.fromkeys(raw.evidence_fact_ids))
    codes = [f"unverified_evidence:{i}" for i in ids if i not in verified]
    if not 2 <= len(ids) <= 3:
        codes.append("evidence_count")
    paragraphs = [p.strip() for p in raw.paragraphs]
    if any(not p for p in paragraphs):
        codes.append("empty_paragraph")
    codes += [c for p in paragraphs for c in text_issues(p, no_exclamation=True)]
    codes += validate_cover_letter(compose(paragraphs, full_name), company, role, banned,
                                   others)
    # The job's company/role strings are untrusted: they never widen the source text. Their
    # exact occurrences are masked so a number or proper noun inside them cannot pass.
    # Every verified fact is true, so any of them may be named; evidence ids still have
    # to be 2-3 verified facts (checked above).
    source = " ".join([*verified.values(), extra_source])
    allowed = set(skills) | _LETTER_WORDS | _allowed_extras(source, skills, role)
    words_ok = {w for a in allowed for w in a.casefold().split()} \
        | set(re.findall(r"[a-z0-9.]+", source.casefold()))
    for n, p in enumerate(paragraphs):
        p = _mask(p, company, role)
        # Paragraph length is governed by the word limit, not the per-bullet ratio.
        codes += [f"p{n + 1}:{c}" for c in validate_rewrite(source, p, allowed, terms)
                  if c != "length" and not _made_of_allowed_words(c, words_ok)]
    return list(dict.fromkeys(codes))


def _next_file_version(directory: Path) -> int:
    found = [int(m.group(1)) for p in directory.iterdir() if (m := _VERSION_RE.match(p.name))]
    return max(found, default=0) + 1


class CoverLetterAgent:
    name = "cover_letter"
    prompt_version = PROMPT_VERSION
    output_model = CoverLetter
    tools = ("application_loader", "profile_loader", "profile_file", "artifact_dir",
             "tech_terms", "cover_letter_banned")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> CoverLetter:
        if ctx.gateway is None:
            raise OutputRejected("no_model_gateway")
        load_app: Callable[[str], dict | None] = ctx.tools["application_loader"]
        load_profile: Callable[[], tuple[int, list[dict], float]] = ctx.tools["profile_loader"]
        profile_file: Callable[[], ProfileFile] = ctx.tools["profile_file"]
        artifact_dir: Callable[[str], Path] = ctx.tools["artifact_dir"]
        tech_terms: Callable[[], set[str]] = ctx.tools["tech_terms"]
        load_banned: Callable[[], list[str]] = ctx.tools["cover_letter_banned"]

        app = load_app(inp.entity_id)
        if app is None:
            raise OutputRejected("application_not_found")
        if app["status"] != ApplicationStatus.DOCUMENTS_PENDING.value:
            raise OutputRejected("not_documents_pending")
        company, role = app.get("company") or "", app.get("title") or ""
        if not company or not role:
            raise OutputRejected("job_missing_company_or_title")
        version, facts, _years = load_profile()
        evidence_facts = [f for f in facts if f["category"] != "skill"]
        if not evidence_facts:
            raise OutputRejected("no_verified_facts")
        verified = {f["fact_id"]: f["value"] for f in evidence_facts}
        skills = [s for f in facts if f["category"] == "skill" for s in split_skills(f["value"])]
        preferred = [i for i in app.get("resume_fact_ids") or [] if i in verified]
        pf = profile_file()
        full_name = pf.identity.full_name
        # The candidate's own current title and employer are profile data, not claims.
        current = f"{pf.current_employment.title} {pf.current_employment.employer}"
        terms, banned = tech_terms(), load_banned()
        others = list(app.get("other_companies") or [])

        base_instructions = (
            "Write a cover letter for the role and company given in the untrusted job block.\n"
            f"Candidate facts (verified): {canonical_json(evidence_facts)}\n"
            f"Verified skills: {canonical_json(skills)}\n"
            f"Preferred evidence fact_ids (used in the tailored resume): "
            f"{canonical_json(preferred)}"
        )
        job_block = (
            f"Title: {role}\nCompany: {company}\n"
            f"Requirements: {canonical_json(app.get('requirements') or {})}\n"
            f"Summary: {app.get('job_summary') or ''}"
        )
        instructions = base_instructions
        for attempt in (1, 2):
            messages = build_messages(_system_prompt(), instructions, untrusted=job_block)
            raw, _meta = await ctx.gateway.generate(_ROUTE, messages, _CoverLLM,
                                                    PROMPT_VERSION)
            raw = raw.model_copy(update={"paragraphs": _with_opening(
                [clean_text(p) for p in raw.paragraphs], company, role)})
            codes = _violations(raw, verified, company, role, full_name, banned, others,
                                skills, terms, current)
            if not codes:
                break
            if attempt == 2:
                raise OutputRejected("cover_letter_invalid")
            instructions = (base_instructions + "\nYour previous attempt was rejected for: "
                            + ", ".join(codes) + ". Fix every one of these."
                            + avoid_note(codes))

        paragraphs = [p.strip() for p in raw.paragraphs]
        evidence = list(dict.fromkeys(raw.evidence_fact_ids))
        text = clean_text(compose(paragraphs, full_name))
        out_dir = artifact_dir(inp.entity_id)
        n = _next_file_version(out_dir)
        pdf_path = out_dir / f"cover_letter_v{n}.pdf"
        txt_path = out_dir / f"cover_letter_v{n}.txt"
        html = render_cover_letter_html(full_name, GREETING, paragraphs, CLOSING)
        if await html_to_pdf(html, pdf_path, author=full_name) != 1:
            pdf_path.unlink(missing_ok=True)
            raise DocRenderError("cover letter over one page")
        try:
            txt_path.write_text(text, encoding="utf-8")
        except BaseException:
            pdf_path.unlink(missing_ok=True)  # never leave an orphan PDF behind
            txt_path.unlink(missing_ok=True)
            raise
        return CoverLetter(
            task_id=ctx.task_id, created_at=datetime.now(UTC), application_id=inp.entity_id,
            profile_version=version, company=company, role=role, paragraphs=paragraphs,
            evidence_fact_ids=evidence, text=text, word_count=len(text.split()),
            pdf_path=str(pdf_path), txt_path=str(txt_path))


def make_cover_letter_handler(storage_path: Path) -> Handler:
    """Records the rendered letter (PDF + text) and moves the application on."""
    def cover_letter_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
        if not isinstance(out, CoverLetter) or out.application_id != task.entity_id:
            raise OutputRejected("cover letter output does not match task application")
        app = get_application(s, out.application_id)
        if app.status != ApplicationStatus.DOCUMENTS_PENDING.value:
            raise OutputRejected("not_documents_pending")
        job = s.get(JobRow, app.job_id)
        if job is None or (out.company, out.role) != (job.company, job.title):
            raise OutputRejected("company_role_mismatch")
        stale = stale_follow_up(
            s, task, app.application_id,
            [out.profile_version, current_artifact_version(s, app.resume_artifact_id)],
            _ACTOR)
        if stale is not None:
            return stale
        verified = {f.fact_id for f in s.query(CandidateFactRow).filter(
            CandidateFactRow.profile_version == out.profile_version,
            CandidateFactRow.verified_by_user.is_(True))}
        if not out.evidence_fact_ids or \
                any(fid not in verified for fid in out.evidence_fact_ids):
            raise OutputRejected("unverified_fact")
        root = storage_path / "artifacts" / out.application_id
        pdf, txt = Path(out.pdf_path), Path(out.txt_path)
        for p in (pdf, txt):
            if not p.resolve().is_relative_to(root.resolve()) or not p.is_file():
                raise OutputRejected("artifact_path_invalid")
        ids = out.evidence_fact_ids
        pdf_row = store.write_artifact(s, app.application_id, "cover_letter", pdf, ids,
                                       root=storage_path,
                                       profile_version=out.profile_version)
        store.write_artifact(s, app.application_id, "cover_letter_text", txt, ids,
                             root=storage_path,
                             profile_version=out.profile_version)
        mutations.set_artifact(s, app.application_id, "cover_letter", pdf_row.artifact_id,
                               actor=_ACTOR)
        transition(s, app, ApplicationStatus.FORM_IN_PROGRESS, actor=_ACTOR,
                   reason="documents_ready")
        automated = apply_mode(job) == "automated"
        kind = TaskKind.PREPARE_FORM if automated else TaskKind.MANUAL_PACKAGE
        return [(kind, "application", app.application_id)]
    return cover_letter_handler
