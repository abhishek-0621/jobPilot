"""Shared source-adapter contract: raw postings, errors, rate-limited JSON fetch."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx

from jobpilot.contracts.enums import ErrorCode
from jobpilot.contracts.models import Contract
from jobpilot.profile.preferences import CompanyBoard

USER_AGENT = "JobPilot/0.1 (+personal job search; contact via repo)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
TIMEOUT_S = 20.0


class RawPosting(Contract):
    source: Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workday"]
    source_job_id: str
    company: str
    title: str
    location: str | None
    description_html: str
    canonical_url: str
    application_url: str | None
    posted_at: datetime | None


class SourceError(Exception):
    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code


class SourceAdapter(Protocol):
    source: str

    async def list_postings(self, board: CompanyBoard) -> list[RawPosting]: ...


class RateLimitedFetcher:
    """Sequential JSON GETs with at least ``min_interval_s`` between requests."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        min_interval_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def get_json(self, url: str) -> Any:
        async with self._lock:
            if self._last is not None:
                wait = self._min_interval_s - (self._clock() - self._last)
                if wait > 0:
                    await self._sleep(wait)
            self._last = self._clock()
            try:
                resp = await self._client.get(url, headers=HEADERS, timeout=TIMEOUT_S)
            except httpx.TimeoutException as e:
                raise SourceError(ErrorCode.SOURCE_TIMEOUT, type(e).__name__) from e
            except httpx.HTTPError as e:
                raise SourceError(ErrorCode.SOURCE_UNAVAILABLE, type(e).__name__) from e
        if resp.status_code == 429:
            raise SourceError(ErrorCode.SOURCE_RATE_LIMIT, "429")
        if resp.status_code != 200:
            raise SourceError(ErrorCode.SOURCE_UNAVAILABLE, str(resp.status_code))
        try:
            return json.loads(resp.text)
        except ValueError as e:
            raise SourceError(ErrorCode.SOURCE_CHANGED, "invalid json") from e
