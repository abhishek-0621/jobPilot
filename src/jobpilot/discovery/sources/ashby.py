"""Ashby public job board API adapter."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.normalize import normalize_url
from jobpilot.discovery.sources.base import RateLimitedFetcher, RawPosting, SourceError
from jobpilot.profile.preferences import CompanyBoard

BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbySource:
    source = "ashby"

    def __init__(
        self,
        client: httpx.AsyncClient,
        min_interval_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = RateLimitedFetcher(client, min_interval_s, sleep, clock)

    async def list_postings(self, board: CompanyBoard) -> list[RawPosting]:
        data = await self._fetch.get_json(f"{BASE}/{board.token}?includeCompensation=true")
        try:
            return [_parse(j, board) for j in data["jobs"] if j.get("isListed") is not False]
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise SourceError(ErrorCode.SOURCE_CHANGED, type(e).__name__) from e


def _location(j: dict[str, Any]) -> str | None:
    parts = [j.get("location") or ""]
    parts += [s.get("location") or "" for s in j.get("secondaryLocations") or []]
    text = "; ".join(p for p in parts if p)
    if j.get("isRemote"):
        return f"{text} (Remote)" if text else "Remote"
    return text or None


def _parse(j: dict[str, Any], board: CompanyBoard) -> RawPosting:
    published = j.get("publishedAt")
    apply_url = j.get("applyUrl")
    return RawPosting(
        source="ashby",
        source_job_id=str(j["id"]),
        company=board.name,
        title=j["title"],
        location=_location(j),
        description_html=j.get("descriptionHtml") or "",
        canonical_url=normalize_url(j["jobUrl"]),
        application_url=normalize_url(apply_url) if apply_url else None,
        posted_at=datetime.fromisoformat(published).astimezone(UTC) if published else None,
    )
