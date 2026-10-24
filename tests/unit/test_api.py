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
    return TestClient(create_app(cfg, factory, orch, gateway, start_worker=False))


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
    with TestClient(create_app(cfg, factory, orch)) as client:
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
