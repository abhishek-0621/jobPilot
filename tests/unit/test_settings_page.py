"""Read-only Settings page: mode, schedule, last backup, retention, model routes."""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig, ModelRoute
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "s.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "storage", model_routes={
        "match": ModelRoute(model_tag="model-a:7b", fallback="model-b:3b")})


def _client(tmp_path: Path, factory: sessionmaker[Session], runner: object,
            platform: str = "darwin") -> TestClient:
    cfg = _cfg(tmp_path)
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    app = create_app(cfg, factory, orch, start_worker=False)
    app.state.schedule_runner = runner
    app.state.schedule_home = tmp_path / "home"
    app.state.platform = platform
    return TestClient(app, base_url="http://127.0.0.1")


def _fake_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 113, "", "")  # not loaded


def _boom(args: list[str]) -> subprocess.CompletedProcess[str]:
    raise subprocess.TimeoutExpired(args, 3)


def test_settings_page_renders(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    bdir = tmp_path / "storage" / "backups"
    bdir.mkdir(parents=True)
    old = bdir / "jobpilot-20260101-000000.tar.gz"
    new = bdir / "jobpilot-20260102-000000.tar.gz"
    old.write_bytes(b"x" * 10)
    new.write_bytes(b"y" * 2048)
    past = time.time() - 3 * 3600
    os.utime(new, (past, past))
    older = time.time() - 2 * 86400
    os.utime(old, (older, older))
    r = _client(tmp_path, factory, _fake_runner).get("/ui/settings")
    assert r.status_code == 200
    t = r.text
    assert "Dry run" in t and "on" in t and "config/app.yaml" in t
    assert "not editable here" in t
    assert "com.jobpilot.discover" in t and "not installed" in t
    assert "jobpilot-20260102-000000.tar.gz" in t and "2.0 KB" in t and "3 hours ago" in t
    assert "jobpilot-20260101-000000" not in t
    assert "365" in t and "180" in t and "model-a:7b" in t and "model-b:3b" in t
    assert "jobpilot export all" in t and "jobpilot maintenance" in t
    assert 'href="/ui/settings"' in t


def test_settings_page_no_mutating_controls_for_live_mode(
        tmp_path: Path, factory: sessionmaker[Session]) -> None:
    client = _client(tmp_path, factory, _fake_runner)
    t = client.get("/ui/settings").text
    assert "<form" not in t and "<button" not in t and "<input" not in t
    assert client.post("/ui/settings").status_code == 405
    mutating = [r for r in client.app.routes  # type: ignore[attr-defined]
                if getattr(r, "path", "").startswith("/ui/settings")
                and getattr(r, "methods", set()) - {"GET", "HEAD"}]
    assert mutating == []


def test_settings_schedule_unknown_on_runner_error(
        tmp_path: Path, factory: sessionmaker[Session]) -> None:
    r = _client(tmp_path, factory, _boom).get("/ui/settings")
    assert r.status_code == 200 and "unknown" in r.text


def test_settings_schedule_not_available_off_macos(
        tmp_path: Path, factory: sessionmaker[Session]) -> None:
    r = _client(tmp_path, factory, _boom, platform="linux").get("/ui/settings")
    assert "not available" in r.text and "unknown" not in r.text


def test_settings_no_backups_yet(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    assert "No backup yet" in _client(tmp_path, factory, _fake_runner).get("/ui/settings").text
