"""Find which public job boards a company has, by probing slug candidates (live postings only)."""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

import httpx

from jobpilot.discovery.sources.base import RateLimitedFetcher, SourceError
from jobpilot.profile.preferences import CompanyBoard

SUFFIXES = ("inc", "labs", "ai", "technologies", "india")
MAX_CANDIDATES = 6
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SOURCES = ("greenhouse", "lever", "ashby", "smartrecruiters")
_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
}


class BoardHit(NamedTuple):
    board: CompanyBoard
    postings: int


def slug_candidates(name: str) -> list[str]:
    """Up to 6 lowercase slugs: words joined by '', '-' or '_', with and without suffix words."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    stripped = list(words)
    while len(stripped) > 1 and stripped[-1] in SUFFIXES:
        stripped.pop()
    out: list[str] = []
    for joiner in ("", "-", "_"):
        for ws in (words, stripped):
            slug = joiner.join(ws)
            if slug and _TOKEN.fullmatch(slug) and slug not in out:
                out.append(slug)
    return out[:MAX_CANDIDATES]


def _count(source: str, data: Any) -> int:
    """Number of postings in a probe response; 0 if the shape is not a live board."""
    try:
        if source == "lever":
            return len(data) if isinstance(data, list) else 0
        if source == "smartrecruiters":
            return max(int(data["totalFound"]), 0)
        return len(data["jobs"])
    except (KeyError, TypeError, ValueError):
        return 0


async def _probe_source(source: str, name: str, slugs: list[str],
                        fetch: RateLimitedFetcher) -> BoardHit | None:
    for slug in slugs:
        try:
            data = await fetch.get_json(_URLS[source].format(slug=slug))
        except SourceError:  # 404, rate limit, network error: all count as a miss
            continue
        n = _count(source, data)
        if n > 0:
            board = CompanyBoard(name=name, source=source, token=slug)  # type: ignore[arg-type]
            return BoardHit(board, n)
    return None


async def find_hits(
    names: list[str],
    client: httpx.AsyncClient,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[BoardHit]:
    """Probe every name; one rate limiter per host (>= 1.0 s) is shared across all names.
    Hosts are probed concurrently; a source stops at its first slug that has postings."""
    fetchers = {s: RateLimitedFetcher(client, 1.0, sleep, clock) for s in SOURCES}
    hits: list[BoardHit] = []
    for name in names:
        slugs = slug_candidates(name)
        found = await asyncio.gather(*(
            _probe_source(s, name.strip(), slugs, fetchers[s]) for s in SOURCES))
        hits.extend(h for h in found if h is not None)
    return hits


async def find_boards(
    name: str,
    client: httpx.AsyncClient,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[CompanyBoard]:
    """Boards with at least one live posting, ordered by source. Never raises on network
    errors. Workday is not probed: tenants are added by hand."""
    hits = await find_hits([name], client, sleep=sleep, clock=clock)
    return [h.board for h in hits]
