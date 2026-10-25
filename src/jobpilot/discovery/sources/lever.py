"""Lever public postings API adapter."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.normalize import normalize_url, require_http
from jobpilot.discovery.sources.base import RateLimitedFetcher, RawPosting, SourceError
from jobpilot.profile.preferences import CompanyBoard

BASE = "https://api.lever.co/v0/postings"


class LeverSource:
    source = "lever"
    supports_max_postings = False

    def __init__(
        self,
        client: httpx.AsyncClient,
        min_interval_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = RateLimitedFetcher(client, min_interval_s, sleep, clock)

    async def list_postings(self, board: CompanyBoard) -> list[RawPosting]:
        data = await self._fetch.get_json(f"{BASE}/{board.token}?mode=json")
        try:
            if not isinstance(data, list):
                raise TypeError("expected list")
            parsed = [_parse(p, board) for p in data]
            return [p for p in parsed if p is not None]
        except (KeyError, TypeError, ValueError) as e:
            raise SourceError(ErrorCode.SOURCE_CHANGED, type(e).__name__) from e


def _parse(p: dict[str, Any], board: CompanyBoard) -> RawPosting | None:
    parts = [p.get("description") or ""]
    for item in p.get("lists") or []:
        parts.append(f"<h3>{item.get('text', '')}</h3><ul>{item.get('content', '')}</ul>")
    parts.append(p.get("additional") or "")
    hosted = require_http(p["hostedUrl"])
    if hosted is None:
        return None
    apply_url = require_http(p.get("applyUrl"))
    created = p.get("createdAt")
    return RawPosting(
        source="lever",
        source_job_id=str(p["id"]),
        company=board.name,
        title=p["text"],
        location=(p.get("categories") or {}).get("location") or None,
        description_html="".join(parts),
        canonical_url=normalize_url(hosted),
        application_url=normalize_url(apply_url) if apply_url else None,
        posted_at=datetime.fromtimestamp(created / 1000, tz=UTC) if created else None,
    )
