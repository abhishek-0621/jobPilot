"""Supervised essay answers: drafted only from the user's verified stories, verified facts
and canned answers. Every draft is FILL_AND_REVIEW (never AUTO_FILL); a question with no
suitable verified story is ASK_USER with a ``needs_story:<key>`` note. The model writes;
deterministic code validates (numbers, tech terms, proper nouns, leadership claims,
placeholders, ``missing`` notes, SWOT parts), retries once, then gives up to the user."""

import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from jobpilot.agents.cover_letter import _LETTER_WORDS, _mask
from jobpilot.contracts.enums import FieldDecision
from jobpilot.contracts.models import FieldAnswer
from jobpilot.documents.hygiene import avoid_note, clean_text, text_issues
from jobpilot.documents.validate import _PLACEHOLDER_RE, _numbers, validate_rewrite
from jobpilot.forms.essay import classify_essay
from jobpilot.llm.prompts import build_messages
from jobpilot.orchestrator.engine import OutputRejected
from jobpilot.portals.base import FormField
from jobpilot.profile.stories import (
    KEY_THEMES,
    QuestionKey,
    StoredBank,
    Story,
    Theme,
    stored_bank,
)
from jobpilot.util.hashing import canonical_json
from jobpilot.util.textmatch import has_phrase

PROMPT_VERSION = "essay_v1"
ROUTE = "essay"
ESSAY_PREFIX = "essay:"
DEFAULT_MAXLENGTH = 1200
MAX_STORIES = 2
_NOTE_MAX = 160
_SWOT_LABELS = ("strengths", "weaknesses", "opportunities", "threats")
_SWOT_RE = re.compile(r"(?im)^\s*(strengths?|weaknesses?|opportunities|threats)\s*[:\-–]\s*")
_ESSAY_WORDS = _LETTER_WORDS | frozenset({
    "strengths", "weaknesses", "opportunities", "threats", "strength", "weakness",
    "when", "what", "one", "the", "a", "an", "and", "but", "so", "then", "now", "as",
    "looking", "in", "on", "at", "for", "with", "from", "by", "to", "of", "if",
})
_SWOT_STRENGTH_THEMES = (Theme.STRENGTHS, Theme.ACHIEVEMENT, Theme.PROUD_OF,
                         Theme.BUILT_EXPLAIN)


class Gateway(Protocol):
    async def generate(self, route: str, messages: list[dict], schema: type[Any],
                       prompt_version: str) -> tuple[Any, Any]: ...


class _EssayLLM(BaseModel):
    answer: str
    story_ids: list[str] = Field(default_factory=list, max_length=MAX_STORIES)
    fact_ids: list[str] = Field(default_factory=list, max_length=4)


class EssayDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: QuestionKey
    answer: str | None
    decision: FieldDecision
    story_ids: list[str] = []
    fact_ids: list[str] = []
    note: str = ""

    @property
    def source(self) -> str:
        if self.note.startswith("canned:"):
            return f"{ESSAY_PREFIX}canned:{self.key.value}"
        return ESSAY_PREFIX + ",".join(self.story_ids)


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts") / "essay_v1.md").read_text("utf-8")


def _job_terms(job: dict[str, Any]) -> list[str]:
    req = job.get("requirements") or {}
    out: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str) and 1 < len(v) <= 40:
            out.append(v)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k != "summary":
                    walk(x)
    walk(req)
    return list(dict.fromkeys(out))


def _rank(themes: tuple[Theme, ...], job: dict[str, Any],
          stories: list[Story]) -> list[Story]:
    terms = _job_terms(job)
    scored = []
    for pos, st in enumerate(stories):
        if themes[0] in st.themes:
            theme = 2
        elif any(t in st.themes for t in themes):
            theme = 1
        else:
            continue
        text = st.source_text()
        overlap = sum(1 for t in terms if has_phrase(text, t))
        scored.append((-theme, -overlap, pos, st))
    return [st for *_, st in sorted(scored, key=lambda x: x[:3])]


def select_stories(key: QuestionKey, job: dict[str, Any], stories: list[Story]) -> list[Story]:
    """At most two stories for ``key``: theme match first, then overlap with the job's
    extracted requirements, then the user's own order. Callers pass verified stories."""
    if key == QuestionKey.SWOT:
        strong = _rank(_SWOT_STRENGTH_THEMES, job, stories)[:1]
        weak = _rank((Theme.WEAKNESSES,), job, stories)[:1]
        return strong + [st for st in weak if st not in strong]
    return _rank(KEY_THEMES[key], job, stories)[:MAX_STORIES]


def _missing_phrases(stories: list[Story]) -> list[str]:
    out = []
    for st in stories:
        for note in st.missing:
            text = re.sub(r"\[[^\]]*\]", " ", note)
            text = " ".join(re.sub(r"[^\w\s%.-]", " ", text).split())
            if len(text) >= 4:
                out.append(text)
    return out


def _rewrite_codes(source: str, text: str, allowed: set[str], terms: set[str] | None,
                   company: str, role: str) -> list[str]:
    return [c for c in validate_rewrite(source, _mask(text, company, role), allowed, terms)
            if c != "length"]


_ANY_LABEL_RE = re.compile(r"(?m)^\s*([A-Za-z][A-Za-z ]{0,30}?)\s*:")


def _swot_sections(answer: str) -> tuple[dict[str, str], bool]:
    """(sections, well_formed). Well formed: nothing before the first label, each of the
    four labels exactly once, no other ``Label:`` lines."""
    marks = list(_SWOT_RE.finditer(answer))
    out: dict[str, str] = {}
    ok = bool(marks) and not answer[:marks[0].start()].strip()
    for i, m in enumerate(marks):
        label = m.group(1).casefold()
        label = {"strength": "strengths", "weakness": "weaknesses"}.get(label, label)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(answer)
        if label in out:
            ok = False
        out[label] = answer[m.end():end].strip()
    others = [m for m in _ANY_LABEL_RE.finditer(answer)
              if not _SWOT_RE.match(answer, m.start())]
    if others or set(out) != set(_SWOT_LABELS) or not all(out.values()):
        ok = False
    return out, ok


def _weakness_source(bank: StoredBank, chosen: list[Story]) -> str:
    parts = [st.source_text() for st in chosen if Theme.WEAKNESSES in st.themes]
    if QuestionKey.WEAKNESSES in bank.canned:
        parts.append(bank.canned[QuestionKey.WEAKNESSES])
    return "\n".join(parts)


def essay_violations(raw: _EssayLLM, key: QuestionKey, chosen: list[Story],
                     bank: StoredBank, facts: dict[str, str], skills: list[str],
                     job: dict[str, Any], maxlength: int,
                     terms: set[str] | None = None) -> list[str]:
    answer = raw.answer.strip()
    codes: list[str] = []
    if not answer:
        return ["empty"]
    if len(answer) > maxlength:
        codes.append("too_long")
    codes += text_issues(answer)
    if _PLACEHOLDER_RE.search(answer) or re.search(r"\bADD\b", answer):
        codes.append("placeholder")
    chosen_ids = {st.story_id for st in chosen}
    ids = list(dict.fromkeys(raw.story_ids))
    if not ids:
        codes.append("no_story")
    codes += [f"unknown_story:{i}" for i in ids if i not in chosen_ids]
    codes += [f"unverified_fact:{i}" for i in dict.fromkeys(raw.fact_ids) if i not in facts]
    used = [st for st in chosen if st.story_id in ids]
    cited = [facts[i] for i in raw.fact_ids if i in facts]
    source = "\n".join([st.source_text() for st in used] + cited)
    company, role = str(job.get("company") or ""), str(job.get("title") or "")
    allowed = set(skills) | _ESSAY_WORDS
    for phrase in _missing_phrases(chosen):
        if has_phrase(answer, phrase) and not has_phrase(source, phrase):
            codes.append("missing_note")
    if key == QuestionKey.SWOT:
        parts, well_formed = _swot_sections(answer)
        if not well_formed:
            codes.append("swot_parts")
        weak = _weakness_source(bank, chosen)
        # the whole answer against everything it may draw on (catches text anywhere)
        whole = _SWOT_RE.sub(" ", answer)
        codes += _rewrite_codes(source + "\n" + weak, whole, allowed, terms, company, role)
        for label, text in parts.items():
            src = weak if label in ("weaknesses", "threats") else source
            codes += [f"{label}:{c}" for c in _rewrite_codes(src, text, allowed, terms,
                                                             company, role)]
    else:
        codes += _rewrite_codes(source, answer, allowed, terms, company, role)
    return list(dict.fromkeys(codes))


def _ask(key: QuestionKey, note: str) -> EssayDraft:
    return EssayDraft(key=key, answer=None, decision=FieldDecision.ASK_USER, note=note)


def _canned(key: QuestionKey, bank: StoredBank, facts: dict[str, str],
            maxlength: int) -> EssayDraft:
    text = clean_text(bank.canned[key].strip())
    if len(text) > maxlength:
        return _ask(key, "canned_too_long")
    source = "\n".join([st.source_text() for st in bank.verified()] + list(facts.values()))
    note = f"canned:{key.value}"
    if _numbers(text) - _numbers(source):
        note += ":check_numbers"  # the user's own words; flagged for their review
    return EssayDraft(key=key, answer=text, decision=FieldDecision.FILL_AND_REVIEW, note=note)


def _story_payload(st: Story) -> dict[str, Any]:
    """What the model sees of a story: never its ``missing`` notes."""
    return st.model_dump(mode="json", exclude={"missing", "verified"})


class EssayAgent:
    """Drafts one essay answer (route ``essay``). Runs inside PREPARE_FORM and
    MANUAL_PACKAGE; the single-writer handlers store the result."""

    name = "essay"
    prompt_version = PROMPT_VERSION

    async def draft(self, gateway: Gateway | None, *, question: str, key: QuestionKey,
                    bank: StoredBank, facts: dict[str, str], skills: list[str],
                    job: dict[str, Any], maxlength: int | None = None,
                    terms: set[str] | None = None) -> EssayDraft:
        limit = maxlength or DEFAULT_MAXLENGTH
        if key in bank.canned:
            return _canned(key, bank, facts, limit)
        chosen = select_stories(key, job, bank.verified())
        if not chosen:
            return _ask(key, f"needs_story:{key.value}")
        if key == QuestionKey.SWOT and not _weakness_source(bank, chosen):
            return _ask(key, "needs_story:weaknesses")
        if key == QuestionKey.SWOT and not any(Theme.WEAKNESSES not in st.themes
                                               for st in chosen):
            return _ask(key, "needs_story:strengths")
        if gateway is None:
            return _ask(key, "no_model_gateway")
        weak = _weakness_source(bank, chosen) if key == QuestionKey.SWOT else ""
        base = (
            f"Draft the answer to the {key.value} question in the untrusted block, in at "
            f"most {limit} characters.\n"
            f"Verified stories (the only experiences you may describe): "
            f"{canonical_json([_story_payload(st) for st in chosen])}\n"
            f"Verified facts (cite by fact_id if used): {canonical_json(facts)}\n"
            f"Verified skills: {canonical_json(skills)}\n"
            f"Candidate's standing rules: {canonical_json(bank.standing_rules)}"
            + (f"\nWeakness material (Weaknesses/Threats only): {canonical_json(weak)}"
               if weak else ""))
        untrusted = (f"Question: {question}\nJob title: {job.get('title') or ''}\n"
                     f"Company: {job.get('company') or ''}\n"
                     f"Job summary: {job.get('job_summary') or ''}")
        instructions = base
        codes: list[str] = []
        for attempt in (1, 2):
            messages = build_messages(_system_prompt(), instructions, untrusted=untrusted)
            raw, _meta = await gateway.generate(ROUTE, messages, _EssayLLM, PROMPT_VERSION)
            raw = raw.model_copy(update={"answer": clean_text(raw.answer)})
            codes = essay_violations(raw, key, chosen, bank, facts, skills, job, limit, terms)
            if not codes:
                return EssayDraft(
                    key=key, answer=raw.answer.strip(),
                    decision=FieldDecision.FILL_AND_REVIEW,
                    story_ids=list(dict.fromkeys(raw.story_ids)),
                    fact_ids=list(dict.fromkeys(raw.fact_ids)),
                    note=f"drafted:{key.value}")
            if attempt == 1:
                instructions = (base + "\nYour previous attempt was rejected for: "
                                + ", ".join(codes) + ". Fix every one of these."
                                + avoid_note(codes))
        return _ask(key, ("essay_invalid:" + ",".join(codes))[:_NOTE_MAX])


# ---- wiring: form prep / manual package ---------------------------------------------

LIBRARY_PREFIX = "essay_"
_DRAFT_CONFIDENCE = 0.5


@dataclass(frozen=True)
class EssayContext:
    """Everything an essay draft may draw on, loaded for one application."""
    bank: StoredBank
    facts: dict[str, str]
    skills: list[str]
    job: dict[str, Any]
    library: dict[str, Any] = field(default_factory=dict)


def library_key(key: QuestionKey) -> str:
    return LIBRARY_PREFIX + key.value


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == []


def essay_candidates(fields: list[FormField],
                     answers: list[FieldAnswer]) -> list[tuple[FormField, QuestionKey]]:
    """Long-text essay questions the deterministic mapper left without a value."""
    by_id = {a.field_id: a for a in answers}
    out = []
    for f in fields:
        a = by_id.get(f.field_id)
        if a is None or not _empty(a.proposed_value) or a.decision not in (
                FieldDecision.ASK_USER, FieldDecision.NOT_APPLICABLE):
            continue
        key = classify_essay(f.label, f.help_text, field_type=f.field_type,
                             options=f.options)
        if key is not None:
            out.append((f, key))
    return out


def to_field_answer(f: FormField, d: EssayDraft) -> FieldAnswer:
    if d.decision == FieldDecision.FILL_AND_REVIEW and d.answer:
        n = len(d.story_ids)
        why = (f"Essay ({d.key.value}): your canned answer" if d.note.startswith("canned:")
               else f"Essay ({d.key.value}) drafted from {n} verified "
                    f"{'story' if n == 1 else 'stories'}; review before use")
        if d.note.endswith(":check_numbers"):
            why += "; check its numbers"
        return FieldAnswer(field_id=f.field_id, label=f.label, field_type=f.field_type,
                           proposed_value=d.answer, decision=FieldDecision.FILL_AND_REVIEW,
                           source_fact_id=d.source, confidence=_DRAFT_CONFIDENCE,
                           rationale=why[:200])
    return FieldAnswer(field_id=f.field_id, label=f.label, field_type=f.field_type,
                       proposed_value=None, decision=FieldDecision.ASK_USER,
                       source_fact_id=None, confidence=0.0,
                       rationale=f"Essay ({d.key.value}): {d.note}"[:200])


async def draft_essay_answers(fields: list[FormField], answers: list[FieldAnswer],
                              gateway: Gateway | None, ectx: EssayContext,
                              terms: set[str] | None = None) -> list[FieldAnswer]:
    """Replace the empty answers of essay questions with a draft (or an ASK_USER note).
    An answer saved to the library for that question type is used first."""
    better: dict[str, FieldAnswer] = {}
    agent = EssayAgent()
    for f, key in essay_candidates(fields, answers):
        saved = ectx.library.get(library_key(key))
        if isinstance(saved, str):
            saved = {"text": saved, "story_ids": []}
        text = saved.get("text") if isinstance(saved, dict) else None
        if isinstance(text, str) and text.strip():
            ids = [str(i) for i in saved.get("story_ids") or []]
            src = f"library:{library_key(key)}" + (":" + ",".join(ids) if ids else "")
            better[f.field_id] = FieldAnswer(
                field_id=f.field_id, label=f.label, field_type=f.field_type,
                proposed_value=text.strip(), decision=FieldDecision.FILL_AND_REVIEW,
                source_fact_id=src, confidence=_DRAFT_CONFIDENCE,
                rationale=f"Essay ({key.value}): your saved answer for this question type")
            continue
        d = await agent.draft(gateway, question=f.label + (
            f"\n{f.help_text}" if f.help_text else ""), key=key, bank=ectx.bank,
            facts=ectx.facts, skills=ectx.skills, job=ectx.job, maxlength=f.maxlength,
            terms=terms)
        better[f.field_id] = to_field_answer(f, d)
    return [better.get(a.field_id, a) for a in answers]


def guard_essay_answers(s: Session, answers: list[FieldAnswer]) -> list[FieldAnswer]:
    """Handler-side check (single writer): an essay answer is never AUTO_FILL, and a draft
    whose stories are no longer verified goes back to the user."""
    from jobpilot.data.mutations import essay_refs

    bank = stored_bank(s)
    verified = {st.story_id for st in bank.verified()}
    canned = {k.value for k in bank.canned}
    out = []
    for a in answers:
        refs = essay_refs(a.source_fact_id)
        if refs is None:
            out.append(a)
            continue
        if a.decision not in (FieldDecision.FILL_AND_REVIEW, FieldDecision.ASK_USER):
            raise OutputRejected("essay_answer_not_reviewed")
        ids, key = refs
        if any(i not in verified for i in ids) or (key is not None and key not in canned):
            a = a.model_copy(update={
                "proposed_value": None, "decision": FieldDecision.ASK_USER,
                "source_fact_id": None, "confidence": 0.0,
                "rationale": "Essay: story_changed before the draft was stored"})
        out.append(a)
    return out
