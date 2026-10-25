"""Shared plumbing for public remote-job board adapters (untrusted feed content)."""

import asyncio
import html
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.normalize import normalize_url, require_http
from jobpilot.discovery.sources.base import RateLimitedFetcher, RawPosting, SourceError

MAX_DESCRIPTION_CHARS = 200_000  # raw HTML; html_to_text caps the stored text at 12 000
MAX_TITLE_CHARS = 300
MAX_COMPANY_CHARS = 200
MAX_LOCATION_CHARS = 500
MAX_ID_CHARS = 500
SHAPE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, OverflowError)


class RemoteBoardBase:
    source = ""
    kind = "remote_board"
    supports_max_postings = True

    def __init__(
        self,
        client: httpx.AsyncClient,
        min_interval_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = RateLimitedFetcher(client, min_interval_s, sleep, clock)

    async def list_board(self, max_postings: int) -> list[RawPosting]:
        raise NotImplementedError


def changed(e: Exception) -> SourceError:
    return SourceError(ErrorCode.SOURCE_CHANGED, type(e).__name__)


def as_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return value


def as_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return value


def plain(value: Any, limit: int) -> str:
    """A short single-line field (title, company, location) from untrusted feed text."""
    text = " ".join(html.unescape(str(value or "")).split())
    return text[:limit]


def parse_date(value: Any) -> datetime | None:
    """ISO 8601 string (naive means UTC), or epoch seconds; None when absent/unparseable."""
    if value in (None, ""):
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def posting(source: str, *, job_id: Any, company: Any, title: Any, location: Any,
            description: Any, url: Any, apply_url: Any = None,
            posted_at: Any = None) -> RawPosting | None:
    """A RawPosting from one feed item, or None if it lacks an http(s) URL, id or title."""
    raw = require_http(str(url) if url else None)
    ident = str(job_id or "").strip()[:MAX_ID_CHARS]
    title_s = plain(title, MAX_TITLE_CHARS)
    if raw is None or not ident or not title_s:
        return None
    canonical = normalize_url(raw)
    applied = require_http(str(apply_url) if apply_url else None)
    loc = plain(location, MAX_LOCATION_CHARS) or None
    return RawPosting(
        source=source,  # type: ignore[arg-type]
        source_job_id=ident,
        company=plain(company, MAX_COMPANY_CHARS) or "Unknown company",
        title=title_s,
        location=loc,
        description_html=str(description or "")[:MAX_DESCRIPTION_CHARS],
        canonical_url=canonical,
        application_url=normalize_url(applied) if applied else canonical,
        posted_at=parse_date(posted_at),
    )
