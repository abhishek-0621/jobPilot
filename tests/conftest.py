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


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def form_server() -> Iterator[str]:
    """Serve tests/fixtures/forms on 127.0.0.1 (ephemeral port); yields the base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 partial(_QuietHandler, directory=str(_FORMS)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
