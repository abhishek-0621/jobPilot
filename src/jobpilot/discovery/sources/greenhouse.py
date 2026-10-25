"""Greenhouse public job board API adapter."""

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
from jobpilot.profile.preferences import CompanyBoard

BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseSource:
    source = "greenhouse"

    def __init__(
        self,
        client: httpx.AsyncClient,
        min_interval_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = RateLimitedFetcher(client, min_interval_s, sleep, clock)

    async def list_postings(self, board: CompanyBoard) -> list[RawPosting]:
        data = await self._fetch.get_json(f"{BASE}/{board.token}/jobs?content=true")
        try:
            parsed = (_parse(j, board) for j in data["jobs"])
            return [p for p in parsed if p is not None]
        except (KeyError, TypeError, ValueError) as e:
            raise SourceError(ErrorCode.SOURCE_CHANGED, type(e).__name__) from e


def _parse(j: dict[str, Any], board: CompanyBoard) -> RawPosting | None:
    raw_url = require_http(j["absolute_url"])
    if raw_url is None:
        return None
    url = normalize_url(raw_url)
    loc = j.get("location") or {}
    updated = j.get("updated_at")
    return RawPosting(
        source="greenhouse",
        source_job_id=str(j["id"]),
        company=board.name,
        title=j["title"],
        location=loc.get("name") or None,
        description_html=html.unescape(j.get("content") or ""),
        canonical_url=url,
        application_url=url,
        posted_at=datetime.fromisoformat(updated).astimezone(UTC) if updated else None,
    )
