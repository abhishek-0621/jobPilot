"""Plan 10 Task 5: dashboard & ops polish, first-run readiness."""

import logging
import runpy
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from jobpilot import cli
from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import jobs_with_scores
from jobpilot.data.tables import JobRow, MatchAssessmentRow
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import make_app_row, seed_ready_apps


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "p.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


def _client(factory: sessionmaker[Session], tmp_path: Path, **cfg_kw: Any) -> TestClient:
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={}, **cfg_kw)
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1")


def _count(factory: sessionmaker[Session], fn) -> int:  # type: ignore[no-untyped-def]
    engine = factory.kw["bind"]
    n = 0

    def inc(*a: Any) -> None:
        nonlocal n
        n += 1
    event.listen(engine, "before_cursor_execute", inc)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", inc)
    return n


def _assess(s: Session, job_id: str, version: int, score: float) -> None:
    s.add(MatchAssessmentRow(job_id=job_id, profile_version=version, skill_fit=0,
                             experience_fit=0, project_fit=0, role_seniority_fit=0,
                             location_fit=0, total_score=score, hard_filter_passed=True,
                             evidence_json="{}", confidence=0.9))


def _seed_jobs(factory: sessionmaker[Session], n: int) -> None:
    with factory() as s:
        for _ in range(n):
            app = make_app_row(s)
            _assess(s, app.job_id, 1, 50)
        s.commit()


def test_jobs_page_query_count_constant(factory, tmp_path) -> None:
    client = _client(factory, tmp_path)
    _seed_jobs(factory, 2)
    small = _count(factory, lambda: client.get("/ui/jobs"))
    _seed_jobs(factory, 6)
    big = _count(factory, lambda: client.get("/ui/jobs"))
    assert big == small


def test_batch_page_query_count_constant(factory, tmp_path) -> None:
    client = _client(factory, tmp_path)

    def batch(n: int) -> str:
        with factory() as s:
            ids = seed_ready_apps(s, n)
            b = ApprovalService(s).build_batch(ids)
            s.commit()
            return b.batch_id
    b2, b6 = batch(2), batch(6)
    assert _count(factory, lambda: client.get(f"/ui/batches/{b6}")) == \
        _count(factory, lambda: client.get(f"/ui/batches/{b2}"))


def test_list_and_detail_use_latest_score(factory, tmp_path) -> None:
    with factory() as s:
        app = make_app_row(s)
        _assess(s, app.job_id, 1, 90)
        _assess(s, app.job_id, 2, 40)  # latest assessment, lower score
        s.commit()
        job_id = app.job_id
    with factory() as s:
        [(job, score)] = [(j, sc) for j, sc in jobs_with_scores(s) if j.job_id == job_id]
        assert score == 40
    page = _client(factory, tmp_path).get("/ui/jobs").text
    assert "<td>40.0</td>" in page and "<td>90.0</td>" not in page  # ids may contain "90"
    assert JobRow  # imported for typing clarity


# ---- health_check / doctor connection errors ----

def test_health_check_connection_error_no_traceback(capsys, monkeypatch) -> None:
    import urllib.request

    def refuse(*a: Any, **kw: Any) -> Any:
        raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    mod = runpy.run_path("scripts/health_check.py")
    assert mod["main"]() == 2
    out = capsys.readouterr()
    assert "jobpilot start" in out.out + out.err and "Traceback" not in out.out + out.err


def _config(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "app.yaml").write_text(yaml.safe_dump({
        "db_path": str(tmp_path / "storage" / "jobpilot.db"),
        "storage_path": str(tmp_path / "storage")}), encoding="utf-8")
    (d / "model_routes.yaml").write_text(yaml.safe_dump({"routes": {
        "a": {"model_tag": "m1:small"}}}), encoding="utf-8")
    return d


def test_doctor_malformed_ollama_body_no_traceback(tmp_path, monkeypatch) -> None:
    d = _config(tmp_path)
    monkeypatch.setattr(cli.httpx, "get",
                        lambda url, **kw: httpx.Response(200, content=b"<html>",
                                                         request=httpx.Request("GET", url)))
    monkeypatch.setattr(cli, "_chromium_installed", lambda: True)
    r = CliRunner().invoke(cli.app, ["doctor", "--config-dir", str(d)])
    assert r.exit_code == 1 and r.exception is None or isinstance(r.exception, SystemExit)
    assert "unreachable" in r.output


def test_doctor_first_run_readiness_lines(tmp_path, monkeypatch) -> None:
    d = _config(tmp_path)
    assert CliRunner().invoke(cli.app, ["migrate", "--config-dir", str(d)]).exit_code == 0
    monkeypatch.setattr(cli.httpx, "get", lambda url, **kw: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(cli, "_chromium_installed", lambda: False)
    out = CliRunner().invoke(cli.app, ["doctor", "--config-dir", str(d)]).output
    assert "ollama pull m1:small" in out
    assert "verified facts: 0" in out and "jobpilot profile import" in out
    assert "verified stories: 0" in out
    assert "companies: none configured" in out and "jobpilot boards find" in out
    assert "playwright install chromium" in out


# ---- settings ----

def test_settings_logs_schedule_status_error(factory, tmp_path, caplog) -> None:
    client = _client(factory, tmp_path)

    def boom(args: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, 3)
    client.app.state.schedule_runner = boom  # type: ignore[attr-defined]
    client.app.state.schedule_home = tmp_path / "home"  # type: ignore[attr-defined]
    client.app.state.platform = "darwin"  # type: ignore[attr-defined]
    with caplog.at_level(logging.WARNING):
        r = client.get("/ui/settings")
    assert r.status_code == 200 and "unknown" in r.text
    assert any("schedule status" in rec.getMessage() for rec in caplog.records)


def test_settings_live_mode_render(factory, tmp_path) -> None:
    client = _client(factory, tmp_path, dry_run=False, live_portals=["greenhouse"])
    client.app.state.platform = "linux"  # type: ignore[attr-defined]
    t = client.get("/ui/settings").text
    assert '<span class="live">off</span>' in t
    assert '<span class="live">greenhouse</span>' in t
    assert "nothing is submitted" not in t
