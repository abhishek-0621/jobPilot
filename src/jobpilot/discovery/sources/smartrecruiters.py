"""SmartRecruiters public postings API adapter (list pages plus one detail call each)."""

import asyncio
import html
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.sources.base import RateLimitedFetcher, RawPosting, SourceError
from jobpilot.profile.preferences import CompanyBoard

BASE = "https://api.smartrecruiters.com/v1/companies"
PAGE = 100
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
log = logging.getLogger(__name__)
SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


class SmartRecruitersSource:
    source = "smartrecruiters"
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
        base = f"{BASE}/{board.token}/postings"
        items: list[dict[str, Any]] = []
        offset = 0
        invalid = 0
        try:
            while True:
                data = await self._fetch.get_json(f"{base}?limit={PAGE}&offset={offset}")
                total = int(data["totalFound"])
                page = data["content"]
                for item in page:
                    item["name"]  # shape check before any detail call
                    if not _ID.fullmatch(str(item["id"])):
                        invalid += 1
                        continue
                    items.append(item)
                offset += PAGE
                if not page or offset >= total or len(items) >= max_postings:
                    break
            items = items[:max_postings]
        except (KeyError, TypeError, ValueError) as e:
            raise SourceError(ErrorCode.SOURCE_CHANGED, type(e).__name__) from e
        if invalid:
            log.warning("smartrecruiters %s: skipped %d postings with invalid id",
                        board.token, invalid)
        out: list[RawPosting] = []
        for item in items:
            detail = await self._fetch.get_json(f"{base}/{item['id']}")
            try:
                out.append(_parse(item, detail, board))
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise SourceError(ErrorCode.SOURCE_CHANGED, type(e).__name__) from e
        return out


def _location(loc: dict[str, Any]) -> str | None:
    text = ", ".join(p for p in (loc.get("city"), loc.get("country")) if p)
    if loc.get("remote"):
        return f"{text} (Remote)" if text else "Remote"
    return text or None


def _description(detail: dict[str, Any]) -> str:
    sections = detail["jobAd"]["sections"]
    parts = []
    for key in SECTIONS:
        sec = sections.get(key)
        if sec and sec.get("text"):
            parts.append(f"<h3>{html.escape(str(sec.get('title', '')))}</h3>{sec['text']}")
    return "".join(parts)


def _parse(item: dict[str, Any], detail: dict[str, Any], board: CompanyBoard) -> RawPosting:
    url = f"https://jobs.smartrecruiters.com/{board.token}/{item['id']}"
    released = detail.get("releasedDate")
    return RawPosting(
        source="smartrecruiters",
        source_job_id=str(item["id"]),
        company=board.name,
        title=item["name"],
        location=_location(item.get("location") or {}),
        description_html=_description(detail),
        canonical_url=url,
        application_url=url,
        posted_at=datetime.fromisoformat(released).astimezone(UTC) if released else None,
    )
