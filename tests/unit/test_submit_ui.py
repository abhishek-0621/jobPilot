"""Submit button (typed confirm in live mode), uncertain resolution and "Prepare again"."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head, utcnow
from jobpilot.data.tables import ApplicationRow, AuditEventRow, TaskRow
from jobpilot.observability.events import EVENT_ORDER, prior_unresolved_click, record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import seed_ready_apps


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    upgrade_to_head(tmp_path / "submit.db")
    engine = make_engine(tmp_path / "submit.db")
    yield make_session_factory(engine)
    engine.dispose()


def _client(factory: sessionmaker[Session], cfg: AppConfig) -> TestClient:
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1")


def _approved_batch(factory: sessionmaker[Session], portal: str) -> tuple[str, str]:
    with factory() as s:
        (app_id,) = seed_ready_apps(s, 1)
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        app.portal = portal
        svc = ApprovalService(s)
        batch = svc.build_batch([app_id])
        svc.approve(batch.batch_id, 1, batch.content_hash or "", [app_id])
        s.commit()
        return batch.batch_id, app_id


def _submit_tasks(factory: sessionmaker[Session]) -> list[str]:
    with factory() as s:
        return list(s.execute(select(TaskRow.entity_id)
                              .where(TaskRow.kind == "SUBMIT")).scalars())


def test_submit_button_requires_typed_confirm_in_live(
        factory: sessionmaker[Session], tmp_path: Path) -> None:
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={}, dry_run=False,
                    live_portals=["greenhouse"])
    batch_id, app_id = _approved_batch(factory, "greenhouse")
    client = _client(factory, cfg)
    page = client.get(f"/ui/batches/{batch_id}").text
    assert "will submit LIVE to greenhouse" in page and 'name="confirm"' in page
    for confirm in ({}, {"confirm": "submit"}, {"confirm": "yes"}):
        r = client.post(f"/ui/batches/{batch_id}/submit", data=confirm)
        assert r.status_code == 422 and "Type SUBMIT to confirm live submission" in r.text
    assert client.post(f"/batches/{batch_id}/submit", json={}).status_code == 422
    assert _submit_tasks(factory) == []
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.submit_requested_at is None
    r = client.post(f"/ui/batches/{batch_id}/submit", data={"confirm": "SUBMIT"})
    assert r.status_code == 200 and "Submission queued for 1" in r.text
    assert _submit_tasks(factory) == [app_id]
    with factory() as s:
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "submit_requested")).scalar_one()
        assert ev.actor == "user" and app_id in ev.redacted_details_json


def test_submit_button_dry_run_needs_no_confirm(
        factory: sessionmaker[Session], tmp_path: Path) -> None:
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    batch_id, app_id = _approved_batch(factory, "greenhouse")
    client = _client(factory, cfg)
    page = client.get(f"/ui/batches/{batch_id}").text
    assert "dry run (simulated)" in page and 'name="confirm"' not in page
    assert client.post(f"/ui/batches/{batch_id}/submit").status_code == 200
    assert _submit_tasks(factory) == [app_id]


def _uncertain(factory: sessionmaker[Session]) -> str:
    """An application whose submit was clicked with no confirmation seen."""
    with factory() as s:
        (app_id,) = seed_ready_apps(s, 1)
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        app.status = "SUBMISSION_UNCERTAIN"
        app.submit_requested_at = utcnow()
        app.idempotency_key = "key-1"
        record_event(s, entity_type="application", entity_id=app_id, actor="orchestrator",
                     action="submit_clicked", outcome="ok",
                     details={"idempotency_key": "key-1"})
        record_event(s, entity_type="application", entity_id=app_id, actor="orchestrator",
                     action="submit_finished", outcome="SUBMISSION_UNCERTAIN",
                     details={"error_codes": ["PAGE_ERRORS:2"]})
        s.commit()
        return app_id


def _status(factory: sessionmaker[Session], app_id: str) -> str:
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        return app.status


def _transition_reason(factory: sessionmaker[Session], app_id: str) -> tuple[str, str]:
    with factory() as s:
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.entity_id == app_id, AuditEventRow.action == "status_changed")
            .order_by(EVENT_ORDER.desc())).scalars().first()
        assert ev is not None
        return ev.actor, ev.redacted_details_json


def test_uncertain_resolution_submitted(factory: sessionmaker[Session],
                                        tmp_path: Path) -> None:
    app_id = _uncertain(factory)
    client = _client(factory, AppConfig(storage_path=tmp_path / "s", model_routes={}))
    assert "Uncertain (1)" in client.get("/").text
    page = client.get("/ui/uncertain").text
    assert app_id in page and "PAGE_ERRORS:2" in page and "It was submitted" in page
    r = client.post(f"/ui/applications/{app_id}/resolve", data={"submitted": "yes"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _status(factory, app_id) == "CONFIRMED"
    actor, details = _transition_reason(factory, app_id)
    assert actor == "user" and '"reason":"user_confirmed"' in details
    assert "Uncertain (" not in client.get("/").text
    # resolving twice is refused
    assert client.post(f"/ui/applications/{app_id}/resolve",
                       data={"submitted": "no"}).status_code == 409


def test_uncertain_resolution_not_submitted(factory: sessionmaker[Session],
                                            tmp_path: Path) -> None:
    app_id = _uncertain(factory)
    client = _client(factory, AppConfig(storage_path=tmp_path / "s", model_routes={}))
    with factory() as s:
        assert prior_unresolved_click(s, app_id)
    r = client.post(f"/ui/applications/{app_id}/resolve", data={"submitted": "no"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _status(factory, app_id) == "SUBMISSION_FAILED"
    actor, details = _transition_reason(factory, app_id)
    assert actor == "user" and '"reason":"user_not_submitted"' in details
    with factory() as s:
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "user_resolved_not_submitted")).scalar_one()
        assert ev.redacted_details_json == f'{{"application_id":"{app_id}"}}'
        assert not prior_unresolved_click(s, app_id)
    assert "Prepare again" in client.get("/ui/uncertain").text


def test_prepare_again_after_failed(factory: sessionmaker[Session], tmp_path: Path) -> None:
    app_id = _uncertain(factory)
    with factory() as s:  # an approval still names the application
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        app.status = "READY_FOR_REVIEW"
        svc = ApprovalService(s)
        batch = svc.build_batch([app_id])
        svc.approve(batch.batch_id, 1, batch.content_hash or "", [app_id])
        app.status, app.form_snapshot_hash = "SUBMISSION_UNCERTAIN", "sha256:snap"
        s.commit()
    client = _client(factory, AppConfig(storage_path=tmp_path / "s", model_routes={}))
    # only from SUBMISSION_FAILED
    assert client.post(f"/ui/applications/{app_id}/prepare-again").status_code == 409
    client.post(f"/ui/applications/{app_id}/resolve", data={"submitted": "no"})
    r = client.post(f"/ui/applications/{app_id}/prepare-again", follow_redirects=False)
    assert r.status_code == 303
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "FORM_IN_PROGRESS"
        assert app.submit_requested_at is None and app.form_snapshot_hash is None
        assert ApprovalService(s).active_approval_for(app_id) is None
        tasks = [(t.kind, t.status) for t in s.execute(select(TaskRow).where(
            TaskRow.entity_id == app_id)).scalars()]
    assert tasks == [("PREPARE_FORM", "QUEUED")]
    actor, details = _transition_reason(factory, app_id)
    assert actor == "user" and '"reason":"prepare_again"' in details


def test_prepare_again_edge_is_user_only(factory: sessionmaker[Session]) -> None:
    from jobpilot.contracts.enums import ApplicationStatus
    from jobpilot.orchestrator.state_machine import IllegalTransition, transition

    with factory() as s:
        (app_id,) = seed_ready_apps(s, 1)
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        app.status = "SUBMISSION_FAILED"
        with pytest.raises(IllegalTransition):
            transition(s, app, ApplicationStatus.FORM_IN_PROGRESS, actor="orchestrator",
                       reason="t")


def _failed_with_click(factory: sessionmaker[Session], *, clicked: bool) -> str:
    if clicked:
        app_id = _uncertain(factory)
    else:
        with factory() as s:
            (app_id,) = seed_ready_apps(s, 1)
            s.commit()
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        app.status = "SUBMISSION_FAILED"
        s.commit()
    return app_id


def test_failed_with_click_can_be_resolved_not_submitted(
        factory: sessionmaker[Session], tmp_path: Path) -> None:
    app_id = _failed_with_click(factory, clicked=True)
    client = _client(factory, AppConfig(storage_path=tmp_path / "s", model_routes={}))
    page = client.get("/ui/uncertain").text
    assert page.split("Failed submissions")[1].count("It was not submitted") == 1
    assert client.post(f"/ui/applications/{app_id}/resolve",
                       data={"submitted": "yes"}).status_code == 409
    r = client.post(f"/ui/applications/{app_id}/resolve", data={"submitted": "no"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _status(factory, app_id) == "SUBMISSION_FAILED"
    with factory() as s:
        assert not prior_unresolved_click(s, app_id)
    assert "It was not submitted" not in client.get("/ui/uncertain").text


def test_failed_without_click_has_no_resolve(
        factory: sessionmaker[Session], tmp_path: Path) -> None:
    app_id = _failed_with_click(factory, clicked=False)
    client = _client(factory, AppConfig(storage_path=tmp_path / "s", model_routes={}))
    assert "It was not submitted" not in client.get("/ui/uncertain").text
    assert client.post(f"/ui/applications/{app_id}/resolve",
                       data={"submitted": "no"}).status_code == 409


def test_illegal_transition_is_409_and_rolled_back(
        factory: sessionmaker[Session], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from jobpilot.orchestrator.state_machine import IllegalTransition

    def boom(*args: object, **kwargs: object) -> None:
        raise IllegalTransition("nope")

    app_id = _uncertain(factory)
    client = _client(factory, AppConfig(storage_path=tmp_path / "s", model_routes={}))
    monkeypatch.setattr("jobpilot.api.actions.transition", boom)
    assert client.post(f"/ui/applications/{app_id}/resolve",
                       data={"submitted": "yes"}).status_code == 409
    assert _status(factory, app_id) == "SUBMISSION_UNCERTAIN"
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        app.status = "SUBMISSION_FAILED"
        s.commit()
    assert client.post(f"/ui/applications/{app_id}/prepare-again").status_code == 409
    assert _status(factory, app_id) == "SUBMISSION_FAILED"
    assert client.post("/ui/applications/missing/prepare-again").status_code == 404
    assert client.post("/ui/applications/missing/resolve",
                       data={"submitted": "no"}).status_code == 404
