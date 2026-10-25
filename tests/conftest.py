import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from sqlalchemy.orm import Session

from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    db_path = tmp_path / "t.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    session = make_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hard guard: no browser in any test may reach a non-loopback host.

    * portal_page's allowlist is intersected with loopback (its guard calls host_allowed);
    * every persistent context / new page gets a context route that aborts non-loopback
      requests (it runs after any portal guard that falls through).
    Every attempt that would otherwise have gone out is recorded and fails the test at
    teardown, even if the code under test swallowed the resulting error.
    """
    from playwright.async_api import Browser, BrowserType, Route

    import jobpilot.forms.browser as fb

    violations: list[str] = []
    original_allowed = fb.host_allowed

    def loopback(host: str | None) -> bool:
        return (host or "").lower().rstrip(".") in _LOOPBACK

    def host_allowed(host: str, allowed_hosts: set[str]) -> bool:
        ok = original_allowed(host, allowed_hosts)
        if ok and not loopback(host):
            violations.append(host)
            return False
        return ok

    async def abort_external(route: Route) -> None:
        url = route.request.url
        parts = urlsplit(url)
        if parts.scheme in {"http", "https", "ws", "wss"} and not loopback(parts.hostname):
            violations.append(url)
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    original_persistent = BrowserType.launch_persistent_context
    original_new_page = Browser.new_page
    original_new_context = Browser.new_context

    async def persistent(self: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = await original_persistent(self, *args, **kwargs)
        await ctx.route("**/*", abort_external)
        return ctx

    async def new_context(self: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = await original_new_context(self, *args, **kwargs)
        await ctx.route("**/*", abort_external)
        return ctx

    async def new_page(self: Any, *args: Any, **kwargs: Any) -> Any:
        page = await original_new_page(self, *args, **kwargs)
        await page.context.route("**/*", abort_external)
        return page

    monkeypatch.setattr(fb, "host_allowed", host_allowed)
    monkeypatch.setattr(BrowserType, "launch_persistent_context", persistent)
    monkeypatch.setattr(Browser, "new_context", new_context)
    monkeypatch.setattr(Browser, "new_page", new_page)
    yield
    assert violations == [], f"test tried to reach non-local hosts: {violations}"


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_FORMS = Path(__file__).resolve().parent / "fixtures" / "forms"


_REDIRECTS = {"/submit_ok": "/confirmation.html", "/submit_thanks": "/thank-you.html",
              "/submit_listing": "/jobs-board.html"}
_POST_PAGES = {
    "/submit_keep": (
        '<!doctype html><html><body><form id="application_form"><div class="error"></div>'
        '<p>Processing.</p></form></body></html>'),
    "/submit_noconfirm": "<!doctype html><html><body><p>Processing.</p></body></html>",
    "/submit_error": (
        '<!doctype html><html><body><form id="application_form" method="post" '
        'action="submit_error"><div class="field"><label for="email">Email *</label>'
        '<input type="email" id="email" name="email" aria-invalid="true">'
        '<p class="error">Email is invalid</p></div>'
        '<button type="submit">Submit Application</button></form></body></html>'),
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def form_posts() -> list[str]:
    """Paths of every POST the fixture server receives (a form submit would show up here)."""
    return []


@pytest.fixture
def form_server(form_posts: list[str]) -> Iterator[str]:
    """Serve tests/fixtures/forms on 127.0.0.1 (ephemeral port); yields the base URL.
    Every POST is recorded in ``form_posts``. POST /submit_ok (/submit_thanks) redirects
    (303) to /confirmation.html (/thank-you.html), /submit_listing to /jobs-board.html,
    /submit_noop back to the Referer (same page), /submit_noconfirm a neutral page,
    /submit_keep a page keeping an empty
    ``.error`` box, /submit_error the form again with a visible error; else 405.
    GET /jobs/submitted-roles/1 serves greenhouse_submit_noop.html."""
    class Recording(_QuietHandler):
        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path == "/jobs/submitted-roles/1":  # confirmation-like words in URL
                self.path = "/greenhouse_submit_noop.html" + (
                    "?" + parts.query if parts.query else "")
            super().do_GET()

        def do_POST(self) -> None:
            form_posts.append(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            route = urlsplit(self.path).path
            if route == "/submit_noop":  # back to the same form page (same URL)
                self.send_response(303)
                self.send_header("Location", self.headers.get("Referer") or "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif route in _REDIRECTS:
                self.send_response(303)
                self.send_header("Location", _REDIRECTS[route])
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif route in _POST_PAGES:
                body = _POST_PAGES[route].encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(405)

    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 partial(Recording, directory=str(_FORMS)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Careers fetches resolve their host first; tests never use real DNS."""
    import jobpilot.outreach.contacts as contacts

    async def fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]  # a public documentation-style address

    monkeypatch.setattr(contacts, "resolve_host", fake_resolve)
