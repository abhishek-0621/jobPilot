import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig, ModelRoute
from jobpilot.contracts.enums import OutreachStatus
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalBatchRow,
    AuditEventRow,
    JobRow,
    OutreachDraftRow,
)
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator, TaskKind
from jobpilot.portals.simulated import SimulatedAdapter
from tests.e2e.stub_agents import INJECTED_DESCRIPTION, ready_application_ids, stub_orchestrator

_INITIAL = {"application": "NOT_STARTED", "job": "DISCOVERED", "approval_batch": "BUILDING"}


@dataclass
class E2E:
    factory: sessionmaker[Session]
    cfg: AppConfig
    orch: Orchestrator
    adapter: SimulatedAdapter

    @property
    def approvals(self) -> "_CommittingApprovals":
        return _CommittingApprovals(self)

    def ready_apps(self) -> list[str]:
        with self.factory() as s:
            return ready_application_ids(s)

    def status(self, application_id: str) -> str:
        with self.factory() as s:
            return get_application(s, application_id).status

    def outreach_sent_count(self) -> int:
        with self.factory() as s:
            return len(list(s.execute(select(OutreachDraftRow).where(
                OutreachDraftRow.status == OutreachStatus.SENT.value)).scalars()))

    def injected_app(self) -> str:
        with self.factory() as s:
            return s.execute(select(ApplicationRow.application_id).join(
                JobRow, JobRow.job_id == ApplicationRow.job_id
            ).where(JobRow.description_text == INJECTED_DESCRIPTION)).scalar_one()

    def status_changes(self) -> dict[str, str]:
        """Current status of every entity that has left its initial status."""
        out: dict[str, str] = {}
        with self.factory() as s:
            for a in s.execute(select(ApplicationRow)).scalars():
                out[a.application_id] = a.status
            for j in s.execute(select(JobRow)).scalars():
                out[j.job_id] = j.status
            for b in s.execute(select(ApprovalBatchRow)).scalars():
                out[b.batch_id] = b.status
        return {k: v for k, v in out.items() if v not in _INITIAL.values()}

    def status_changed_events(self) -> dict[str, str]:
        """Final status reached by walking each entity's status_changed event chain."""
        edges: dict[str, dict[str, str]] = {}
        types: dict[str, str] = {}
        with self.factory() as s:
            for e in s.execute(select(AuditEventRow).where(
                    AuditEventRow.action == "status_changed")).scalars():
                d = json.loads(e.redacted_details_json)
                chain = edges.setdefault(e.entity_id, {})
                assert d["from"] not in chain, f"repeated status in {e.entity_id}"
                chain[d["from"]] = d["to"]
                types[e.entity_id] = e.entity_type
        final: dict[str, str] = {}
        for entity_id, chain in edges.items():
            cur = _INITIAL[types[entity_id]]
            while cur in chain:
                cur = chain.pop(cur)
            assert not chain, f"disconnected status events for {entity_id}: {chain}"
            final[entity_id] = cur
        return final


class _CommittingApprovals:
    """ApprovalService in its own committed session, enqueueing SUBMIT like the API does."""

    def __init__(self, e2e: E2E) -> None:
        self.e2e = e2e

    def build_batch(self, ids: list[str]) -> ApprovalBatchRow:
        with self.e2e.factory() as s:
            batch = ApprovalService(s).build_batch(ids)
            s.commit()
            return batch

    def approve(self, batch_id: str, version: int, content_hash: str | None,
                ids: list[str]) -> None:
        with self.e2e.factory() as s:
            ApprovalService(s).approve(batch_id, version, content_hash or "", ids)
            s.commit()
        for a in ids:
            self.e2e.orch.enqueue(TaskKind.SUBMIT, "application", a)


@pytest.fixture
def e2e(tmp_path: Path) -> Iterator[E2E]:
    db_path = tmp_path / "e2e.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    factory = make_session_factory(engine)
    cfg = AppConfig(storage_path=tmp_path, db_path=db_path, dry_run=True,
                    model_routes={"job_matching": ModelRoute(model_tag="m1")})
    adapter = SimulatedAdapter()
    yield E2E(factory, cfg, stub_orchestrator(factory, cfg, adapter), adapter)
    engine.dispose()


async def _run_and_approve_first(e2e: E2E) -> list[str]:
    e2e.orch.enqueue(TaskKind.DISCOVER, "run", "run_1")
    await e2e.orch.run_until_idle()
    apps = e2e.ready_apps()
    b = e2e.approvals.build_batch(apps)
    e2e.approvals.approve(b.batch_id, b.version, b.content_hash, apps[:1])
    await e2e.orch.run_until_idle()
    return apps


async def test_dry_run_pipeline(e2e: E2E) -> None:
    e2e.orch.enqueue(TaskKind.DISCOVER, "run", "run_1")
    await e2e.orch.run_until_idle()
    apps = e2e.ready_apps()
    assert len(apps) == 2
    b = e2e.approvals.build_batch(apps)
    e2e.approvals.approve(b.batch_id, b.version, b.content_hash, apps[:1])
    await e2e.orch.run_until_idle()
    assert e2e.status(apps[0]) == "CONFIRMED" and e2e.status(apps[1]) == "READY_FOR_REVIEW"
    assert e2e.adapter.real_submissions == 0 and e2e.adapter.simulated_submissions == 1
    assert e2e.outreach_sent_count() == 0  # no outreach approval -> nothing sent


async def test_injected_description_cannot_submit(e2e: E2E) -> None:
    e2e.orch.enqueue(TaskKind.DISCOVER, "run", "run_1")
    await e2e.orch.run_until_idle()
    injected = e2e.injected_app()
    assert e2e.status(injected) == "READY_FOR_REVIEW"
    # Even a forged SUBMIT task without approval must be gate-denied.
    e2e.orch.enqueue(TaskKind.SUBMIT, "application", injected)
    await e2e.orch.run_until_idle()
    assert e2e.status(injected) == "READY_FOR_REVIEW"
    assert e2e.adapter.submit_calls == 0
    with e2e.factory() as s:
        actions = {e.action for e in s.execute(select(AuditEventRow).where(
            AuditEventRow.entity_id == injected)).scalars()}
    assert "gate_denied" in actions and "submit_started" not in actions
    assert e2e.outreach_sent_count() == 0


@pytest.fixture
async def e2e_after_run(e2e: E2E) -> E2E:
    await _run_and_approve_first(e2e)
    return e2e


def test_every_status_change_has_event(e2e_after_run: E2E) -> None:
    changes = e2e_after_run.status_changes()
    assert "CONFIRMED" in changes.values() and "MATCHED" in changes.values()
    assert changes == e2e_after_run.status_changed_events()


@pytest.fixture
async def e2e_seeded(e2e: E2E) -> E2E:
    await _run_and_approve_first(e2e)
    return e2e


@pytest.fixture
def e2e_client(e2e_seeded: E2E) -> Iterator[TestClient]:
    app = create_app(e2e_seeded.cfg, e2e_seeded.factory, e2e_seeded.orch, start_worker=False)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def batch(e2e_seeded: E2E) -> ApprovalBatchRow:
    remaining = e2e_seeded.ready_apps()
    return e2e_seeded.approvals.build_batch(remaining)


def test_dashboard_overview_renders(e2e_client: TestClient) -> None:
    r = e2e_client.get("/")
    assert r.status_code == 200 and "CONFIRMED" in r.text
    assert 'hx-trigger="every 5s"' in r.text
    assert e2e_client.get("/static/htmx.min.js").status_code == 200


def test_dashboard_pages_render(e2e_client: TestClient) -> None:
    jobs = e2e_client.get("/ui/jobs")
    assert jobs.status_code == 200 and "https://example.com/jobs/1" in jobs.text
    events = e2e_client.get("/ui/events")
    assert events.status_code == 200 and "status_changed" in events.text
    assert "UTC" in events.text
    counts = e2e_client.get("/ui/overview/counts")
    assert counts.status_code == 200 and "CONFIRMED" in counts.text


def test_dashboard_approve_form_rejects_stale_hash(
    e2e_client: TestClient, batch: ApprovalBatchRow
) -> None:
    page = e2e_client.get(f"/ui/batches/{batch.batch_id}")
    assert page.status_code == 200 and (batch.content_hash or "") in page.text
    ids = _batch_ids(e2e_client, batch)
    r = e2e_client.post(f"/ui/batches/{batch.batch_id}/approve",
                        data={"content_hash": "sha256:old", "ids": ids})
    assert r.status_code == 409
    assert "This batch changed since you viewed it" in r.text


def _batch_ids(client: TestClient, batch: ApprovalBatchRow) -> list[str]:
    body = client.get(f"/batches/{batch.batch_id}").json()
    return [i["application_id"] for i in body["items"]]


async def test_dashboard_approve_form_approves_and_queues_submit(
    e2e_seeded: E2E, batch: ApprovalBatchRow
) -> None:
    app = create_app(e2e_seeded.cfg, e2e_seeded.factory, e2e_seeded.orch, start_worker=False)
    with TestClient(app) as client:
        ids = _batch_ids(client, batch)
        r = client.post(f"/ui/batches/{batch.batch_id}/approve",
                        data={"content_hash": batch.content_hash, "version": "1",
                              "ids": ids})
        assert r.status_code == 200 and "Approved 1" in r.text
    await e2e_seeded.orch.run_until_idle()
    assert e2e_seeded.status(ids[0]) == "CONFIRMED"
    assert e2e_seeded.adapter.simulated_submissions == 2


async def test_dashboard_builds_batch_from_ready_apps(e2e: E2E) -> None:
    e2e.orch.enqueue(TaskKind.DISCOVER, "run", "run_1")
    await e2e.orch.run_until_idle()
    app = create_app(e2e.cfg, e2e.factory, e2e.orch, start_worker=False)
    with TestClient(app) as client:
        assert "Build review batch" in client.get("/").text
        r = client.post("/ui/batches", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/ui/batches/batch")
        page = client.get(r.headers["location"])
        assert page.text.count('name="ids"') == 2
        # Both apps are now in a pending batch: nothing left to batch.
        assert client.post("/ui/batches", follow_redirects=False).status_code == 422
