"""Classify open-ended (essay) application questions into a question key.

Deterministic phrase patterns only (config/essay_questions.yaml): label text can never
choose its own key by instruction. Sensitive questions are classified first and are never
essays; only long-text (textarea) fields qualify.
"""

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
    "family situation", "personal circumstances",
)


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
    return any(has_phrase(text, p) for p in _ESSAY_SENSITIVE)


def classify_essay(label: str, help_text: str = "", *, field_type: str = "textarea",
                   options: list[str] | None = None) -> QuestionKey | None:
    """The essay question key for a long-text field, or None (not an essay, sensitive,
    or not a long-text field)."""
    if field_type not in ESSAY_FIELD_TYPES:
        return None
    if is_sensitive_essay(label, help_text, options):
        return None
    patterns = load_essay_patterns()
    for text in (_normalize(label), _normalize(help_text)):
        if not text:
            continue
        for key, phrases in patterns.items():
            if any(has_phrase(text, p) for p in phrases):
                return key
    return None
