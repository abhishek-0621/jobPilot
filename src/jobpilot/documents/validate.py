"""Deterministic guard against invented claims in LLM-rewritten resume bullets."""

import re
from functools import lru_cache
from pathlib import Path

from jobpilot.util.textmatch import has_phrase

TECH_TERMS_PATH = Path(__file__).resolve().parents[3] / "config" / "tech_terms.txt"

MIN_RATIO = 0.3
MAX_RATIO = 1.6

# Sentence-initial verbs that are capitalized only by position.
_SENTENCE_STARTERS = {
    "built", "led", "designed", "developed", "shipped", "owned", "cut", "rebuilt", "run",
    "partner", "architected", "evaluated", "improved", "reduced", "created", "delivered",
    "migrated", "automated", "implemented",
}
_CONNECTORS = {"of", "and", "&", "for"}

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’.+#/-]*|&")


def load_tech_terms(path: Path | str) -> set[str]:
    """One term per line; blank lines and ``#`` comments ignored; lower-cased."""
    terms: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.add(line.casefold())
    return terms


@lru_cache(maxsize=1)
def _default_terms() -> frozenset[str]:
    return frozenset(load_tech_terms(TECH_TERMS_PATH))


def _numbers(text: str) -> set[str]:
    """Canonical numeric values: separators/suffixes/percent-ness are ignored."""
    return {m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(text)}


def _tech_in(text: str, terms: frozenset[str] | set[str]) -> set[str]:
    return {t for t in terms if has_phrase(text, t)}


def _strip_terms(text: str, terms: set[str]) -> str:
    for t in sorted(terms, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(t)}(?!\w)", " | ", text, flags=re.IGNORECASE)
    return text


def _flush(run: list[str], found: set[str]) -> None:
    while run and run[-1].lower() in _CONNECTORS:
        run.pop()
    if sum(1 for w in run if w.lower() not in _CONNECTORS) >= 2:
        found.add(" ".join(run).casefold())
    run.clear()


def _proper_nouns(text: str, tech_present: set[str]) -> set[str]:
    cleaned = _strip_terms(text, tech_present)
    found: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n|\|", cleaned):
        words = _WORD_RE.findall(sentence)
        words = [w.rstrip(".,;:") for w in words]
        run: list[str] = []
        for i, w in enumerate(words):
            is_cap = w[0].isupper()
            if i == 0 and w.lower() in _SENTENCE_STARTERS:
                is_cap = False
            if is_cap:
                run.append(w)
            elif run and w.lower() in _CONNECTORS:
                run.append(w)
            else:
                _flush(run, found)
        _flush(run, found)
    return found


def validate_rewrite(source: str, rewrite: str, allowed_terms: set[str],
                     tech_terms: set[str] | None = None) -> list[str]:
    """Violation codes for ``rewrite`` vs ``source``; empty list means valid."""
    terms = _default_terms() if tech_terms is None else tech_terms
    violations: set[str] = set()

    if _numbers(rewrite) - _numbers(source):
        violations.add("new_number")

    allowed = [a.casefold() for a in allowed_terms]
    rewrite_tech = _tech_in(rewrite, terms)
    for term in rewrite_tech:
        if has_phrase(source, term):
            continue
        if any(has_phrase(a, term) for a in allowed):
            continue
        violations.add(f"new_tech:{term}")

    source_l = source.casefold()
    for noun in _proper_nouns(rewrite, rewrite_tech):
        if noun not in source_l:
            violations.add(f"new_proper_noun:{noun}")

    n, m = len(source.strip()), len(rewrite.strip())
    if not (MIN_RATIO * n <= m <= MAX_RATIO * n):
        violations.add("length")
    return sorted(violations)
