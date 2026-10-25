"""Select -> documents -> PREPARE_FORM -> user answers -> VALIDATE -> batch -> approve ->
explicit Submit, end to end on the local Greenhouse fixture. The only portal that is ever
contacted is the loopback fixture server."""

import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobpilot.agents.form_prep import unresolved_required
from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.data.tables import (
    ApplicationRow,
    AuditEventRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    TaskRow,
)
from jobpilot.documents.render import chromium_available
from jobpilot.observability.events import EVENT_ORDER
from jobpilot.orchestrator.engine import Orchestrator, TaskKind
from jobpilot.runtime import build_runtime
from tests.agent_contracts.test_form_prep import ALIASES
from tests.e2e.test_discovery_pipeline import FIX, Pipeline, pipe  # noqa: F401
from tests.e2e.test_tailor_pipeline import DocsGateway

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium not installed"),
]

LIVE_BAR = "LIVE SUBMISSION ON for: greenhouse"


async def _run(p: Pipeline, form_server: str, live: list[str]
               ) -> tuple[TestClient, AppConfig, str, str, Orchestrator]:
    """Everything up to and including approval; returns the client, cfg, ids, orchestrator."""
    p.gateway.__class__ = DocsGateway
    cfg = p.cfg.model_copy(update={
        "browser_headless": True, "field_aliases_path": ALIASES, "dry_run": False,
        "live_portals": live})
    rt = build_runtime(cfg, p.factory, client=p.runtime.client)
    orch = Orchestrator(p.factory, cfg, rt.agents, {"simulated": p.adapter},
                        handlers=rt.handlers, gateway=p.gateway,  # type: ignore[arg-type]
                        tools=rt.tools, submitter_factory=rt.submitter_factory)
    shutil.copy(FIX / "profile_example.yaml", cfg.storage_path / "profiles" / "profile.yaml")
    p.import_profile(verify=True)
    await p.discover()
    job = p.jobs()["greenhouse:2001"]
    with p.factory() as s:  # the board's URL is a real portal: use the local form
        row = s.get(JobRow, job.job_id)
        assert row is not None
        row.application_url = f"{form_server}/greenhouse_submit_full.html"
        s.commit()
    client = TestClient(create_app(cfg, p.factory, orch, start_worker=False),
                        base_url="http://127.0.0.1",
                        headers={"origin": f"http://127.0.0.1:{cfg.port}"})
    assert client.post(f"/ui/jobs/{job.job_id}/select").status_code == 200
    with p.factory() as s:
        app_id = s.execute(select(ApplicationRow.application_id)
                           .where(ApplicationRow.job_id == job.job_id)).scalar_one()

    await orch.run_until_idle()  # documents -> PREPARE_FORM
    with p.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "NEEDS_USER_INPUT"
        missing = unresolved_required(s, app_id)
        rows = {r.field_id: r for r in s.execute(select(FormFieldAnswerRow)).scalars()}
    assert rows["question_1003"].decision == "USER_ONLY"
    assert rows["question_1004"].decision == "USER_ONLY"
    data: dict[str, str] = {}
    for fid in missing:
        options = json.loads(rows[fid].options_json)
        data[f"v:{fid}"] = options[0] if options else "Synthetic answer for testing."
    data |= {"seen:question_1004": "1", "v:question_1004": "on"}
    assert client.post(f"/ui/applications/{app_id}/answers", data=data,
                       follow_redirects=False).status_code == 303
    await orch.run_until_idle()  # VALIDATE
    with p.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "READY_FOR_REVIEW", app and app.status

    r = client.post("/ui/batches", follow_redirects=False)
    assert r.status_code == 303
    batch_url = r.headers["location"]
    batch_id = batch_url.rsplit("/", 1)[1]
    h = re.search(r'name="content_hash" value="([^"]+)"', client.get(batch_url).text)
    assert h is not None
    assert client.post(f"/ui/batches/{batch_id}/approve",
                       data={"content_hash": h.group(1), "version": 1,
                             "ids": [app_id]}).status_code == 200
    await orch.run_until_idle()
    with p.factory() as s:
        approved = s.get(ApplicationRow, app_id)
        assert approved is not None and approved.status == "APPROVED"
    return client, cfg, app_id, batch_id, orch


async def test_live_submit_confirmed_on_fixture_portal(
        pipe: Pipeline, form_server: str, form_posts: list[str]) -> None:  # noqa: F811
    client, cfg, app_id, batch_id, orch = await _run(pipe, form_server, ["greenhouse"])
    assert form_posts == []  # approving never submits
    for path in ("/", f"/ui/batches/{batch_id}", f"/ui/applications/{app_id}"):
        assert LIVE_BAR in client.get(path).text
    assert "will submit LIVE to greenhouse" in client.get(f"/ui/batches/{batch_id}").text

    # no typed confirmation -> refused, nothing queued, nothing posted
    r = client.post(f"/ui/batches/{batch_id}/submit")
    assert r.status_code == 422 and "Type SUBMIT to confirm live submission" in r.text
    await orch.run_until_idle()
    assert form_posts == []
    with pipe.factory() as s:
        assert s.execute(select(TaskRow).where(
            TaskRow.kind == TaskKind.SUBMIT.value)).first() is None

    assert client.post(f"/ui/batches/{batch_id}/submit",
                       data={"confirm": "SUBMIT"}).status_code == 200
    await orch.run_until_idle()
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "CONFIRMED"
        assert app.confirmation_reference == "APP-12345"
        shot = s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.application_id == app_id,
            DocumentArtifactRow.type == "submit_confirmation")).scalar_one()
        clicks = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "submit_clicked").order_by(EVENT_ORDER)).scalars().all()
        submits = s.execute(select(TaskRow).where(
            TaskRow.kind == TaskKind.SUBMIT.value)).scalars().all()
    assert (cfg.storage_path / shot.path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert form_posts == ["/submit_ok"]
    assert len(clicks) == 1 and len(submits) == 1 and submits[0].status == "SUCCEEDED"
    assert LIVE_BAR in client.get(f"/ui/applications/{app_id}").text


async def test_dry_run_portal_not_listed_stays_simulated(
        pipe: Pipeline, form_server: str, form_posts: list[str]) -> None:  # noqa: F811
    client, _, app_id, batch_id, orch = await _run(pipe, form_server, [])
    assert LIVE_BAR not in client.get(f"/ui/batches/{batch_id}").text
    assert 'name="confirm"' not in client.get(f"/ui/batches/{batch_id}").text
    assert client.post(f"/ui/batches/{batch_id}/submit").status_code == 200  # no confirm
    await orch.run_until_idle()
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "CONFIRMED"
        clicks = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "submit_clicked")).scalars().all()
        assert s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.type == "submit_confirmation")).first() is None
    assert form_posts == []  # simulated: the fixture never saw a request
    assert clicks == []


async def _rerun_with(p: Pipeline, cfg: AppConfig, live: list[str]) -> None:
    """Restart with live mode flipped and drain the queued SUBMIT."""
    cfg2 = cfg.model_copy(update={"live_portals": live})
    rt = build_runtime(cfg2, p.factory, client=p.runtime.client)
    orch = Orchestrator(p.factory, cfg2, rt.agents, {"simulated": p.adapter},
                        handlers=rt.handlers, gateway=p.gateway,  # type: ignore[arg-type]
                        tools=rt.tools, submitter_factory=rt.submitter_factory)
    await orch.run_until_idle()


def _gate_reasons(p: Pipeline, app_id: str) -> list[str]:
    with p.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "APPROVED"
        task = s.execute(select(TaskRow).where(
            TaskRow.kind == TaskKind.SUBMIT.value)).scalar_one()
        assert task.status == "FAILED"
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "gate_denied")).scalar_one()
        return list(json.loads(ev.redacted_details_json)["reasons"])


async def test_simulated_request_never_runs_live_after_flip(
        pipe: Pipeline, form_server: str, form_posts: list[str]) -> None:  # noqa: F811
    client, cfg, app_id, batch_id, _ = await _run(pipe, form_server, [])
    assert client.post(f"/ui/batches/{batch_id}/submit").status_code == 200
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.submit_live_intent is None
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "submit_requested")).scalar_one()
        assert json.loads(ev.redacted_details_json)["live"] == {app_id: False}
    await _rerun_with(pipe, cfg, ["greenhouse"])  # user flips live on and restarts
    assert _gate_reasons(pipe, app_id) == ["live_intent_missing"]
    assert form_posts == []


async def test_live_request_never_silently_simulated_after_flip(
        pipe: Pipeline, form_server: str, form_posts: list[str]) -> None:  # noqa: F811
    client, cfg, app_id, batch_id, _ = await _run(pipe, form_server, ["greenhouse"])
    assert client.post(f"/ui/batches/{batch_id}/submit",
                       data={"confirm": "SUBMIT"}).status_code == 200
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.submit_live_intent == "greenhouse"
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "submit_requested")).scalar_one()
        assert json.loads(ev.redacted_details_json)["live"] == {app_id: True}
    await _rerun_with(pipe, cfg, [])  # user flips live off and restarts
    assert _gate_reasons(pipe, app_id) == ["live_mode_off"]
    assert form_posts == []


async def test_orphan_requeue_without_live_intent_is_gate_denied(
        pipe: Pipeline, form_server: str, form_posts: list[str]) -> None:  # noqa: F811
    client, cfg, app_id, batch_id, _ = await _run(pipe, form_server, [])
    assert client.post(f"/ui/batches/{batch_id}/submit").status_code == 200
    with pipe.factory() as s:  # the SUBMIT task is gone: only the request remains
        for t in s.execute(select(TaskRow).where(
                TaskRow.kind == TaskKind.SUBMIT.value)).scalars():
            s.delete(t)
        s.commit()
    cfg2 = cfg.model_copy(update={"live_portals": ["greenhouse"]})
    rt = build_runtime(cfg2, pipe.factory, client=pipe.runtime.client)
    orch = Orchestrator(pipe.factory, cfg2, rt.agents, {"simulated": pipe.adapter},
                        handlers=rt.handlers, gateway=pipe.gateway,  # type: ignore[arg-type]
                        tools=rt.tools, submitter_factory=rt.submitter_factory)
    orch.recover_on_startup()  # re-queues the requested submit
    await orch.run_until_idle()
    assert _gate_reasons(pipe, app_id) == ["live_intent_missing"]
    assert form_posts == []
