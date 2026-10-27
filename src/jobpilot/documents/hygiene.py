"""Deterministic text hygiene for everything JobPilot writes for the user.

``clean_text`` strips invisible/format Unicode and normalises typographic punctuation
(smart quotes, em/en dashes, ellipsis) to plain ASCII, leaving URLs, emails and inline
code untouched. ``text_issues`` flags stock "AI voice" phrasing, markdown and (where
asked) exclamation marks so agents can retry once and then fall back. No network."""

import re
from functools import cache
from pathlib import Path

PHRASES_PATH = Path(__file__).resolve().parents[3] / "config" / "ai_stock_phrases.txt"

_INVISIBLE = re.compile(
    "[­​-‍⁠-⁤﻿‪-‮⁦-⁩"
    "︀-️\U000e0000-\U000e007f\U000e0100-\U000e01ef]+")
_PROTECT = re.compile(
    r"`[^`\n]*`"                                  # inline code
    r"|\b(?:https?|ftp)://[^\s<>\"']+"           # URLs
    r"|\bwww\.[^\s<>\"']+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+")             # emails
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'",
                         "′": "'", "“": '"', "”": '"', "„": '"',
                         "‟": '"', "″": '"', "…": "..."})
_DASH = re.compile("[ \t]*[–—―]+[ \t]*")
_EN = "–"
_PUNCT_BEFORE = set(",;:.!?([")


def _dash(m: re.Match[str]) -> str:
    s, i, j = m.string, m.start(), m.end()
    left = s[i - 1] if i > 0 else ""
    right = s[j] if j < len(s) else ""
    spaced = m.group() != m.group().strip(" \t")
    if left.isdigit() and right.isdigit():
        return "-"                                 # 2019-2021, 10-20
    if left in ("", "\n"):
        return "- " if right not in ("", "\n") else ""   # dash used as a list bullet
    if right in ("", "\n"):
        return ""                                  # trailing dash
    if left.isdigit() and spaced:
        return " - "                               # "2021 - Present"
    if left in _PUNCT_BEFORE:
        return " "
    if right in ",;:.!?)]":
        return ""
    if _EN in m.group() and not spaced:
        return "-"                                 # compounds: New York-London
    return ", "                                    # clause joiner in prose


def _clean_segment(s: str) -> str:
    s = _INVISIBLE.sub("\x01", s)
    # a removed character between two spaces leaves one space, otherwise none
    s = re.sub(r"( ?)\x01+( ?)", lambda m: " " if m.group(1) and m.group(2)
               else m.group(1) + m.group(2), s)
    s = s.translate(_QUOTES)
    return _DASH.sub(_dash, s)


def clean_text(s: str) -> str:
    """Idempotent Unicode/punctuation clean; layout spaces such as NBSP are kept."""
    if not s:
        return s
    kept: list[str] = []

    def hold(m: re.Match[str]) -> str:
        kept.append(m.group())
        return f"\x02{len(kept) - 1}\x03"

    out = _clean_segment(_PROTECT.sub(hold, s))
    return re.sub(r"\x02(\d+)\x03", lambda m: kept[int(m.group(1))], out)


def _phrase_lines(path: Path) -> tuple[str, ...]:
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return tuple(dict.fromkeys(ln.casefold() for ln in lines if ln and not ln.startswith("#")))


@cache
def _patterns(path: Path) -> tuple[tuple[str, re.Pattern[str]], ...]:
    out = []
    for p in _phrase_lines(path):
        body = r"\s+".join(re.escape(w).replace("'", "['’]") for w in p.split())
        out.append((p, re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)))
    return tuple(out)


def stock_phrases(s: str, path: Path = PHRASES_PATH) -> list[str]:
    """Stock phrases from ``config/ai_stock_phrases.txt`` found in ``s`` (file order)."""
    return [p for p, rx in _patterns(path) if rx.search(s)]


_MARKDOWN = re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__|(?m:^[ \t]*#{1,6}[ \t]+\S)")


def text_issues(s: str, *, no_exclamation: bool = False,
                path: Path = PHRASES_PATH) -> list[str]:
    """Rejection codes for generated plain text: ``stock_phrase:<p>``, ``markdown``,
    ``exclamation`` (only when ``no_exclamation``) and ``dash`` (an em/en dash that
    survived; never after ``clean_text``)."""
    codes = [f"stock_phrase:{p}" for p in stock_phrases(s, path)]
    if _MARKDOWN.search(s):
        codes.append("markdown")
    if no_exclamation and "!" in s:
        codes.append("exclamation")
    if re.search("[–—―]", s):
        codes.append("dash")
    return codes


def avoid_note(codes: list[str]) -> str:
    """Extra retry instruction naming the stock phrases to avoid ("" when none)."""
    phrases = [c.split(":", 1)[1] for c in codes if c.startswith("stock_phrase:")]
    parts = []
    if phrases:
        parts.append("Do not use these words or phrases: " + ", ".join(phrases) + ".")
    if "markdown" in codes:
        parts.append("Write plain text with no markdown (no ** or # headers).")
    if "exclamation" in codes:
        parts.append("Do not use exclamation marks.")
    return (" " + " ".join(parts)) if parts else ""
