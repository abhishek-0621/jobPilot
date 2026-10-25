"""Workday public CXS job-site adapter (POST list pages plus one detail GET each)."""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.normalize import normalize_url, require_http
from jobpilot.discovery.sources.base import RateLimitedFetcher, RawPosting, SourceError
from jobpilot.profile.preferences import CompanyBoard

log = logging.getLogger(__name__)

PAGE = 20
MAX_PAGES = 5
_EXTERNAL_PATH = re.compile(r"^/job/[A-Za-z0-9_./%-]+$")
_ENCODED_SEPARATOR = re.compile(r"%2[ef]", re.IGNORECASE)


class WorkdaySource:
    source = "workday"
    supports_max_postings = True

    def __init__(
        self,
        client: httpx.AsyncClient,
        min_interval_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = RateLimitedFetcher(client, min_interval_s, sleep, clock)

    async def list_postings(self, board: CompanyBoard, max_postings: int = 100) -> list[RawPosting]:
        host = f"https://{board.token}.{board.extra['wd_host']}.myworkdayjobs.com"
        site = board.extra["site"]
        api = f"{host}/wday/cxs/{board.token}/{site}"
        items: list[dict[str, Any]] = []
        rejected = 0
        try:
            for page in range(MAX_PAGES):
                data = await self._fetch.post_json(f"{api}/jobs", {
                    "appliedFacets": {}, "limit": PAGE, "offset": page * PAGE,
                    "searchText": ""})
                postings = data["jobPostings"]
                for item in postings:
                    item["title"]  # shape check before any detail call
                    path = item.get("externalPath")
                    if not isinstance(path, str) or not _EXTERNAL_PATH.fullmatch(path) \
                            or ".." in path \
                            or _ENCODED_SEPARATOR.search(path):
                        rejected += 1
                        continue
                    items.append(item)
                if len(postings) < PAGE or len(items) >= max_postings:
                    break
            items = items[:max_postings]
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise SourceError(ErrorCode.SOURCE_CHANGED, type(e).__name__) from e
        if rejected:
            log.warning("workday %s: skipped %d postings with invalid externalPath",
                        board.token, rejected)
        out: list[RawPosting] = []
        for item in items:
            detail = await self._fetch.get_json(f"{api}{item['externalPath']}")
            try:
                posting = _parse(item, detail, board, host, site)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise SourceError(ErrorCode.SOURCE_CHANGED, type(e).__name__) from e
            if posting is not None:
                out.append(posting)
        return out


def _parse(item: dict[str, Any], detail: dict[str, Any], board: CompanyBoard, host: str,
           site: str) -> RawPosting | None:
    info = detail["jobPostingInfo"]
    url = normalize_url(
        require_http(info.get("externalUrl")) or f"{host}/{site}{item['externalPath']}")
    location = info.get("location") or item.get("locationsText") or None
    if location and "Remote" in str(info.get("remoteType") or ""):
        location = f"{location} (Remote)"
    start = info.get("startDate")
    return RawPosting(
        source="workday",
        source_job_id=str(info["jobReqId"]),
        company=board.name,
        title=item["title"],
        location=location,
        description_html=info["jobDescription"],
        canonical_url=url,
        application_url=url,
        posted_at=datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC) if start else None,
    )
