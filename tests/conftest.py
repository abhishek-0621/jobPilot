from collections.abc import Iterator
from pathlib import Path
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
def _loopback_forms_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net: no test may open a form on a real portal."""
    from jobpilot.portals.playwright_form import PlaywrightFormAdapter

    original = PlaywrightFormAdapter.open_job

    async def guarded(self: PlaywrightFormAdapter, url: str) -> None:
        if urlsplit(url).hostname not in {"127.0.0.1", "localhost"}:
            raise AssertionError(f"test tried to open a non-local form: {url}")
        await original(self, url)

    monkeypatch.setattr(PlaywrightFormAdapter, "open_job", guarded)
