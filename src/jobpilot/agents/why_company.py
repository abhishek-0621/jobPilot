"""Why-company answers ("Why <company>?", "Why do you want to work here?"), written from
the company brief (recent, grounded items) and the candidate's verified facts and stories.

Never a generic answer: with no brief items the user is asked (ASK_USER) to add research
URLs. Drafts are FILL_AND_REVIEW only, and their source ``essay:why_company:<hash>`` pins
the brief they were written from, so a refreshed brief sends them back to the user.
Validation: hygiene (stock phrases, markdown, dashes, exclamation marks), length and
sentence count, at least one brief item named, and the cover-letter claim check: numbers,
tech terms and proper nouns must come from verified facts, the cited stories, the current
title or the brief items; company/product names may also come from the job text.
"""

import re
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field

from jobpilot.agents.cover_letter import (
    _LETTER_WORDS,
    _allowed_extras,
    _made_of_allowed_words,
    _mask,
)
from jobpilot.agents.essay import (
    _ESSAY_WORDS,
    EssayDraft,
    Gateway,
    _story_payload,
    select_stories,
)
from jobpilot.contracts.enums import FieldDecision
from jobpilot.documents.hygiene import avoid_note, clean_text, text_issues
from jobpilot.documents.validate import _PLACEHOLDER_RE, validate_rewrite
from jobpilot.llm.prompts import build_messages
from jobpilot.profile.stories import QuestionKey, StoredBank, Story
from jobpilot.research.brief import BriefItem
from jobpilot.util.hashing import canonical_json
from jobpilot.util.textmatch import has_phrase

PROMPT_VERSION = "why_company_v1"
ROUTE = "essay"
WHY_PREFIX = "essay:why_company:"
MIN_WORDS, MAX_WORDS = 90, 160
MIN_SENTENCES, MAX_SENTENCES = 3, 5
MAX_NAMED = 2
_NOTE_MAX = 160
_K = QuestionKey.WHY_COMPANY
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+(?=\s|$)|[^.!?]+$")
_WHY_WORDS = frozenset({"what", "when", "that", "which", "because", "if", "so", "and",
                        "but", "there", "their", "they", "one", "two"})


class _WhyLLM(BaseModel):
    answer: str
    item_names: list[str] = Field(default_factory=list, max_length=4)
    fact_ids: list[str] = Field(default_factory=list, max_length=4)
    story_ids: list[str] = Field(default_factory=list, max_length=2)


def no_brief_note(company: str) -> str:
    return (f"No recent company information found; add research URLs for "
            f"{company[:60]} in Companies")


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts") / "why_company_v1.md").read_text(
        "utf-8")


def sentences(text: str) -> list[str]:
    return [m.group().strip() for m in _SENTENCE_RE.finditer(text) if m.group().strip()]


def _brief_text(items: list[BriefItem]) -> str:
    return "\n".join(f"{i.name}. {i.one_line}" for i in items)


def _mask_items(text: str, items: list[BriefItem]) -> str:
    for i in sorted(items, key=lambda x: -len(x.name)):
        text = re.sub(rf"(?<!\w){re.escape(i.name)}(?!\w)", "the item", text,
                      flags=re.IGNORECASE)
    return text


def why_violations(raw: _WhyLLM, items: list[BriefItem], facts: dict[str, str],
                   skills: list[str], stories: list[Story], current: str,
                   job: dict[str, Any], maxlength: int | None,
                   terms: set[str] | None = None) -> list[str]:
    answer = raw.answer.strip()
    if not answer:
        return ["empty"]
    codes = text_issues(answer, no_exclamation=True)
    words = len(answer.split())
    if words < MIN_WORDS:
        codes.append("too_short")
    if words > MAX_WORDS or (maxlength is not None and len(answer) > maxlength):
        codes.append("too_long")
    if not MIN_SENTENCES <= len(sentences(answer)) <= MAX_SENTENCES:
        codes.append("sentence_count")
    if _PLACEHOLDER_RE.search(answer) or re.search(r"\bADD\b", answer):
        codes.append("placeholder")
    known = {i.name.casefold(): i for i in items}
    named = [n for n in dict.fromkeys(raw.item_names) if n.strip()]
    codes += [f"unknown_item:{n[:40]}" for n in named if n.casefold() not in known]
    mentioned = [i for i in items if has_phrase(answer, i.name)]
    if not mentioned:
        codes.append("no_company_item")
    elif len(mentioned) > MAX_NAMED:
        codes.append("too_many_items")
    codes += [f"unverified_fact:{i}" for i in dict.fromkeys(raw.fact_ids) if i not in facts]
    by_id = {st.story_id: st for st in stories}
    codes += [f"unknown_story:{i}" for i in dict.fromkeys(raw.story_ids) if i not in by_id]
    # Every verified fact is true and may be named (as in cover letters); the brief items
    # are grounded in public pages. The company name, role and item names are masked so a
    # number or proper noun hidden inside them cannot pass as source.
    company, role = str(job.get("company") or ""), str(job.get("title") or "")
    used = [by_id[i].source_text() for i in raw.story_ids if i in by_id]
    source = "\n".join([*facts.values(), *used, current, _brief_text(items)])
    allowed = set(skills) | _LETTER_WORDS | _ESSAY_WORDS | _WHY_WORDS \
        | _allowed_extras(source, skills, role)
    words_ok = {w for a in allowed for w in a.casefold().split()} \
        | set(re.findall(r"[a-z0-9.]+", source.casefold()))
    job_text = " ".join(str(job.get(k) or "") for k in ("company", "title", "job_summary"))
    text = _mask(_mask_items(answer, items), company, role)
    for c in validate_rewrite(source, text, allowed, terms):
        if c == "length" or _made_of_allowed_words(c, words_ok):
            continue
        if c.startswith("new_proper_noun:") and has_phrase(job_text, c.split(":", 1)[1]):
            continue  # a company / product name the posting itself uses
        codes.append(c)
    return list(dict.fromkeys(codes))


def _ask(note: str, brief_hash: str = "") -> EssayDraft:
    return EssayDraft(key=_K, answer=None, decision=FieldDecision.ASK_USER,
                      note=note[:_NOTE_MAX], brief_hash=brief_hash)


class WhyCompanyDrafter:
    """Drafts one why-company answer (route ``essay``)."""

    name = "why_company"
    prompt_version = PROMPT_VERSION

    async def draft(self, gateway: Gateway | None, *, question: str,
                    items: list[BriefItem] | None, brief_hash: str, bank: StoredBank,
                    facts: dict[str, str], skills: list[str], current: str,
                    job: dict[str, Any], maxlength: int | None = None,
                    terms: set[str] | None = None) -> EssayDraft:
        company = str(job.get("company") or "this company")
        if not items:
            return _ask(no_brief_note(company), brief_hash)
        if not facts and not bank.verified():
            return _ask("needs_verified_facts: verify your profile facts first", brief_hash)
        if gateway is None:
            return _ask("no_model_gateway", brief_hash)
        stories = select_stories(_K, job, bank.verified())
        payload = [i.model_dump(mode="json", exclude={"source_url"}) for i in items]
        base = (
            "Draft the candidate's answer to the company question in the untrusted block.\n"
            f"Company items (recent, from the company's own public pages; name one or "
            f"two of them exactly): {canonical_json(payload)}\n"
            f"Verified facts (the only experience you may describe; cite by fact_id): "
            f"{canonical_json(facts)}\n"
            f"Verified stories (optional detail): "
            f"{canonical_json([_story_payload(st) for st in stories])}\n"
            f"Verified skills: {canonical_json(skills)}\n"
            f"Candidate's current role: {canonical_json(current)}\n"
            f"Candidate's standing rules: {canonical_json(bank.standing_rules)}\n"
            f"Write {MIN_WORDS} to {MAX_WORDS} words in {MIN_SENTENCES} to {MAX_SENTENCES} "
            "sentences" + (f", at most {maxlength} characters." if maxlength else "."))
        untrusted = (f"Question: {question}\nCompany: {company}\n"
                     f"Job title: {job.get('title') or ''}\n"
                     f"Job summary: {job.get('job_summary') or ''}")
        instructions = base
        codes: list[str] = []
        for attempt in (1, 2):
            messages = build_messages(_system_prompt(), instructions, untrusted=untrusted)
            raw, _meta = await gateway.generate(ROUTE, messages, _WhyLLM, PROMPT_VERSION)
            raw = raw.model_copy(update={"answer": clean_text(raw.answer).strip()})
            codes = why_violations(raw, items, facts, skills, stories, current, job,
                                   maxlength, terms)
            if not codes:
                return EssayDraft(
                    key=_K, answer=raw.answer, decision=FieldDecision.FILL_AND_REVIEW,
                    story_ids=[i for i in dict.fromkeys(raw.story_ids)],
                    fact_ids=list(dict.fromkeys(raw.fact_ids)),
                    note="drafted:why_company", brief_hash=brief_hash)
            if attempt == 1:
                instructions = (base + "\nYour previous attempt was rejected for: "
                                + ", ".join(codes) + ". Fix every one of these."
                                + avoid_note(codes))
        found = "; ".join(i.name for i in items[:4])
        return _ask(f"write it yourself from: {found}", brief_hash)
