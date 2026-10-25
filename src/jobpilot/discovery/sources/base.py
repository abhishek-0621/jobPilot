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
HTML_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html"}
MAX_TEXT_BYTES = 2_000_000
TIMEOUT_S = 20.0
# Public remote-job boards (one feed each, not per company); always the manual apply path.
REMOTE_BOARDS = ("himalayas", "remotive", "weworkremotely", "remoteok", "jobicy",
                 "workingnomads")
RemoteBoardName = Literal["himalayas", "remotive", "weworkremotely", "remoteok", "jobicy",
                          "workingnomads"]


class RawPosting(Contract):
    source: Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workday",
                    "himalayas", "remotive", "weworkremotely", "remoteok", "jobicy",
                    "workingnomads"]
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
    # True when list_postings accepts ``max_postings`` (the finder passes the run cap).
    supports_max_postings: bool

    async def list_postings(self, board: CompanyBoard) -> list[RawPosting]: ...


class RateLimitedFetcher:
    """Sequential JSON GET/POST requests with at least ``min_interval_s`` between requests."""

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
        return await self._request("GET", url, None)

    async def post_json(self, url: str, body: dict[str, Any]) -> Any:
        return await self._request("POST", url, body)

    async def get_text(self, url: str) -> str:
        """One rate-limited GET of a public page (no redirects followed); the body is
        capped at MAX_TEXT_BYTES."""
        text: str = await self._request("GET", url, None, as_text=True)
        return text

    async def _request(self, method: str, url: str, body: dict[str, Any] | None,
                       as_text: bool = False) -> Any:
        async with self._lock:
            if self._last is not None:
                wait = self._min_interval_s - (self._clock() - self._last)
                if wait > 0:
                    await self._sleep(wait)
            self._last = self._clock()
            try:
                if as_text:
                    resp = await self._client.get(url, headers=HTML_HEADERS,
                                                  timeout=TIMEOUT_S, follow_redirects=False)
                elif body is None:
                    resp = await self._client.get(url, headers=HEADERS, timeout=TIMEOUT_S)
                else:
                    resp = await self._client.post(url, json=body, headers=HEADERS,
                                                   timeout=TIMEOUT_S)
            except httpx.TimeoutException as e:
                raise SourceError(ErrorCode.SOURCE_TIMEOUT, type(e).__name__) from e
            except httpx.HTTPError as e:
                raise SourceError(ErrorCode.SOURCE_UNAVAILABLE, type(e).__name__) from e
        if resp.status_code == 429:
            raise SourceError(ErrorCode.SOURCE_RATE_LIMIT, "429")
        if resp.status_code != 200:
            raise SourceError(ErrorCode.SOURCE_UNAVAILABLE, str(resp.status_code))
        if as_text:
            return resp.content[:MAX_TEXT_BYTES].decode(resp.encoding or "utf-8", "replace")
        try:
            return json.loads(resp.text)
        except ValueError as e:
            raise SourceError(ErrorCode.SOURCE_CHANGED, "invalid json") from e
