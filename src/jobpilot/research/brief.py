"""Company brief items: model-extracted from untrusted page text, then grounded by code.

An item survives only when its ``source_url`` is one of the fetched sources, its name
appears verbatim (case-insensitive) in that source's text, and every number and proper
noun of its one-line description also appears there. A stated date must appear in the
source too. Items are sorted recent first (dated within ``RECENT_MONTHS`` first, then
undated, then older) and capped at ``MAX_ITEMS``.
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from jobpilot.documents.hygiene import clean_text
from jobpilot.documents.validate import _numbers, _proper_nouns
from jobpilot.outreach.state import company_key as _company_key
from jobpilot.util.hashing import canonical_json, content_hash
from jobpilot.util.textmatch import has_phrase

MAX_ITEMS = 8
RECENT_MONTHS = 18
FRESH_DAYS = 14
MAX_NAME = 80
MAX_LINE = 200
ItemKind = Literal["product", "project", "research", "initiative", "mission", "value"]

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
    start=1)}
_ISO_RE = re.compile(r"\b(20\d{2})-(\d{1,2})(?:-\d{1,2})?\b")
_MONTH_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(?:\d{1,2},?\s+)?(20\d{2})\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_ITEM_WORDS = frozenset({"the", "a", "an", "our", "its", "their", "we", "it", "this",
                         "with", "for", "and", "new", "now", "today"})


class BriefItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ItemKind
    name: str = Field(min_length=1, max_length=MAX_NAME)
    one_line: str = Field(min_length=1, max_length=MAX_LINE)
    date_if_stated: str | None = None
    source_url: str


class BriefSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str
    title: str = ""
    fetched_at: datetime


def company_key(company: str | None) -> str | None:
    """Normalized cache key ("Acme, Inc." == "acme"); None when there is no name."""
    if not company or not company.strip():
        return None
    key = re.sub(r"[^a-z0-9]", "", _company_key(company))[:64]
    return key or None


def brief_hash(items: list[BriefItem]) -> str:
    return content_hash(canonical_json([i.model_dump(mode="json") for i in items]))[-16:] \
        if items else "none"


def parse_date(text: str | None) -> tuple[int, int] | None:
    """(year, month) of a stated date; month 0 when only the year is known."""
    if not text:
        return None
    if m := _ISO_RE.search(text):
        month = int(m.group(2))
        return int(m.group(1)), month if 1 <= month <= 12 else 0
    if (m := _MONTH_RE.search(text)) and m.group(1).casefold() in _MONTHS:
        return int(m.group(2)), _MONTHS[m.group(1).casefold()]
    if m := _YEAR_RE.search(text):
        return int(m.group(1)), 0
    return None


def _months_ago(d: tuple[int, int], now: datetime) -> int:
    year, month = d
    return (now.year - year) * 12 + (now.month - (month or 12))


def _grounded_line(line: str, page: str) -> bool:
    if _numbers(line) - _numbers(page):
        return False
    phrases, singles = _proper_nouns(line)
    return all(has_phrase(page, n) for n in phrases | singles
               if n.casefold() not in _ITEM_WORDS)


def ground_items(raw: list[dict], pages: dict[str, str], now: datetime) -> list[BriefItem]:
    """Grounded, de-duplicated, recent-first items (see the module docstring)."""
    kept: list[tuple[int, int, int, BriefItem]] = []
    seen: set[str] = set()
    for pos, r in enumerate(raw):
        try:
            item = BriefItem.model_validate({
                k: (clean_text(" ".join(v.split())) if isinstance(v, str) else v)
                for k, v in r.items()})
        except ValueError:
            continue
        page = pages.get(item.source_url)
        name_key = item.name.casefold()
        if page is None or name_key in seen or len(item.name) < 2:
            continue
        if not has_phrase(page, item.name) or not _grounded_line(item.one_line, page):
            continue
        date = item.date_if_stated
        if date and not has_phrase(page, date):
            item = item.model_copy(update={"date_if_stated": None})
        when = parse_date(item.date_if_stated)
        if when is None:
            bucket, age = 1, 0
        else:
            age = _months_ago(when, now)
            bucket = 0 if age <= RECENT_MONTHS else 2
        seen.add(name_key)
        kept.append((bucket, age, pos, item))
    kept.sort(key=lambda t: t[:3])
    return [t[3] for t in kept][:MAX_ITEMS]
