"""Dashboard assets are served locally: stylesheet, script and vendored Geist fonts."""

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import STATIC_DIR, create_app
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.orchestrator.engine import Orchestrator

FONTS = {
    "Geist-Variable.woff2":
        "a369fcf5628ea2aa4e1b9e2ec6a5b3624e365bda588e1f0f2f12b564f728fbb8",
    "GeistMono-Variable.woff2":
        "fba8f577f38a2bbcbe818efa6348dd58f36303a10b8737c42fefad275be563ab",
}


def _client(tmp_path: Path) -> TestClient:
    upgrade_to_head(tmp_path / "a.db")
    factory: sessionmaker[Session] = make_session_factory(make_engine(tmp_path / "a.db"))
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    return TestClient(create_app(cfg, factory, Orchestrator(factory, cfg, {}, {}),
                                 start_worker=False), base_url="http://127.0.0.1")


def test_vendored_fonts_match_recorded_hashes() -> None:
    for name, digest in FONTS.items():
        data = (STATIC_DIR / "fonts" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        assert digest in (STATIC_DIR / "fonts" / "README.md").read_text()


def test_pages_use_local_assets_only(tmp_path: Path) -> None:
    client = _client(tmp_path)
    page = client.get("/").text
    assert 'href="/static/app.css"' in page and 'href="#main"' in page
    assert '<main id="main"' in page and 'aria-current="page"' in page
    assert "fonts.googleapis" not in page and "cdn.jsdelivr" not in page
    css = client.get("/static/app.css")
    assert css.status_code == 200 and "font-display: swap" in css.text
    assert "http" not in css.text.split("*/", 1)[1]  # no remote url() after the header
    for name in FONTS:
        assert client.get(f"/static/fonts/{name}").status_code == 200
    assert client.get("/static/app.js").status_code == 200
