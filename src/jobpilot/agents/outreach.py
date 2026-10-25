"""OUTREACH_RESEARCH: find public recruiting contacts and draft one short, verified note.

JobPilot never sends it: the draft goes to the Outreach review queue and the user sends
it themselves. The model writes 1-3 sentences grounded in verified facts; code writes the
opening (role, company and the recorded application date only), greeting and sign-off,
validates, retries once and otherwise falls back to a deterministic template.
"""

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field

from jobpilot.agents.cover_letter import _LETTER_WORDS, _mask
from jobpilot.contracts.models import AgentOutput
from jobpilot.discovery.normalize import html_to_text
from jobpilot.documents.model import split_skills
from jobpilot.documents.validate import _PLACEHOLDER_RE, validate_rewrite
from jobpilot.llm.prompts import build_messages
from jobpilot.orchestrator.engine import AgentContext, OutputRejected, TaskInput
from jobpilot.outreach.contacts import (
    MAX_POSTING_CHARS,
    FoundContact,
    company_domain,
    extract_from_posting,
    rank,
)
from jobpilot.util.hashing import canonical_json
from jobpilot.util.textmatch import has_phrase

PROMPT_VERSION = "outreach_v1"
ROUTE = "outreach_draft"
MAX_WORDS = 120
GREETING = "Hello,"
CLOSING = "Best regards,"
POLITE_CLOSE = "Thank you for your time, and I would be glad to share more if it helps."
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


def opening(role: str, company: str, applied_on: str | None) -> str:
    """The first sentence; a date appears only when one was recorded."""
    day = _day(applied_on)
    if day is None:
        return f"I recently applied for the {role} role at {company}."
    return f"I applied for the {role} role at {company} on {day}."


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
