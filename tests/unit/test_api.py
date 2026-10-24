from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig, ModelRoute
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import JobRow
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import make_app_row, seed_ready_apps


class FakeGateway:
    def __init__(self, models: set[str] | Exception) -> None:
        self.models = models

    async def available_models(self) -> set[str]:
        if isinstance(self.models, Exception):
            raise self.models
        return self.models


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "api.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


def make_client(factory: sessionmaker[Session], tmp_path: Path,
                gateway: Any = None) -> TestClient:
    cfg = AppConfig(
        storage_path=tmp_path,
        model_routes={
            "a": ModelRoute(model_tag="m1", fallback="m3"),
            "b": ModelRoute(model_tag="m2"),
        },
    )
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, gateway, start_worker=False),
                      base_url="http://127.0.0.1")


def seed_batch(factory: sessionmaker[Session]) -> tuple[list[str], str, str]:
    with factory() as s:
        ids = seed_ready_apps(s, 2)
        batch = ApprovalService(s).build_batch(ids)
        out = (ids, batch.batch_id, batch.content_hash or "")
        s.commit()
    return out


def test_approve_endpoint_409_on_stale_hash(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    ids, batch_id, h = seed_batch(factory)
    client = make_client(factory, tmp_path)
    r = client.post(f"/batches/{batch_id}/approve",
                    json={"version": 1, "content_hash": "sha256:wrong", "application_ids": ids})
    assert r.status_code == 409
    assert r.json() == {"detail": "stale"}
    ok = client.post(f"/batches/{batch_id}/approve",
                     json={"version": 1, "content_hash": h, "application_ids": ids})
    assert ok.status_code == 200
    assert ok.json()["batch_status"] == "APPROVED"
    assert ok.json()["approval_id"].startswith("apr")
    bad = client.post(f"/batches/{batch_id}/approve",
                      json={"version": 1, "content_hash": h, "application_ids": ["nope"]})
    assert bad.status_code == 422


def test_create_batch_and_application_detail(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as s:
        ids = seed_ready_apps(s, 1)
        record_event(s, entity_type="application", entity_id=ids[0], actor="system",
                     action="note", outcome="ok")
        s.commit()
    client = make_client(factory, tmp_path)
    assert client.post("/batches", json={"application_ids": []}).status_code == 422
    b = client.post("/batches", json={"application_ids": ids})
    assert b.status_code == 200
    assert b.json()["status"] == "READY_FOR_REVIEW"
    assert client.get(f"/batches/{b.json()['batch_id']}").json()["items"][0][
        "application_id"] == ids[0]
    d = client.get(f"/applications/{ids[0]}").json()
    assert d["job"]["title"] == "Backend Engineer"
    assert d["artifacts"][0]["type"] == "resume"
    assert d["answers"][0]["proposed_value"] == "30 days"
    assert d["events"][0]["action"] == "note"
    assert client.get("/applications/missing").status_code == 404


def test_overview_counts(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as s:
        make_app_row(s, "NOT_STARTED")
        make_app_row(s, "NOT_STARTED")
        make_app_row(s, "SUBMITTED")
        ids = seed_ready_apps(s, 1)
        ApprovalService(s).build_batch(ids)
        s.commit()
    client = make_client(factory, tmp_path)
    body = client.get("/overview").json()
    assert body["applications"]["NOT_STARTED"] == 2
    assert body["applications"]["SUBMITTED"] == 1
    assert body["applications"]["READY_FOR_REVIEW"] == 1
    assert body["jobs"]["DISCOVERED"] == 4
    assert body["pending_approvals"] == 1
    assert body["recent_failures"] == []


def test_health_reports_missing_models(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client = make_client(factory, tmp_path, FakeGateway({"m1"}))
    body = client.get("/health").json()
    assert body["ollama"] is True
    assert body["missing_models"] == ["m2", "m3"]
    assert body["dry_run"] is True
    assert body["db_revision"]
    assert body["disk_free_gb"] > 0
    down = make_client(factory, tmp_path, FakeGateway(RuntimeError("down")))
    b2 = down.get("/health").json()
    assert b2["ollama"] is False
    assert b2["missing_models"] == ["m1", "m2", "m3"]


def test_events_endpoint_never_returns_secrets(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    secret = "sk-supersecret-token-value-123"
    with factory() as s:
        record_event(s, entity_type="application", entity_id="app_x", actor="system",
                     action="login", outcome="ok",
                     details={"api_key": secret, "authorization": f"Bearer {secret}",
                              "note": "fine"})
        s.commit()
    client = make_client(factory, tmp_path)
    r = client.get("/events", params={"entity_id": "app_x"})
    assert r.status_code == 200
    assert secret not in r.text
    assert r.json()[0]["redacted_details"]["note"] == "fine"


def test_jobs_and_control(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as s:
        app = make_app_row(s)
        job_id = app.job_id
        s.commit()
    client = make_client(factory, tmp_path)
    assert len(client.get("/jobs").json()) == 1
    assert client.get("/jobs", params={"min_score": 50}).json() == []
    assert client.get("/jobs", params={"status": "MATCHED"}).json() == []
    assert client.get(f"/jobs/{job_id}").json()["match"] is None
    assert client.get("/jobs/missing").status_code == 404
    for action in ("pause", "resume", "stop"):
        assert client.post(f"/control/{action}").json()["action"] == action
    assert client.post("/control/explode").status_code == 422
    with factory() as s:
        assert s.get(JobRow, job_id) is not None


def test_lifespan_runs_and_stops_worker(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cfg = AppConfig(storage_path=tmp_path, model_routes={})
    orch = Orchestrator(factory, cfg, {}, {})
    with TestClient(create_app(cfg, factory, orch), base_url="http://127.0.0.1") as client:
        assert client.get("/overview").status_code == 200


async def test_worker_loop_survives_run_once_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from jobpilot.api import main

    stop = asyncio.Event()
    calls: list[int] = []

    class Boom:
        async def run_once(self) -> bool:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("db locked")
            stop.set()
            return False

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(aw: Any, timeout: float) -> Any:
        return await real_wait_for(aw, timeout=0.01)

    monkeypatch.setattr(main.asyncio, "wait_for", fast_wait_for)
    await real_wait_for(main.worker_loop(Boom(), stop), timeout=5)  # type: ignore[arg-type]
    assert len(calls) == 2


def test_application_detail_404_only_for_missing(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobpilot.api import main

    client = make_client(factory, tmp_path)
    assert client.get("/applications/app_missing").status_code == 404

    def broken(s: Any, application_id: str) -> Any:
        raise RuntimeError("db corrupted")

    monkeypatch.setattr(main, "get_application", broken)
    with pytest.raises(RuntimeError):
        client.get("/applications/app_missing")


def test_approve_endpoint_enqueues_submit(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    from sqlalchemy import select

    from jobpilot.data.tables import TaskRow

    ids, batch_id, h = seed_batch(factory)
    client = make_client(factory, tmp_path)
    ok = client.post(f"/batches/{batch_id}/approve",
                     json={"version": 1, "content_hash": h, "application_ids": ids[:1]})
    assert ok.status_code == 200
    with factory() as s:
        tasks = s.execute(select(TaskRow)).scalars().all()
    assert [(t.kind, t.entity_id) for t in tasks] == [("SUBMIT", ids[0])]


def _seed_job(factory: sessionmaker[Session], status: str = "MATCHED",
              job_id: str = "job_m1") -> str:
    from datetime import UTC, datetime

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    with factory() as s:
        s.add(JobRow(job_id=job_id, source="greenhouse", source_job_id=job_id,
                     canonical_url=f"https://example.com/{job_id}", company="Example AI",
                     title="GenAI Engineer", location="Bengaluru",
                     description_text="desc", status=status, first_seen_at=t0,
                     last_seen_at=t0))
        s.commit()
    return job_id


def test_discovery_endpoint_conflict_when_running(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    from jobpilot.data.tables import TaskRow

    client = make_client(factory, tmp_path)
    r = client.post("/runs/discovery")
    assert r.status_code == 200 and r.json()["task_id"].startswith("task")
    with factory() as s:
        task = s.get(TaskRow, r.json()["task_id"])
        assert task is not None and task.kind == "DISCOVER" and task.entity_type == "run"
        assert task.entity_id.startswith("run")
    assert client.post("/runs/discovery").status_code == 409
    ui = client.post("/ui/runs/discovery")
    assert ui.status_code == 409 and "already queued or running" in ui.text
    with factory() as s:
        s.get(TaskRow, r.json()["task_id"]).status = "RUNNING"  # type: ignore[union-attr]
        s.commit()
    assert client.post("/runs/discovery").status_code == 409
    with factory() as s:
        s.get(TaskRow, r.json()["task_id"]).status = "SUCCEEDED"  # type: ignore[union-attr]
        s.commit()
    ok = client.post("/ui/runs/discovery")
    assert ok.status_code == 200 and "Discovery started" in ok.text


def test_select_requires_matched(factory: sessionmaker[Session], tmp_path: Path) -> None:
    from sqlalchemy import select

    from jobpilot.data.tables import ApplicationRow, AuditEventRow

    client = make_client(factory, tmp_path)
    _seed_job(factory, "NORMALIZED", "job_n1")
    _seed_job(factory, "MATCHED", "job_m1")
    assert client.post("/jobs/job_n1/select").status_code == 409
    assert client.post("/jobs/nope/select").status_code == 404
    r = client.post("/jobs/job_m1/select")
    assert r.status_code == 200
    with factory() as s:
        app = s.execute(select(ApplicationRow)).scalar_one()
        assert (app.job_id, app.status, app.portal) == ("job_m1", "NOT_STARTED", "greenhouse")
        assert app.application_id == r.json()["application_id"]
        assert s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "job_selected")).scalar_one().entity_id == "job_m1"
    assert client.post("/jobs/job_m1/select").status_code == 409
    assert client.post("/ui/jobs/job_m1/select").status_code == 409


def test_select_enqueues_tailor(factory: sessionmaker[Session], tmp_path: Path) -> None:
    from sqlalchemy import select

    from jobpilot.data.tables import TaskRow

    client = make_client(factory, tmp_path)
    _seed_job(factory, "MATCHED", "job_m1")
    app_id = client.post("/jobs/job_m1/select").json()["application_id"]
    with factory() as s:
        tasks = s.execute(select(TaskRow)).scalars().all()
    assert [(t.kind, t.entity_type, t.entity_id) for t in tasks] == [
        ("TAILOR", "application", app_id)]


def test_tailor_endpoints_conflict_when_queued(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    from sqlalchemy import select, update

    from jobpilot.data.tables import TaskRow

    client = make_client(factory, tmp_path)
    _seed_job(factory, "MATCHED", "job_m1")
    app_id = client.post("/jobs/job_m1/select").json()["application_id"]
    assert client.post(f"/applications/{app_id}/tailor").status_code == 409
    assert client.post(f"/ui/applications/{app_id}/tailor").status_code == 409
    assert client.post("/applications/nope/tailor").status_code == 404
    with factory() as s:
        s.execute(update(TaskRow).values(status="SUCCEEDED"))
        s.commit()
    r = client.post(f"/applications/{app_id}/tailor")
    assert r.status_code == 200
    ui = client.post(f"/ui/applications/{app_id}/tailor")
    assert ui.status_code == 409
    with factory() as s:
        queued = s.execute(select(TaskRow).where(TaskRow.status == "QUEUED")).scalars().all()
    assert [t.task_id for t in queued] == [r.json()["task_id"]]


def test_job_detail_renders_scores_and_evidence(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    import json

    from jobpilot.data.tables import CandidateFactRow, MatchAssessmentRow

    _seed_job(factory)
    with factory() as s:
        s.add(CandidateFactRow(fact_id="fact_exp_001", profile_version=1,
                               category="work_experience", field="fact",
                               value="Built a RAG pipeline for Example Corp",
                               verified_by_user=True))
        s.add(MatchAssessmentRow(
            job_id="job_m1", profile_version=1, total_score=81.5, skill_fit=90.0,
            experience_fit=70.0, project_fit=80.0, role_seniority_fit=75.0,
            location_fit=100.0, hard_filter_passed=True, confidence=0.8,
            evidence_json=json.dumps({
                "matched_evidence_fact_ids": ["fact_exp_001"],
                "missing_requirements": ["Kubernetes"], "concerns": ["short tenure"],
                "explanation": "Strong GenAI overlap " + "x" * 200})))
        s.commit()
    client = make_client(factory, tmp_path)
    page = client.get("/ui/jobs/job_m1")
    assert page.status_code == 200
    for text in ("GenAI Engineer", "Example AI", "81.5", "90.0", "100.0",
                 "Built a RAG pipeline for Example Corp", "Kubernetes", "short tenure",
                 "Strong GenAI overlap", 'href="https://example.com/job_m1"',
                 "/ui/jobs/job_m1/select"):
        assert text in page.text, text
    assert client.get("/ui/jobs/nope").status_code == 404
    listing = client.get("/ui/jobs").text
    assert "81.5" in listing and 'href="/ui/jobs/job_m1"' in listing
    assert "x" * 130 not in listing and "Strong GenAI overlap" in listing


def test_jobs_page_has_discover_button(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed_job(factory, "MATCHED", "job_m1")
    _seed_job(factory, "DUPLICATE", "job_d1")
    _seed_job(factory, "REJECTED", "job_r1")
    client = make_client(factory, tmp_path)
    page = client.get("/ui/jobs").text
    assert 'hx-post="/ui/runs/discovery"' in page and ">Discover<" in page
    assert "job_m1" in page and "job_r1" in page and "job_d1" not in page
    assert "/ui/jobs/job_m1/select" in page and "/ui/jobs/job_r1/select" not in page
    assert "job_d1" in client.get("/ui/jobs?show_duplicates=1").text
    only = client.get("/ui/jobs?status=REJECTED").text
    assert "job_r1" in only and "job_m1" not in only
