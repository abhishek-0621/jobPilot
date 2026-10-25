"""Per-application audit bundle, export-all and the dashboard export route."""

import json
import stat
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from jobpilot import cli
from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ErrorCode
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalRow,
    DocumentArtifactRow,
    JobRow,
    MatchAssessmentRow,
)
from jobpilot.observability.events import record_event
from jobpilot.ops import export as ex
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.util.hashing import canonical_json
from tests.factories import seed_ready_apps
from tests.unit.test_cli import _config


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[AppConfig, sessionmaker[Session]]]:
    storage = tmp_path / "storage"
    cfg = AppConfig(storage_path=storage, db_path=storage / "jobpilot.db", model_routes={})
    storage.mkdir()
    upgrade_to_head(cfg.db_path)
    engine = make_engine(cfg.db_path)
    yield cfg, make_session_factory(engine)
    engine.dispose()


def _seed(cfg: AppConfig, s: Session) -> str:
    (app_id,) = seed_ready_apps(s, 1)
    job = s.execute(select(JobRow)).scalars().one()
    art_dir = cfg.storage_path / "artifacts" / app_id
    art_dir.mkdir(parents=True)
    (art_dir / "resume_v1.pdf").write_bytes(b"%PDF resume")
    outside = cfg.storage_path / "secret.txt"
    outside.write_text("do not export")
    a = s.execute(select(DocumentArtifactRow)).scalars().one()
    a.path = f"artifacts/{app_id}/resume_v1.pdf"
    s.add(DocumentArtifactRow(artifact_id="art_evil", application_id=app_id, type="x",
                              path="secret.txt", content_hash="sha256:e"))
    s.add(DocumentArtifactRow(artifact_id="art_trav", application_id=app_id, type="x",
                              path=f"artifacts/{app_id}/../../secret.txt",
                              content_hash="sha256:t"))
    s.add(MatchAssessmentRow(
        job_id=job.job_id, profile_version=1, skill_fit=0.9, experience_fit=0.8,
        project_fit=0.7, role_seniority_fit=0.6, location_fit=1.0, total_score=0.8,
        hard_filter_passed=True, confidence=0.9,
        evidence_json=canonical_json({"matched_evidence_fact_ids": ["fact_1"],
                                      "explanation": "Good fit"})))
    s.add(ApprovalRow(approval_id="appr_1", batch_id="batch_1", batch_version=2, kind="submit",
                      content_hash="sha256:b"))
    row = s.get(ApplicationRow, app_id)
    assert row is not None
    row.batch_id = "batch_1"
    record_event(s, entity_type="application", entity_id=app_id, actor="user",
                 action="first", outcome="ok", details={"n": 1})
    record_event(s, entity_type="application", entity_id=app_id, actor="orchestrator",
                 action="second", outcome="failed", details={"api_token": "abc123"},
                 error_code=ErrorCode.SOURCE_TIMEOUT)
    s.commit()
    return app_id


def test_export_bundle_contents(env: tuple[AppConfig, sessionmaker[Session]]) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s)
        path = ex.export_application(cfg, s, app_id)
    assert path.parent == cfg.storage_path / "exports"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        data = json.loads(z.read("application.json"))
        assert z.read("documents/resume_v1.pdf") == b"%PDF resume"
    assert names == {"application.json", "documents/resume_v1.pdf"}
    assert data["schema"] == 1 and data["exported_utc"]
    assert set(data["job"]) == {"id", "title", "company", "location", "source",
                                "canonical_url", "application_url", "status"}
    assert data["match"]["evidence_fact_ids"] == ["fact_1"]
    assert data["match"]["explanation"] == "Good fit"
    assert data["match"]["scores"]["total_score"] == 0.8
    assert data["application"]["status"] == "READY_FOR_REVIEW"
    assert [a["field_id"] for a in data["answers"]] == ["notice_period"]
    assert data["answers"][0]["value"] == "30 days"
    by_id = {a["id"]: a for a in data["artifacts"]}
    good = next(a for i, a in by_id.items() if i not in ("art_evil", "art_trav"))
    assert good["path_in_zip"] == "documents/resume_v1.pdf"
    assert by_id["art_evil"]["path_in_zip"] is None
    assert by_id["art_trav"]["path_in_zip"] is None
    assert data["approvals"][0]["batch_id"] == "batch_1"
    assert data["approvals"][0]["version"] == 2 and data["approvals"][0]["kind"] == "submit"
    assert [e["action"] for e in data["events"]] == ["first", "second"]
    assert set(data["events"][0]) == {"timestamp_utc", "entity_type", "entity_id", "actor",
                                      "action", "outcome", "redacted_details", "error_code"}


def test_export_excludes_secrets_and_browser_state(
        env: tuple[AppConfig, sessionmaker[Session]]) -> None:
    cfg, factory = env
    cfg.browser_state_dir.mkdir()
    (cfg.browser_state_dir / "cookies.json").write_text("session-cookie-value")
    with factory() as s:
        app_id = _seed(cfg, s)
        path = ex.export_application(cfg, s, app_id)
        all_dir = ex.export_all(cfg, s)
    with zipfile.ZipFile(path) as z:
        blob = b"".join(z.read(n) for n in z.namelist())
        assert not any("browser_state" in n or "cookies" in n for n in z.namelist())
    assert b"abc123" not in blob and b"[REDACTED]" in blob
    assert b"session-cookie-value" not in blob and b"do not export" not in blob
    assert not any("browser_state" in str(p) for p in all_dir.rglob("*"))


def test_export_all_tables(env: tuple[AppConfig, sessionmaker[Session]]) -> None:
    cfg, factory = env
    (cfg.storage_path / "profiles").mkdir()
    (cfg.storage_path / "profiles" / "profile.yaml").write_text("name: Test\n")
    for junk in ("backups", "logs", "run", "exports", "browser_state"):
        (cfg.storage_path / junk).mkdir(exist_ok=True)
        (cfg.storage_path / junk / "x.txt").write_text("junk")
    with factory() as s:
        app_id = _seed(cfg, s)
        out = ex.export_all(cfg, s)
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    top = {p.name for p in out.iterdir()}
    assert {"job.json", "application.json", "audit_event.json", "approval.json",
            "profiles", "artifacts"} <= top
    assert top.isdisjoint({"backups", "logs", "run", "exports", "browser_state"})
    apps = json.loads((out / "application.json").read_text())
    assert apps[0]["application_id"] == app_id
    events = json.loads((out / "audit_event.json").read_text())
    assert "abc123" not in json.dumps(events)
    assert isinstance(events[0]["timestamp_utc"], str)
    assert (out / "profiles" / "profile.yaml").read_text() == "name: Test\n"
    assert (out / "artifacts" / app_id / "resume_v1.pdf").exists()
    assert stat.S_IMODE((out / "job.json").stat().st_mode) == 0o600


def test_export_route(env: tuple[AppConfig, sessionmaker[Session]]) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s)
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    client = TestClient(create_app(cfg, factory, orch, start_worker=False),
                        base_url="http://127.0.0.1")
    r = client.get(f"/ui/applications/{app_id}/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.content[:2] == b"PK"
    assert client.get("/ui/applications/nope/export").status_code == 404
    page = client.get(f"/ui/applications/{app_id}")
    assert f"/ui/applications/{app_id}/export" in page.text
    assert "personal data" in page.text


def test_export_cli(env: tuple[AppConfig, sessionmaker[Session]], tmp_path: Path) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s)
    cfg_dir = _config(tmp_path)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["export", "app", app_id, "--config-dir", str(cfg_dir)])
    assert r.exit_code == 0, r.output
    assert "personal data" in r.output
    assert list((cfg.storage_path / "exports").glob("application-*.zip"))
    r = runner.invoke(cli.app, ["export", "all", "--config-dir", str(cfg_dir)])
    assert r.exit_code == 0 and "export-all-" in r.output
    r = runner.invoke(cli.app, ["export", "app", "missing", "--config-dir", str(cfg_dir)])
    assert r.exit_code == 1
