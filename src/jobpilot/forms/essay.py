"""Classify open-ended (essay) application questions into a question key.

Deterministic phrase patterns only (config/essay_questions.yaml): label text can never
choose its own key by instruction. Sensitive questions are classified first and are never
essays; only long-text (textarea) fields qualify.
"""

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from jobpilot.forms.classify import _normalize, classify_sensitive
from jobpilot.profile.stories import QuestionKey
from jobpilot.util.textmatch import has_phrase

ESSAY_PATTERNS_PATH = Path(__file__).resolve().parents[3] / "config" / "essay_questions.yaml"
ESSAY_FIELD_TYPES = frozenset({"textarea"})

# Personal circumstances the user answers themselves, never drafted from stories.
_ESSAY_SENSITIVE = (
    "reason for leaving", "reasons for leaving", "why did you leave", "why are you leaving",
    "why do you want to leave", "why are you looking to leave", "leaving your current",
    "leaving your last", "left your last", "left your previous", "employment gap",
    "gap in your employment", "gap in employment", "career gap", "career break",
    "notice period", "relocate", "relocation", "references", "health", "pregnant", "pregnancy",
    "family situation", "personal circumstances", "move on", "moving on", "change jobs",
    "changing jobs", "change job", "new role", "new job", "new opportunity",
    "new opportunities", "looking for a change", "previous employer", "current employer",
    "former employer", "last employer", "why are you leaving",
)
_ESSAY_SENSITIVE_RE = re.compile(r"\b(?:leav\w*|left|quit\w*)\b")


class _Patterns(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    keys: dict[QuestionKey, list[str]]


@lru_cache(maxsize=4)
def load_essay_patterns(path: Path = ESSAY_PATTERNS_PATH) -> dict[QuestionKey, list[str]]:
    data = _Patterns.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return {k: [_normalize(p) for p in v if _normalize(p)] for k, v in data.keys.items()}


def is_sensitive_essay(label: str, help_text: str = "",
                       options: list[str] | None = None) -> bool:
    if classify_sensitive(label, help_text, options) is not None:
        return True
    text = f"{_normalize(label)} {_normalize(help_text)}"
    return (_ESSAY_SENSITIVE_RE.search(text) is not None
            or any(has_phrase(text, p) for p in _ESSAY_SENSITIVE))


def _names_company(text: str, company: str | None) -> bool:
    """"why <company>", "what excites you about <company>" and the like."""
    name = _normalize(company or "")
    if len(name) < 2:
        return False
    return any(has_phrase(text, f"{lead} {name}") for lead in _COMPANY_LEADS)


_COMPANY_LEADS = ("why", "why join", "why work at", "why work for", "what excites you about",
                  "what draws you to", "what attracts you to", "interested in",
                  "interested in joining", "interested in working at",
                  "what do you know about", "excited about joining", "join",
                  "want to work at", "want to work for", "want to join")


def classify_essay(label: str, help_text: str = "", *, field_type: str = "textarea",
                   options: list[str] | None = None,
                   company: str | None = None) -> QuestionKey | None:
    """The essay question key for a long-text field, or None (not an essay, sensitive,
    or not a long-text field). ``company`` (the job's company name) also recognises
    "Why <company>?" questions."""
    if field_type not in ESSAY_FIELD_TYPES:
        return None
    if is_sensitive_essay(label, help_text, options):
        return None
    patterns = load_essay_patterns()
    for text in (_normalize(label), _normalize(help_text)):
        if not text:
            continue
        if _names_company(text, company) and not any(
                has_phrase(text, p) for p in patterns.get(QuestionKey.WHY_ROLE, [])):
            return QuestionKey.WHY_COMPANY
        for key, phrases in patterns.items():
            if any(has_phrase(text, p) for p in phrases):
                return key
    return None
