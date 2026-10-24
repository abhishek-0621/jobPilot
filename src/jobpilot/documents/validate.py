"""Deterministic guard against invented claims in LLM-rewritten resume bullets."""

import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from jobpilot.util.textmatch import has_phrase

_CONFIG = Path(__file__).resolve().parents[3] / "config"
TECH_TERMS_PATH = _CONFIG / "tech_terms.txt"
CASE_SENSITIVE_TERMS_PATH = _CONFIG / "tech_terms_case_sensitive.txt"

MIN_RATIO = 0.3
MAX_RATIO = 1.6

_SENTENCE_STARTERS = {
    "built", "led", "designed", "developed", "shipped", "owned", "cut", "rebuilt", "run",
    "partner", "architected", "evaluated", "improved", "reduced", "created", "delivered",
    "migrated", "automated", "implemented", "spearheaded", "optimized", "optimised",
    "streamlined", "scaled", "launched", "engineered", "integrated", "collaborated",
    "mentored", "managed", "drove", "established", "analyzed", "analysed", "authored",
    "enhanced", "accelerated", "refactored", "deployed", "wrote", "maintained",
    "coordinated", "conducted", "executed", "generated", "increased", "decreased", "halved",
    "doubled", "tripled", "boosted", "trained", "tested", "validated", "instrumented",
    "introduced", "modernized", "modernised", "monitored", "prototyped", "researched",
    "resolved", "secured", "simplified", "standardized", "supported", "transformed",
    "unified", "worked", "contributed", "documented", "reviewed", "defined", "fixed",
    "replaced", "extended", "expanded", "operated", "orchestrated", "pioneered", "leveraged",
    "applied", "advised", "set", "ran", "rewrote", "grew", "won", "made", "took", "taught",
    "began", "oversaw", "drafted",
}
_FUNCTION_WORDS = {"the", "a", "an", "and", "with", "using", "across", "for", "to", "in",
                   "on", "by", "of", "at", "from", "as", "while", "through", "via", "into"}
_CONNECTORS = {"of", "and", "&", "for"}

CLAIM_PHRASES = (
    "led", "lead", "leading", "leadership", "managed", "manage", "managing", "manager",
    "mentored", "mentoring", "mentor", "head of", "headed", "spearheaded", "spearheading",
    "owned", "ownership", "team of", "directed", "supervised", "architected",
)

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000, "million": 10**6,
    "billion": 10**9, "dozen": 12,
}
_MULT_WORDS = {
    "double": 2, "doubled": 2, "doubles": 2, "doubling": 2, "triple": 3, "tripled": 3,
    "tripling": 3, "quadruple": 4, "quadrupled": 4, "halved": 0.5, "halving": 0.5, "half": 0.5,
}


def _alt(words: dict[str, float]) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<d>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    rf"|(?<![A-Za-z])(?P<w>{_alt(_NUM_WORDS)})(?=-?fold\b|(?![A-Za-z]))"
    rf"|(?<![A-Za-z])(?P<m>{_alt(_MULT_WORDS)})(?![A-Za-z])",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(
    r"(?P<pct>\s*(?:%|percent\b|per cent\b))|(?P<mul>\s*[x×](?![A-Za-z0-9])|-?fold\b|\s+times\b)",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:['’][A-Za-z]+)?|&")


def load_tech_terms(path: Path | str) -> set[str]:
    """One term per line; blank lines and ``#`` comments ignored; lower-cased."""
    return {t.casefold() for t in load_case_sensitive_terms(path)}


def load_case_sensitive_terms(path: Path | str) -> set[str]:
    """Like ``load_tech_terms`` but keeps the canonical capitalization."""
    terms: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.add(line)
    return terms


@lru_cache(maxsize=1)
def _default_terms() -> frozenset[str]:
    return frozenset(load_tech_terms(TECH_TERMS_PATH))


@lru_cache(maxsize=1)
def _default_cs_terms() -> frozenset[str]:
    return frozenset(load_case_sensitive_terms(CASE_SENSITIVE_TERMS_PATH))


def _cs_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w&-]){re.escape(term)}(?![\w&+#-])")


def _cs_has(text: str, term: str) -> bool:
    return _cs_pattern(term).search(text) is not None


def _canon(value: float | Decimal) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _numbers(text: str) -> set[str]:
    """Canonical (unit class, value) tokens. Classes: percent, multiplier, plain."""
    out: set[str] = set()
    for m in _NUMBER_RE.finditer(text):
        if m.group("m"):
            out.add(f"mul:{_canon(_MULT_WORDS[m.group('m').lower()])}")
            continue
        if m.group("d"):
            value = _canon(Decimal(m.group("d").replace(",", "")))
        else:
            value = _canon(_NUM_WORDS[m.group("w").lower()])
        suffix = _SUFFIX_RE.match(text, m.end())
        unit = "pct" if suffix and suffix.group("pct") else "mul" if suffix else "num"
        out.add(f"{unit}:{value}")
    return out


def _strip_terms(text: str, terms: set[str], cs_terms: set[str]) -> str:
    for t in sorted(terms, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(t)}(?!\w)", " tech ", text, flags=re.IGNORECASE)
    for t in sorted(cs_terms, key=len, reverse=True):
        text = _cs_pattern(t).sub(" tech ", text)
    return text


def _flush(run: list[str], phrases: set[str]) -> None:
    while run and run[-1].lower() in _CONNECTORS:
        run.pop()
    if sum(1 for w in run if w.lower() not in _CONNECTORS) >= 2:
        phrases.add(" ".join(run))
    run.clear()


def _proper_nouns(text: str) -> tuple[set[str], set[str]]:
    """(multi-word capitalized phrases, single capitalized words) in already-stripped text."""
    phrases: set[str] = set()
    singles: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        run: list[str] = []
        for i, raw in enumerate(_WORD_RE.findall(sentence)):
            w = re.sub(r"['’]s$", "", raw)
            low = w.lower()
            is_cap = w[0].isupper() and low != "tech"
            if i == 0 and (low in _SENTENCE_STARTERS or low in _FUNCTION_WORDS
                           or (len(low) > 4 and low.endswith("ed"))):
                is_cap = False
            if is_cap:
                run.append(w)
                if len(w) > 1:
                    singles.add(w)
            elif run and low in _CONNECTORS:
                run.append(w)
            else:
                _flush(run, phrases)
        _flush(run, phrases)
    return phrases, singles


def validate_rewrite(source: str, rewrite: str, allowed_terms: set[str],
                     tech_terms: set[str] | None = None,
                     case_sensitive_terms: set[str] | None = None) -> list[str]:
    """Violation codes for ``rewrite`` vs ``source``; empty list means valid."""
    terms = _default_terms() if tech_terms is None else tech_terms
    cs_terms = _default_cs_terms() if case_sensitive_terms is None else case_sensitive_terms
    allowed = list(allowed_terms)
    allowed_cf = [a.casefold() for a in allowed]
    violations: set[str] = set()

    if _numbers(rewrite) - _numbers(source):
        violations.add("new_number")

    for term in terms:
        if has_phrase(rewrite, term) and not has_phrase(source, term) \
                and not any(has_phrase(a, term) for a in allowed_cf):
            violations.add(f"new_tech:{term}")
    for term in cs_terms:
        if _cs_has(rewrite, term) and not _cs_has(source, term) \
                and not any(_cs_has(a, term) for a in allowed):
            violations.add(f"new_tech:{term}")

    for phrase in CLAIM_PHRASES:
        if has_phrase(rewrite, phrase) and not has_phrase(source, phrase):
            violations.add(f"new_claim:{phrase}")

    phrases, singles = _proper_nouns(_strip_terms(rewrite, terms, cs_terms))
    for noun in phrases | singles:
        if not has_phrase(source, noun) and not any(has_phrase(a, noun) for a in allowed_cf):
            violations.add(f"new_proper_noun:{noun.casefold()}")

    n, m = len(source.strip()), len(rewrite.strip())
    if not (MIN_RATIO * n <= m <= MAX_RATIO * n):
        violations.add("length")
    return sorted(violations)
