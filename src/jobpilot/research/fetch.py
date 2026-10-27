"""Polite, public-only page fetches for company research.

Every URL must be http(s) on a public host name (no IP literal, no localhost), never a
deny-listed host (LinkedIn, Glassdoor, Crunchbase, ATS and other login-walled or third-
party sites), and must resolve only to public addresses (the production client also pins
the connection to the checked address). Redirects are followed by hand, at most
``MAX_REDIRECTS`` hops and only within the same registrable domain. Bodies are streamed
and capped at ``MAX_BYTES``; at least ``MIN_INTERVAL_S`` passes between two requests to
the same host; robots.txt is honoured (fetched once per host). Page text is untrusted.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from jobpilot.discovery.sources.base import USER_AGENT
from jobpilot.outreach import contacts

MAX_BYTES = 2_000_000
ROBOTS_MAX_BYTES = 200_000
MAX_PAGES = 6
MAX_ATTEMPTS = 14
MAX_REDIRECTS = 3
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 20.0
NEWS_PATHS = ("blog", "news", "newsroom", "press", "research", "engineering", "company",
              "about")
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"}
_HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_REDIRECTS = (301, 302, 303, 307, 308)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Login-walled, aggregator or third-party hosts research never contacts (plus every ATS /
# job-board / platform host the outreach module already refuses as "not the company").
DENY_HOSTS = contacts.NON_COMPANY_HOSTS + (
    "crunchbase.com", "pitchbook.com", "zoominfo.com", "owler.com", "craft.co",
    "ambitionbox.com", "comparably.com", "levels.fyi", "blind.com", "teamblind.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "threads.net",
    "tiktok.com", "youtube.com", "reddit.com", "quora.com", "medium.com", "substack.com",
    "wikipedia.org", "bloomberg.com", "tracxn.com", "builtin.com", "lnkd.in",
)


def denied(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in DENY_HOSTS)


def safe_url(url: str | None) -> str | None:
    """The URL when research may fetch it: public http(s) host name, not deny-listed."""
    url = contacts.public_http_url(url)
    if url is None or any(c.isspace() for c in url) or len(url) > 500:
        return None
    return None if denied(urlsplit(url).hostname or "") else url


def page_title(html: str) -> str:
    m = _TITLE_RE.search(html[:200_000])
    if m is None:
        return ""
    return " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())[:120]


@dataclass(frozen=True)
class Page:
    url: str
    html: str
    title: str


Sleep = Callable[[float], Awaitable[None]]


@dataclass
class ResearchFetcher:
    client: httpx.AsyncClient
    resolve: contacts.Resolver | None = None
    sleep: Sleep = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    min_interval_s: float = MIN_INTERVAL_S
    _last: dict[str, float] = field(default_factory=dict)
    _robots: dict[str, RobotFileParser | None] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)

    async def _allowed_host(self, url: str) -> bool:
        if safe_url(url) is None:
            return False
        host = urlsplit(url).hostname or ""
        return await contacts.resolves_public(host, self.resolve or contacts.resolve_host)

    async def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval_s - (self.clock() - last)
            if wait > 0:
                await self.sleep(wait)
        self._last[host] = self.clock()

    async def _get(self, url: str, cap: int) -> tuple[int, str | None, bytes, str]:
        """(status, location, body<=cap, content type) of one GET, no redirects."""
        await self._wait((urlsplit(url).hostname or "").lower())
        self.requests.append(url)
        async with self.client.stream("GET", url, headers=HEADERS, timeout=TIMEOUT_S,
                                      follow_redirects=False) as resp:
            if resp.status_code != 200:
                return resp.status_code, resp.headers.get("location"), b"", ""
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) >= cap:
                    break
            return 200, None, bytes(body[:cap]), resp.headers.get("content-type", "")

    async def _robots_ok(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None = None
            try:
                status, _loc, body, _ct = await self._get(origin + "/robots.txt",
                                                          ROBOTS_MAX_BYTES)
            except httpx.HTTPError:
                status, body = 0, b""
            if status in (401, 403):
                parser = RobotFileParser()
                parser.disallow_all = True
            elif status == 200:
                parser = RobotFileParser()
                parser.parse(body.decode("utf-8", "replace").splitlines())
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    async def fetch(self, url: str) -> Page | None:
        """One public page (following same-domain redirects), or None on any refusal or
        error. Never raises for network problems."""
        start = safe_url(url)
        if start is None:
            return None
        base = contacts.base_domain(urlsplit(start).hostname or "")
        current = start
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                if not await self._allowed_host(current) or not await self._robots_ok(current):
                    return None
                status, location, body, ctype = await self._get(current, MAX_BYTES)
                if status in _REDIRECTS and location:
                    nxt = safe_url(urljoin(current, location))
                    if nxt is None or contacts.base_domain(
                            urlsplit(nxt).hostname or "") != base:
                        return None
                    current = nxt
                    continue
                mime = ctype.split(";")[0].strip().lower()
                if status != 200 or (mime and mime not in _HTML_TYPES):
                    return None
                html = body.decode("utf-8", "replace")
                return Page(url=current, html=html, title=page_title(html))
        except (httpx.HTTPError, UnicodeError, OSError):
            return None
        return None


def candidate_urls(domain: str | None, research_urls: list[str]) -> list[str]:
    """User-provided research URLs first, then the company homepage and its likely news
    pages. De-duplicated; unsafe URLs dropped."""
    out: list[str] = [u for u in research_urls if safe_url(u)]
    if domain:
        root = f"https://{domain}"
        out += [root + "/"] + [f"{root}/{p}" for p in NEWS_PATHS]
    return [u for u in dict.fromkeys(out) if safe_url(u)]


async def fetch_pages(fetcher: ResearchFetcher, urls: list[str],
                      max_pages: int = MAX_PAGES) -> list[Page]:
    """Fetch in order until ``max_pages`` pages succeeded (at most MAX_ATTEMPTS tries)."""
    pages: list[Page] = []
    seen: set[str] = set()
    for url in urls[:MAX_ATTEMPTS]:
        if len(pages) >= max_pages:
            break
        page = await fetcher.fetch(url)
        if page is not None and page.url not in seen:
            seen.add(page.url)
            pages.append(page)
    return pages
