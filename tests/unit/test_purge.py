"""Deletion (one application / everything), retention and maintenance."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from jobpilot import cli
from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import (
    ApplicationRow,
    AuditEventRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
)
from jobpilot.ids import new_id
from jobpilot.ops import purge
from jobpilot.ops.maintenance import run_maintenance
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import seed_ready_apps
from tests.unit.test_cli import _config

Env = tuple[AppConfig, sessionmaker[Session]]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    storage = tmp_path / "storage"
    cfg = AppConfig(storage_path=storage, db_path=storage / "jobpilot.db", model_routes={})
    storage.mkdir()
    upgrade_to_head(cfg.db_path)
    engine = make_engine(cfg.db_path)
    yield cfg, make_session_factory(engine)
    engine.dispose()


def _seed(cfg: AppConfig, s: Session, status: str, *, changed_days_ago: int = 0) -> str:
    (app_id,) = seed_ready_apps(s, 1)
    app = s.get(ApplicationRow, app_id)
    assert app is not None
    app.status = status
    app.confirmation_reference = "REF-1"
    d = cfg.storage_path / "artifacts" / app_id
    d.mkdir(parents=True)
    (d / "resume_v1.pdf").write_bytes(b"%PDF")
    art = s.execute(select(DocumentArtifactRow).where(
        DocumentArtifactRow.application_id == app_id)).scalars().one()
    art.path = f"artifacts/{app_id}/resume_v1.pdf"
    s.add(AuditEventRow(event_id=new_id("evt"), entity_type="application", entity_id=app_id,
                        actor="user", action="status_changed", outcome="ok",
                        redacted_details_json="{}",
                        timestamp_utc=datetime.now(UTC) - timedelta(days=changed_days_ago)))
    s.commit()
    return app_id


def _count(s: Session, model: type, app_id: str) -> int:
    return s.execute(select(func.count()).select_from(model).where(
        model.application_id == app_id)).scalar_one()  # type: ignore[attr-defined]


@pytest.mark.parametrize("status", ["READY_FOR_REVIEW", "APPROVED", "SUBMISSION_IN_PROGRESS",
                                    "SUBMITTED", "NEEDS_USER_INPUT"])
def test_delete_refuses_in_flight(env: Env, status: str) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, status)
        with pytest.raises(purge.DeleteRefused):
            purge.delete_application(cfg, s, app_id, confirm=app_id)
        assert _count(s, FormFieldAnswerRow, app_id) == 1
    assert (cfg.storage_path / "artifacts" / app_id / "resume_v1.pdf").exists()


def test_delete_uncertain_refused(env: Env) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, "SUBMISSION_UNCERTAIN")
        with pytest.raises(purge.DeleteRefused, match="Resolve"):
            purge.delete_application(cfg, s, app_id, confirm=app_id)


def test_delete_app_removes_files_keeps_events(env: Env) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, "CONFIRMED")
        before = {e.event_id: e.redacted_details_json
                  for e in s.execute(select(AuditEventRow)).scalars()}
        with pytest.raises(purge.ConfirmationMismatch):
            purge.delete_application(cfg, s, app_id, confirm="nope")
        counts = purge.delete_application(cfg, s, app_id, confirm=app_id)
        assert counts == {"answers_n": 1, "artifacts_n": 1}
    assert not (cfg.storage_path / "artifacts" / app_id).exists()
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "DELETED"
        assert app.confirmation_reference is None
        assert s.get(JobRow, app.job_id) is not None
        assert _count(s, FormFieldAnswerRow, app_id) == 0
        assert _count(s, DocumentArtifactRow, app_id) == 0
        after = {e.event_id: e for e in s.execute(select(AuditEventRow)).scalars()}
        for eid, details in before.items():
            assert after[eid].redacted_details_json == details
        deleted = [e for e in after.values() if e.action == "data_deleted"]
        assert len(deleted) == 1
        assert "30 days" not in deleted[0].redacted_details_json
        with pytest.raises(purge.DeleteRefused):
            purge.delete_application(cfg, s, app_id, confirm=app_id)


def test_delete_all_requires_confirm(env: Env) -> None:
    cfg, _ = env
    for tree in ("profiles", "artifacts", "browser_state", "exports", "logs", "backups", "run"):
        (cfg.storage_path / tree).mkdir(exist_ok=True)
        (cfg.storage_path / tree / "f").write_text("x")
    with pytest.raises(purge.ConfirmationMismatch):
        purge.delete_all(cfg, confirm="delete all", server_running=lambda _c: False)
    assert cfg.db_path.exists()
    removed = purge.delete_all(cfg, confirm="DELETE ALL", server_running=lambda _c: False)
    assert cfg.db_path in removed and not cfg.db_path.exists()
    for tree in ("profiles", "artifacts", "browser_state", "exports", "logs"):
        assert not (cfg.storage_path / tree).exists()
    assert (cfg.storage_path / "backups" / "f").exists()
    assert (cfg.storage_path / "run").exists()
    purge.delete_all(cfg, confirm="DELETE ALL", include_backups=True,
                     server_running=lambda _c: False)
    assert not (cfg.storage_path / "backups").exists()


def test_delete_all_refuses_when_server_running(env: Env) -> None:
    cfg, _ = env
    with pytest.raises(purge.DeleteRefused, match="running"):
        purge.delete_all(cfg, confirm="DELETE ALL", server_running=lambda _c: True)
    (cfg.storage_path / "RESTORE_IN_PROGRESS").write_text("x")
    with pytest.raises(purge.DeleteRefused, match="restore"):
        purge.delete_all(cfg, confirm="DELETE ALL", server_running=lambda _c: False)
    assert cfg.db_path.exists()


def test_retention_only_terminal(env: Env) -> None:
    cfg, factory = env
    with factory() as s:
        old_done = _seed(cfg, s, "CONFIRMED", changed_days_ago=400)
        old_busy = _seed(cfg, s, "READY_FOR_REVIEW", changed_days_ago=400)
        new_done = _seed(cfg, s, "REJECTED_BY_USER", changed_days_ago=10)
    counts = run_maintenance(cfg)
    assert counts["artifacts_pruned"] == 1 and counts["contacts_pruned"] == 0
    assert not (cfg.storage_path / "artifacts" / old_done).exists()
    assert (cfg.storage_path / "artifacts" / old_busy / "resume_v1.pdf").exists()
    assert (cfg.storage_path / "artifacts" / new_done / "resume_v1.pdf").exists()
    with factory() as s:
        assert s.get(ApplicationRow, old_done).status == "CONFIRMED"  # type: ignore[union-attr]
        assert _count(s, DocumentArtifactRow, old_done) == 0
        assert _count(s, DocumentArtifactRow, old_busy) == 1
        assert _count(s, FormFieldAnswerRow, old_done) == 1
        actions = [e.action for e in s.execute(select(AuditEventRow)).scalars()]
        assert actions.count("artifacts_pruned") == 1 and "maintenance_run" in actions
    assert run_maintenance(cfg)["artifacts_pruned"] == 0  # idempotent


def test_maintenance_prunes_old_exports(env: Env) -> None:
    cfg, _ = env
    exp = cfg.storage_path / "exports"
    (exp / "export-all-old").mkdir(parents=True)
    old_zip, new_zip = exp / "application-a-old.zip", exp / "application-a-new.zip"
    old_zip.write_bytes(b"PK")
    new_zip.write_bytes(b"PK")
    past = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    for p in (old_zip, exp / "export-all-old"):
        os.utime(p, (past, past))
    assert run_maintenance(cfg)["exports_pruned"] == 2
    assert new_zip.exists() and not old_zip.exists() and not (exp / "export-all-old").exists()


def _client(cfg: AppConfig, factory: sessionmaker[Session]) -> TestClient:
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1")


def test_dashboard_delete_form(env: Env) -> None:
    cfg, factory = env
    with factory() as s:
        busy = _seed(cfg, s, "READY_FOR_REVIEW")
        done = _seed(cfg, s, "SUBMITTED_BY_USER")
    client = _client(cfg, factory)
    assert "Type the application id to confirm" not in client.get(
        f"/ui/applications/{busy}").text
    assert "Type the application id to confirm" in client.get(f"/ui/applications/{done}").text
    assert client.post(f"/ui/applications/{busy}/delete",
                       data={"confirm": busy}).status_code == 409
    assert client.post(f"/ui/applications/{done}/delete",
                       data={"confirm": "wrong"}).status_code == 422
    r = client.post(f"/ui/applications/{done}/delete", data={"confirm": done},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui/jobs")
    assert "Deleted the data" in client.get(r.headers["location"]).text
    with factory() as s:
        assert s.get(ApplicationRow, done).status == "DELETED"  # type: ignore[union-attr]


def test_export_route_missing_job_404(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, "CONFIRMED")
    from jobpilot.ops import export as ex

    def gone(*_a: object, **_k: object) -> Path:
        raise LookupError("job for application not found")

    monkeypatch.setattr(ex, "export_application", gone)
    assert _client(cfg, factory).get(f"/ui/applications/{app_id}/export").status_code == 404


def test_dashboard_export_leaves_no_file(env: Env) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, "CONFIRMED")
    r = _client(cfg, factory).get(f"/ui/applications/{app_id}/export")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    exp = cfg.storage_path / "exports"
    assert not exp.exists() or not any(exp.iterdir())


def test_cli_delete_and_maintenance(env: Env, tmp_path: Path) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, "CONFIRMED")
    d = _config(tmp_path)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["delete", "app", app_id, "--confirm", "x", "--config-dir", str(d)])
    assert r.exit_code == 1 and "refused" in r.output
    r = runner.invoke(cli.app, ["delete", "app", app_id, "--confirm", app_id,
                                "--config-dir", str(d)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["maintenance", "--config-dir", str(d)])
    assert r.exit_code == 0 and "Maintenance done" in r.output
    r = runner.invoke(cli.app, ["delete", "all", "--confirm", "nope", "--config-dir", str(d)])
    assert r.exit_code == 1 and cfg.db_path.exists()


def test_scheduled_maintenance_error_exits_zero(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    d = _config(tmp_path)

    async def fake_now(_cfg: object) -> int:
        return 1

    def boom(_cfg: object) -> dict[str, int]:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cli, "_discover_now", fake_now)
    monkeypatch.setattr("jobpilot.ops.maintenance.run_maintenance", boom)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["discover", "--run-now", "--scheduled", "--config-dir", str(d)])
    assert r.exit_code == 0 and "Maintenance failed" in r.output
    r = runner.invoke(cli.app, ["discover", "--run-now", "--config-dir", str(d)])
    assert r.exit_code == 1


def test_pruned_failed_app_clears_refs_and_prepare_again_tailors(env: Env) -> None:
    from jobpilot.api.actions import prepare_again
    from jobpilot.data.tables import TaskRow

    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, "SUBMISSION_FAILED", changed_days_ago=400)
    assert run_maintenance(cfg)["artifacts_pruned"] == 1
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        assert app.resume_artifact_id is None and app.cover_letter_artifact_id is None
        prepare_again(s, Orchestrator(factory, cfg, {}, {}), app_id)
        assert s.get(ApplicationRow, app_id).status == "DOCUMENTS_PENDING"  # type: ignore[union-attr]
        kinds = [t.kind for t in s.execute(select(TaskRow).where(
            TaskRow.entity_id == app_id)).scalars()]
        assert kinds == ["TAILOR"]


def test_delete_refuses_symlinked_app_dir(env: Env) -> None:
    cfg, factory = env
    with factory() as s:
        other = _seed(cfg, s, "READY_FOR_REVIEW")
        app_id = _seed(cfg, s, "CONFIRMED")
    victim = cfg.storage_path / "artifacts" / app_id
    for p in victim.iterdir():
        p.unlink()
    victim.rmdir()
    victim.symlink_to(cfg.storage_path / "artifacts" / other)
    with factory() as s, pytest.raises(purge.DeleteRefused, match="symlink"):
        purge.delete_application(cfg, s, app_id, confirm=app_id)
    assert (cfg.storage_path / "artifacts" / other / "resume_v1.pdf").exists()


def test_delete_route_lookup_error_404(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, "CONFIRMED")

    def gone(*_a: object, **_k: object) -> dict[str, int]:
        raise LookupError("vanished")

    monkeypatch.setattr(purge, "delete_application", gone)
    r = _client(cfg, factory).post(f"/ui/applications/{app_id}/delete",
                                   data={"confirm": app_id})
    assert r.status_code == 404


def test_delete_event_recorded_even_if_file_removal_fails(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, factory = env
    with factory() as s:
        app_id = _seed(cfg, s, "CONFIRMED")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk error")

    monkeypatch.setattr(purge.shutil, "rmtree", boom)
    with factory() as s, pytest.raises(OSError):
        purge.delete_application(cfg, s, app_id, confirm=app_id)
    with factory() as s:
        assert s.get(ApplicationRow, app_id).status == "DELETED"  # type: ignore[union-attr]
        actions = [e.action for e in s.execute(select(AuditEventRow).where(
            AuditEventRow.entity_id == app_id)).scalars()]
        assert actions.count("data_deleted") == 1


def test_maintenance_skips_symlinked_app_dir_and_continues(
        env: Env, caplog: pytest.LogCaptureFixture) -> None:
    cfg, factory = env
    with factory() as s:
        bad = _seed(cfg, s, "CONFIRMED", changed_days_ago=400)
        good = _seed(cfg, s, "REJECTED_BY_USER", changed_days_ago=400)
    victim = cfg.storage_path / "artifacts" / bad
    outside = cfg.storage_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    for p in victim.iterdir():
        p.unlink()
    victim.rmdir()
    victim.symlink_to(outside)
    with caplog.at_level("WARNING"):
        counts = run_maintenance(cfg)
    assert counts["artifacts_pruned"] == 1
    assert "symlink" in caplog.text and bad in caplog.text
    assert (outside / "keep.txt").exists() and victim.is_symlink()
    assert not (cfg.storage_path / "artifacts" / good).exists()
