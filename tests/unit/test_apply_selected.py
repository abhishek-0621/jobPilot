"""Apply to selected: bulk start from the Jobs page and the Ready to submit view.

This feature only starts preparation (TAILOR). It never approves or submits."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api import actions
from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import upsert_job
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalBatchRow,
    ApprovalRow,
    BatchItemRow,
    JobRow,
    TaskRow,
)
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import make_app_row, make_listing, seed_ready_apps


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "apply.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> TestClient:
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1")


def _matched(s: Session, key: str, **kw: Any) -> str:
    row, _ = upsert_job(s, make_listing(job_id=key, source_job_id=key, company=f"Co {key}",
                                        title=f"Role {key}",
                                        canonical_url=f"https://example.com/{key}", **kw))
    row.status = "MATCHED"
    s.flush()
    return row.job_id


def _jobs(factory: sessionmaker[Session], *keys: str, **kw: Any) -> list[str]:
    with factory() as s:
        ids = [_matched(s, k, **kw) for k in keys]
        s.commit()
        return ids


def _tasks(factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    with factory() as s:
        return [(t.kind, t.entity_id) for t in s.execute(select(TaskRow)).scalars()]


def _apps(factory: sessionmaker[Session]) -> dict[str, ApplicationRow]:
    with factory() as s:
        return {a.job_id: a for a in s.execute(select(ApplicationRow)).scalars()}


def test_bulk_start_creates_apps_and_enqueues_tailor_only(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    ids = _jobs(factory, "a", "b", "c")
    r = client.post("/ui/jobs/apply", data={"job_ids": ids}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Started 3" in r.text and "Skipped 0" in r.text and "Failed 0" in r.text
    apps = _apps(factory)
    assert set(apps) == set(ids)
    assert all(a.status == "NOT_STARTED" for a in apps.values())
    tasks = _tasks(factory)
    assert sorted(tasks) == sorted(("TAILOR", a.application_id) for a in apps.values())
    assert not any(kind == "SUBMIT" for kind, _ in tasks)
    with factory() as s:
        assert s.execute(select(ApprovalBatchRow)).first() is None
        assert s.execute(select(ApprovalRow)).first() is None


def test_manual_job_still_starts(client: TestClient, factory: sessionmaker[Session]) -> None:
    (jid,) = _jobs(factory, "m", source="manual")
    r = client.post("/ui/jobs/apply", data={"job_ids": [jid]})
    assert r.status_code == 200 and "Started 1" in r.text
    assert jid in _apps(factory)


def test_skips_job_with_application_in_progress(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    fresh, busy = _jobs(factory, "fresh", "busy")
    with factory() as s:
        s.add(ApplicationRow(application_id="app_busy", job_id=busy, status="FORM_IN_PROGRESS"))
        s.commit()
    r = client.post("/ui/jobs/apply", data={"job_ids": [fresh, busy]})
    assert r.status_code == 200
    assert "Started 1" in r.text and "Skipped 1" in r.text
    assert "already in progress" in r.text
    assert [k for k, e in _tasks(factory)] == ["TAILOR"]
    assert _apps(factory)[busy].status == "FORM_IN_PROGRESS"


def test_partial_failure_is_isolated(client: TestClient, factory: sessionmaker[Session],
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    good1, bad, good2 = _jobs(factory, "g1", "bad", "g2")
    real = actions.select_job

    def flaky(s: Session, orch: Orchestrator, job_id: str) -> ApplicationRow:
        if job_id == bad:
            s.add(ApplicationRow(application_id="app_half", job_id=bad, status="NOT_STARTED"))
            s.flush()
            raise RuntimeError("boom")
        return real(s, orch, job_id)

    monkeypatch.setattr(actions, "select_job", flaky)
    r = client.post("/ui/jobs/apply", data={"job_ids": [good1, bad, good2, "job_missing"]})
    assert r.status_code == 200
    assert "Started 2" in r.text and "Failed 2" in r.text
    apps = _apps(factory)
    assert set(apps) == {good1, good2}  # the half-written row was rolled back
    assert len(_tasks(factory)) == 2


def test_more_than_50_ids_rejected(client: TestClient, factory: sessionmaker[Session]) -> None:
    r = client.post("/ui/jobs/apply", data={"job_ids": [f"job_{i}" for i in range(51)]})
    assert r.status_code == 422
    assert _tasks(factory) == []


@pytest.mark.parametrize("ids", [[], ["bad id"], ["<script>"], ["x" * 200]])
def test_invalid_ids_rejected(client: TestClient, factory: sessionmaker[Session],
                              ids: list[str]) -> None:
    r = client.post("/ui/jobs/apply", data={"job_ids": ids})
    assert r.status_code == 422
    assert "<script>" not in r.text
    assert _tasks(factory) == []


def test_bulk_start_rejects_cross_origin(client: TestClient,
                                         factory: sessionmaker[Session]) -> None:
    ids = _jobs(factory, "x")
    r = client.post("/ui/jobs/apply", data={"job_ids": ids},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/ui/jobs/apply", data={"job_ids": ids},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert _tasks(factory) == [] and _apps(factory) == {}


def test_jobs_page_has_checkboxes_and_action_bar(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    (jid,) = _jobs(factory, "row")
    page = client.get("/ui/jobs").text
    assert f'name="job_ids" value="{jid}"' in page
    assert 'id="apply-form"' in page and "Start applications" in page
    assert 'id="select-all"' in page


def test_job_detail_has_start_application_button(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    (jid,) = _jobs(factory, "detail")
    page = client.get(f"/ui/jobs/{jid}").text
    assert 'hx-post="/ui/jobs/apply"' in page and "Start application" in page
    assert f'value="{jid}"' in page


# ---- Ready to submit ---------------------------------------------------------

def test_ready_page_lists_ready_and_manual(client: TestClient,
                                           factory: sessionmaker[Session]) -> None:
    with factory() as s:
        (ready,) = seed_ready_apps(s, 1)
        manual = make_app_row(s, status="MANUAL_APPLY_READY").application_id
        s.commit()
    page = client.get("/ui/ready").text
    assert f'name="application_ids" value="{ready}"' in page
    assert "Review and approve selected" in page
    assert f"/ui/applications/{manual}/apply" in page
    assert f'value="{manual}"' not in page


def test_batch_from_selected_builds_batch_and_redirects(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        a, b = seed_ready_apps(s, 2)
        s.commit()
    r = client.post("/ui/ready/batch", data={"application_ids": [a]},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui/batches/")
    with factory() as s:
        items = list(s.execute(select(BatchItemRow)).scalars())
        assert [i.application_id for i in items] == [a]
        assert not any(i.selected for i in items)  # nothing approved
        assert s.execute(select(ApprovalRow)).first() is None
        statuses = {x.application_id: x.status for x in s.execute(select(ApplicationRow))
                    .scalars()}
        assert statuses[a] == statuses[b] == "READY_FOR_REVIEW"
    assert not any(k == "SUBMIT" for k, _ in _tasks(factory))


@pytest.mark.parametrize("status", ["FORM_IN_PROGRESS", "MANUAL_APPLY_READY", "APPROVED"])
def test_batch_from_selected_only_accepts_ready(
        client: TestClient, factory: sessionmaker[Session], status: str) -> None:
    with factory() as s:
        (ready,) = seed_ready_apps(s, 1)
        other = make_app_row(s, status=status).application_id
        s.commit()
    r = client.post("/ui/ready/batch", data={"application_ids": [ready, other]},
                    follow_redirects=False)
    assert r.status_code == 422
    with factory() as s:
        assert s.execute(select(ApprovalBatchRow)).first() is None


def test_batch_from_selected_rejects_already_batched_and_bad_input(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        (ready,) = seed_ready_apps(s, 1)
        s.commit()
    assert client.post("/ui/ready/batch", data={"application_ids": [ready]},
                       follow_redirects=False).status_code == 303
    # already in a pending batch
    assert client.post("/ui/ready/batch", data={"application_ids": [ready]},
                       follow_redirects=False).status_code == 422
    assert client.post("/ui/ready/batch", data={"application_ids": []}).status_code == 422
    too_many = [f"app_{i}" for i in range(51)]
    assert client.post("/ui/ready/batch",
                       data={"application_ids": too_many}).status_code == 422
    r = client.post("/ui/ready/batch", data={"application_ids": [ready]},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    with factory() as s:
        assert len(list(s.execute(select(ApprovalBatchRow)).scalars())) == 1


def test_job_rows_unchanged_by_failed_start(client: TestClient,
                                            factory: sessionmaker[Session]) -> None:
    with factory() as s:
        row, _ = upsert_job(s, make_listing(job_id="n", source_job_id="n",
                                            canonical_url="https://example.com/n"))
        jid = row.job_id  # not MATCHED
        s.commit()
    r = client.post("/ui/jobs/apply", data={"job_ids": [jid]})
    assert r.status_code == 200 and "Started 0" in r.text
    with factory() as s:
        job = s.get(JobRow, jid)
        assert job is not None and job.status != "MATCHED"
    assert _apps(factory) == {}
