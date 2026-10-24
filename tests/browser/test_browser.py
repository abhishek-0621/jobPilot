from pathlib import Path

import pytest

from jobpilot.config import AppConfig
from jobpilot.documents.render import chromium_available
from jobpilot.forms.browser import detect_blockers, portal_page

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium not installed"),
]


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "storage", browser_headless=True, model_routes={})


async def test_disallowed_host_aborted(form_server: str, tmp_path: Path) -> None:
    async with portal_page(_cfg(tmp_path), "greenhouse", {"127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/external.html")
        await s.page.wait_for_load_state("load")
        assert s.blocked == ["https://tracker.example.invalid/x.png"]
        await s.page.goto(f"{form_server}/greenhouse.html")
        assert await s.page.locator("#application_form").count() == 1
        assert await detect_blockers(s.page) == []


async def test_detect_captcha(form_server: str, tmp_path: Path) -> None:
    async with portal_page(_cfg(tmp_path), "greenhouse", {"127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/captcha.html")
        assert await detect_blockers(s.page) == ["captcha"]


async def test_detect_login(form_server: str, tmp_path: Path) -> None:
    async with portal_page(_cfg(tmp_path), "lever", {"127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/login.html")
        assert await detect_blockers(s.page) == ["login"]


async def test_persistent_context_in_storage(form_server: str, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    async with portal_page(cfg, "lever", {"127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/lever.html")
    state = tmp_path / "storage" / "browser_state" / "lever"
    assert state.is_dir() and any(state.iterdir())


async def test_websocket_to_disallowed_host_blocked(form_server: str, tmp_path: Path) -> None:
    async with portal_page(_cfg(tmp_path), "greenhouse", {"127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/websocket.html")
        await s.page.wait_for_timeout(500)
        assert s.blocked == ["ws://socket.example.invalid/"]


async def test_detect_mfa(form_server: str, tmp_path: Path) -> None:
    async with portal_page(_cfg(tmp_path), "lever", {"127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/mfa.html")
        assert await detect_blockers(s.page) == ["mfa"]


async def test_detect_bot_check(form_server: str, tmp_path: Path) -> None:
    async with portal_page(_cfg(tmp_path), "lever", {"127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/bot_check.html")
        assert await detect_blockers(s.page) == ["bot_check"]


async def test_empty_allowed_host_ignored(form_server: str, tmp_path: Path) -> None:
    async with portal_page(_cfg(tmp_path), "lever", {"", "127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/external.html")
        assert s.blocked == ["https://tracker.example.invalid/x.png"]


async def test_fixture_submit_reaches_recorder(
        form_server: str, form_posts: list[str], tmp_path: Path) -> None:
    """Proves the no-submit check can fail: a real click on Submit POSTs to the server."""
    async with portal_page(_cfg(tmp_path), "greenhouse", {"127.0.0.1"}) as s:
        await s.page.goto(f"{form_server}/greenhouse.html")
        await s.page.evaluate("document.querySelector('#application_form').noValidate = true")
        async with s.page.expect_request(
                lambda r: r.method == "POST", timeout=5000):
            await s.page.click("#submit_app")
    assert form_posts == ["/submit"]
