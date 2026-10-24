"""Isolated, headful-by-default Chromium profile per portal with a request allowlist."""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route, WebSocketRoute, async_playwright

from jobpilot.config import AppConfig

_log = logging.getLogger(__name__)
_VIEWPORT = {"width": 1280, "height": 900}
_TIMEOUT_MS = 20_000
_ALWAYS_ALLOWED = ("data:", "blob:")

_CAPTCHA_SELECTOR = ", ".join([
    "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare']", ".g-recaptcha", ".h-captcha", "[data-sitekey]",
])
_BOT_CHECK = re.compile(r"verify you are human|are you a robot|checking your browser", re.I)
_MFA_TEXT = re.compile(r"verification code", re.I)


@dataclass
class PortalSession:
    page: Page
    blocked: list[str] = field(default_factory=list)


def host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    host = host.lower().rstrip(".")
    return bool(host) and any(
        host == a.lower() or host.endswith("." + a.lower()) for a in allowed_hosts)


@asynccontextmanager
async def portal_page(cfg: AppConfig, portal: str,
                      allowed_hosts: set[str]) -> AsyncIterator[PortalSession]:
    """Persistent per-portal context under storage/browser_state/<portal>; requests to
    hosts outside ``allowed_hosts`` are aborted and recorded in ``blocked``."""
    allowed_hosts = {h.strip() for h in allowed_hosts if h.strip()}
    user_dir = cfg.browser_state_dir / portal
    user_dir.mkdir(parents=True, exist_ok=True)
    blocked: list[str] = []

    async def guard(route: Route) -> None:
        url = route.request.url
        if url.startswith(_ALWAYS_ALLOWED) or host_allowed(urlsplit(url).hostname or "",
                                                           allowed_hosts):
            await route.continue_()
        else:
            blocked.append(url)
            await route.abort("blockedbyclient")

    async def guard_ws(ws: WebSocketRoute) -> None:
        if host_allowed(urlsplit(ws.url).hostname or "", allowed_hosts):
            ws.connect_to_server()
        else:
            blocked.append(ws.url)
            await ws.close()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_dir), headless=cfg.browser_headless, viewport=_VIEWPORT,
            service_workers="block")
        try:
            ctx.set_default_timeout(_TIMEOUT_MS)
            await ctx.route("**/*", guard)
            await ctx.route_web_socket("**/*", guard_ws)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            yield PortalSession(page=page, blocked=blocked)
        finally:
            _log.debug("portal %s: blocked %d requests", portal, len(blocked))
            await ctx.close()


async def _visible(page: Page, selector: str) -> bool:
    loc = page.locator(selector)
    return any([await loc.nth(i).is_visible() for i in range(await loc.count())])


async def detect_blockers(page: Page) -> list[str]:
    """Any of captcha, login, mfa, bot_check currently present on the page."""
    found: list[str] = []
    await page.wait_for_load_state("domcontentloaded")
    try:
        text = await page.evaluate("document.body ? document.body.innerText : ''")
    except PlaywrightError:
        text = ""
    if await page.locator(_CAPTCHA_SELECTOR).count():
        found.append("captcha")
    if await _visible(page, "input[type=password]"):
        found.append("login")
    if await _visible(page, "input[autocomplete=one-time-code]") or _MFA_TEXT.search(text):
        found.append("mfa")
    if _BOT_CHECK.search(text):
        found.append("bot_check")
    return found
