"""Deterministic form-field mapping; the LLM may only suggest a canonical key, never a value."""

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from jobpilot.contracts.enums import FieldDecision
from jobpilot.contracts.models import FieldAnswer
from jobpilot.forms.classify import classify_sensitive
from jobpilot.forms.model import AliasConfig, AliasEntry, MappingContext
from jobpilot.llm.prompts import build_messages
from jobpilot.portals.base import FormField, FormSnapshot
from jobpilot.util.textmatch import has_phrase

PROMPT_VERSION = "field_map_v1"
ROUTE = "form_mapping"
_RATIONALE_MAX = 200
_EXACT, _TOKEN, _SUGGESTED = 1.0, 0.8, 0.5
_CHOICE_TYPES = {"select", "radio"}
_FILE_KEYS = {"resume", "cover_letter"}
_PHONE_CC_KEY = "phone_country_code"
_EXPERIENCE_KEY = "total_experience"

_NOISE = re.compile(r"\*|\((?:required|optional)\)", re.IGNORECASE)
_NON_WORD = re.compile(r"[\W_]+")
_PATH_PART = re.compile(r"^(\w+)(?:\[(\d+)\])?$")
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)")
_PLUS = re.compile(r"(\d+(?:\.\d+)?)\s*\+")
_THIRD_PARTY = ("referrer", "referral", "referred", "reference", "references", "manager",
                "supervisor", "emergency", "recruiter", "spouse", "partner", "parent",
                "guardian", "kin", "contact person", "hiring manager", "interviewer")
_ARTIFACT_ATTR = {"resume": "resume_path", "cover_letter": "cover_letter_path",
                  "cover_letter_text": "cover_letter_text"}


class Gateway(Protocol):
    async def generate(self, route: str, messages: list[dict], schema: type[Any],
                       prompt_version: str) -> tuple[Any, Any]: ...


def normalize_label(s: str) -> str:
    """Lowercase, drop '*'/'(required)'/'(optional)', strip punctuation, collapse spaces."""
    return " ".join(_NON_WORD.sub(" ", _NOISE.sub(" ", s).casefold()).split())


def _is_third_party(label: str) -> bool:
    """Label asks about someone other than the applicant (referrer, manager, ...)."""
    norm = normalize_label(label)
    return any(has_phrase(norm, t) for t in _THIRD_PARTY)


def _clip(text: str) -> str:
    return text[:_RATIONALE_MAX]


# ---- alias matching -------------------------------------------------------------------


def _match_key(label: str, aliases: AliasConfig, *, files: bool) -> tuple[str | None, float, str]:
    """Return (key, confidence, how). key None + how 'ambiguous' means a token-subset tie."""
    norm = normalize_label(label)
    fields = {k: e for k, e in aliases.fields.items() if (k in _FILE_KEYS) == files}
    for key, entry in fields.items():
        for alias in entry.aliases:
            if normalize_label(alias) == norm:
                return key, _EXACT, f"Exact alias '{alias}'"
    tokens = set(norm.split())
    best: dict[str, str] = {}
    best_len = 0
    for key, entry in fields.items():
        for alias in entry.aliases:
            a_tokens = set(normalize_label(alias).split())
            if not a_tokens or not a_tokens <= tokens:
                continue
            if len(a_tokens) > best_len:
                best, best_len = {key: alias}, len(a_tokens)
            elif len(a_tokens) == best_len:
                best.setdefault(key, alias)
    if len(best) == 1:
        [(key, alias)] = best.items()
        return key, _TOKEN, f"Token match '{alias}'"
    if len(best) > 1:
        return None, 0.0, "ambiguous"
    return None, 0.0, "none"


def _file_key(label: str) -> str | None:
    tokens = set(normalize_label(label).split())
    if "resume" in tokens or "cv" in tokens:
        return "resume"
    if "cover letter" in normalize_label(label):
        return "cover_letter"
    return None


# ---- value resolution -----------------------------------------------------------------


def _walk(root: Any, parts: list[str]) -> Any:
    cur = root
    for part in parts:
        m = _PATH_PART.match(part)
        if m is None or cur is None:
            return None
        name, idx = m.group(1), m.group(2)
        cur = cur.get(name) if isinstance(cur, dict) else getattr(cur, name, None)
        if idx is not None:
            i = int(idx)
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
    return cur


def _resolve_one(path: str, ctx: MappingContext) -> tuple[Any, str]:
    """Return (value, source_fact_id) for one profile path."""
    head, _, rest = path.partition(".")
    if head == "artifact":
        return getattr(ctx.artifacts, _ARTIFACT_ATTR.get(rest, ""), None), f"artifact:{rest}"
    if head == "answer_library":
        entry = ctx.library.get(rest)
        return (entry[0] if entry else None), f"library:{rest}"
    roots: dict[str, Any] = {"identity": ctx.identity, "links": ctx.links,
                             "current_employment": ctx.current_employment,
                             "education": ctx.education, "languages": ctx.languages}
    return _walk(roots, path.split(".")), f"profile:{path}"


def _is_empty(v: Any) -> bool:
    return v is None or v == "" or v == []


def _text(v: Any) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, list):
        return ", ".join(_text(x) for x in v if not _is_empty(x))
    return str(v)


def _band(value: float, options: list[str]) -> str | None:
    for opt in options:
        if m := _RANGE.search(opt):
            if float(m.group(1)) <= value <= float(m.group(2)):
                return opt
        elif (m := _PLUS.search(opt)) and value >= float(m.group(1)):
            return opt
    return None


def _match_option(value: str, entry: AliasEntry, options: list[str]) -> str | None:
    by_fold = {o.casefold().strip(): o for o in options}
    synonyms = [s.casefold() for s in entry.options_match]
    if value.casefold() in synonyms:
        for s in synonyms:
            if s in by_fold:
                return by_fold[s]
    return by_fold.get(value.casefold().strip())


class _Resolved(BaseModel):
    value: str | bool | None
    source: str | None
    from_library: bool = False
    note: str = ""


def _value_for(key: str, entry: AliasEntry, f: FormField, ctx: MappingContext,
               has_cc: bool) -> _Resolved:
    if entry.profile is None:
        return _Resolved(value=None, source=None)
    if key == _EXPERIENCE_KEY:
        src = f"profile:{entry.profile}"
        years = ctx.years_experience
        if years is None:
            return _Resolved(value=None, source=src)
        if f.field_type in _CHOICE_TYPES:
            return _Resolved(value=_band(years, f.options), source=src, note="band")
        if f.field_type == "number":
            return _Resolved(value=str(round(years)), source=src)
        return _Resolved(value=f"{years:g}", source=src)
    if isinstance(entry.profile, list):
        parts = [_resolve_one(p, ctx) for p in entry.profile]
        vals = [v for v, _ in parts if not _is_empty(v)]
        src = "+".join(s for _, s in parts)
        return _Resolved(value=_text(vals) if vals else None, source=src)
    if key == "phone" and not has_cc:
        e164, src = _resolve_one("identity.phone_e164", ctx)
        if _is_empty(e164):
            nat, _ = _resolve_one("identity.phone_national", ctx)
            cc, _ = _resolve_one("identity.phone_country_code", ctx)
            e164 = (f"+{str(cc).lstrip('+')}{nat}"
                    if not _is_empty(nat) and not _is_empty(cc) else None)
        return _Resolved(value=None if e164 is None else str(e164), source=src)
    value, src = _resolve_one(entry.profile, ctx)
    lib = entry.profile.startswith("answer_library.")
    if _is_empty(value):
        return _Resolved(value=None, source=src, from_library=lib)
    if isinstance(value, Path):
        value = str(value)
    if f.field_type == "checkbox" and isinstance(value, bool):
        return _Resolved(value=value, source=src, from_library=lib)
    text = _text(value)
    if f.field_type in _CHOICE_TYPES:
        return _Resolved(value=_match_option(text, entry, f.options), source=src,
                         from_library=lib, note="option")
    return _Resolved(value=text, source=src, from_library=lib)


def _answer(f: FormField, decision: FieldDecision, rationale: str, *, value: Any = None,
            source: str | None = None, confidence: float = 0.0) -> FieldAnswer:
    return FieldAnswer(field_id=f.field_id, label=f.label, field_type=f.field_type,
                       proposed_value=value, decision=decision, source_fact_id=source,
                       confidence=confidence, rationale=_clip(rationale))


def _missing(f: FormField, rationale: str, source: str | None = None) -> FieldAnswer:
    decision = FieldDecision.ASK_USER if f.required else FieldDecision.NOT_APPLICABLE
    return _answer(f, decision, rationale, source=source)


def _answer_for_key(f: FormField, key: str, confidence: float, how: str,
                    ctx: MappingContext, has_cc: bool,
                    forced: FieldDecision | None = None) -> FieldAnswer:
    entry = ctx.aliases.fields[key]
    why = f"{how} → {key}"
    if entry.decision in (FieldDecision.USER_ONLY, FieldDecision.ASK_USER):
        return _answer(f, entry.decision, f"{why}: {entry.decision.value.lower()} field",
                       confidence=confidence)
    r = _value_for(key, entry, f, ctx, has_cc)
    if r.value is None:
        if r.note:
            return _answer(f, FieldDecision.ASK_USER, f"{why}: no matching {r.note}",
                           source=r.source, confidence=confidence)
        return _missing(f, f"{why}: no value on file", r.source)
    if forced is not None:
        decision = forced
    elif r.from_library:
        decision = FieldDecision.FILL_AND_REVIEW
    elif confidence < _EXACT:
        decision = FieldDecision.FILL_AND_REVIEW
    else:
        decision = entry.decision or FieldDecision.AUTO_FILL
    return _answer(f, decision, f"{why} ({r.source})", value=r.value, source=r.source,
                   confidence=confidence)


# ---- public API -----------------------------------------------------------------------


def _has_country_code(snapshot: FormSnapshot, aliases: AliasConfig) -> bool:
    return any(_match_key(f.label, aliases, files=False)[0] == _PHONE_CC_KEY
               for f in snapshot.fields
               if f.field_type != "file" and not _is_third_party(f.label))


def map_fields(snapshot: FormSnapshot,
               ctx: MappingContext) -> tuple[list[FieldAnswer], list[FormField]]:
    """Map every field to a decision; also return fields no rule matched (LLM candidates)."""
    has_cc = _has_country_code(snapshot, ctx.aliases)
    answers: list[FieldAnswer] = []
    unmatched: list[FormField] = []
    for f in snapshot.fields:
        if f.field_type == "file":
            key = _file_key(f.label)
            if key is None:
                answers.append(_answer(f, FieldDecision.ASK_USER, "Unrecognised file upload"))
            else:
                answers.append(_answer_for_key(f, key, _EXACT, "File upload", ctx, has_cc))
            continue
        category = classify_sensitive(f.label, f.help_text, f.options)
        if category is not None and category != "compensation":
            answers.append(_answer(f, FieldDecision.USER_ONLY,
                                   f"Sensitive ({category}): user only"))
            continue
        if _is_third_party(f.label):
            answers.append(_missing(f, "Asks about another person; not mapped"))
            continue
        key, conf, how = _match_key(f.label, ctx.aliases, files=False)
        if category == "compensation":
            answers.append(_compensation(f, key, conf, how, ctx))
            continue
        if key is None:
            why = ("Ambiguous: several aliases match equally" if how == "ambiguous"
                   else "No alias matched")
            answers.append(_missing(f, why))
            unmatched.append(f)
            continue
        answers.append(_answer_for_key(f, key, conf, how, ctx, has_cc))
    return answers, unmatched


def _compensation(f: FormField, key: str | None, conf: float, how: str,
                  ctx: MappingContext) -> FieldAnswer:
    entry = ctx.aliases.fields.get(key) if key else None
    path = entry.profile if entry else None
    if isinstance(path, str) and path.startswith("answer_library."):
        value, src = _resolve_one(path, ctx)
        if not _is_empty(value):
            return _answer(f, FieldDecision.FILL_AND_REVIEW,
                           f"{how} → {key} ({src})", value=_text(value), source=src,
                           confidence=conf)
    return _answer(f, FieldDecision.ASK_USER, "Compensation: no answer-library value")


class _Suggestion(BaseModel):
    field_id: str
    key: str


class _SuggestLLM(BaseModel):
    suggestions: list[_Suggestion]


def _suggestable_keys(aliases: AliasConfig) -> list[str]:
    return [k for k, e in aliases.fields.items()
            if e.profile is not None and k not in _FILE_KEYS
            and e.decision not in (FieldDecision.USER_ONLY, FieldDecision.ASK_USER)]


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts") / "field_map_v1.md").read_text("utf-8")


def _is_sensitive(f: FormField) -> bool:
    return (f.field_type == "file" or _is_third_party(f.label)
            or classify_sensitive(f.label, f.help_text, f.options) is not None)


async def suggest_keys(unmatched: list[FormField], gateway: Gateway,
                       aliases: AliasConfig) -> dict[str, str]:
    """Ask the model which canonical key each unmatched field means. Keys only, no values."""
    candidates = [f for f in unmatched if not _is_sensitive(f)]
    if not candidates:
        return {}
    keys = _suggestable_keys(aliases)
    instructions = (
        "For each form field in the untrusted content block, choose the canonical key it "
        "asks for, or \"none\". Allowed keys: " + ", ".join(keys) + ", none.")
    untrusted = "\n".join(
        json.dumps({"field_id": f.field_id, "label": f.label, "help_text": f.help_text,
                    "options": f.options}, ensure_ascii=False) for f in candidates)
    messages = build_messages(_system_prompt(), instructions, untrusted=untrusted)
    raw, _meta = await gateway.generate(ROUTE, messages, _SuggestLLM, PROMPT_VERSION)
    ids = {f.field_id for f in candidates}
    allowed = set(keys)
    return {s.field_id: s.key for s in raw.suggestions
            if s.field_id in ids and s.key in allowed}


def apply_suggestions(snapshot: FormSnapshot, unmatched: list[FormField],
                      suggestions: dict[str, str], ctx: MappingContext) -> list[FieldAnswer]:
    """Answers for model-suggested keys, one per suggested unmatched field.

    Merge contract: the caller replaces the answer from ``map_fields`` that has the same
    ``field_id``. A suggestion with a value is FILL_AND_REVIEW; one whose key has no value
    yields the ASK_USER (required) / NOT_APPLICABLE (optional) answer. Sensitive,
    third-party and unknown-key suggestions produce nothing (the original answer stands).
    """
    has_cc = _has_country_code(snapshot, ctx.aliases)
    allowed = set(_suggestable_keys(ctx.aliases))
    out: list[FieldAnswer] = []
    for f in unmatched:
        key = suggestions.get(f.field_id)
        if key is None or key not in allowed or _is_sensitive(f):
            continue
        a = _answer_for_key(f, key, _SUGGESTED, "Model", ctx, has_cc,
                            forced=FieldDecision.FILL_AND_REVIEW)
        if a.proposed_value is None:
            out.append(a)  # ASK_USER / NOT_APPLICABLE from the shared resolver
            continue
        out.append(a.model_copy(update={"rationale": "Model-suggested mapping"}))
    return out
