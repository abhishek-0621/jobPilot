"""Select -> documents -> PREPARE_FORM -> user answers -> VALIDATE -> batch -> approve
(simulated), end to end on the local Greenhouse fixture. Nothing is ever submitted."""

import json
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobpilot.agents.form_prep import make_form_opener, unresolved_required
from jobpilot.api.main import create_app
from jobpilot.data.tables import (
    ApplicationRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    TaskRow,
)
from jobpilot.documents.render import chromium_available
from jobpilot.orchestrator.engine import TaskKind
from tests.agent_contracts.test_form_prep import ALIASES
from tests.e2e.test_discovery_pipeline import FIX, Pipeline, pipe  # noqa: F401
from tests.e2e.test_tailor_pipeline import DocsGateway

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium not installed"),
]


async def test_prepare_answer_review_approve_simulated(
        pipe: Pipeline, form_server: str, form_posts: list[str]) -> None:  # noqa: F811
    pipe.gateway.__class__ = DocsGateway
    cfg = pipe.cfg.model_copy(update={"browser_headless": True,
                                      "field_aliases_path": ALIASES})
    real = make_form_opener(cfg)
    closed: list[dict[str, Any]] = []

    @asynccontextmanager
    async def watched(source: str, url: str, application_id: str) -> AsyncIterator[Any]:
        async with real(source, url, application_id) as adapter:
            try:
                yield adapter
            finally:
                closed.append(await adapter.page.evaluate(
                    """() => ({submitted: window.__submitted === true,
                               resume: [...document.querySelector('#resume').files]
                                       .map(f => f.name)})"""))

    pipe.runtime.tools["form_opener"] = watched
    shutil.copy(FIX / "profile_example.yaml", cfg.storage_path / "profiles" / "profile.yaml")
    pipe.import_profile(verify=True)
    await pipe.discover()
    job = pipe.jobs()["greenhouse:2001"]
    assert job.status == "MATCHED"
    with pipe.factory() as s:  # the board's URL is a real portal: use the local form
        row = s.get(JobRow, job.job_id)
        assert row is not None
        row.application_url = f"{form_server}/greenhouse.html"
        s.commit()

    origin = {"origin": f"http://127.0.0.1:{cfg.port}"}
    client = TestClient(create_app(cfg, pipe.factory, pipe.orch, start_worker=False),
                        base_url="http://127.0.0.1", headers=origin)
    assert client.post(f"/ui/jobs/{job.job_id}/select").status_code == 200
    with pipe.factory() as s:
        app_id = s.execute(select(ApplicationRow.application_id)
                           .where(ApplicationRow.job_id == job.job_id)).scalar_one()

    # documents -> PREPARE_FORM: work authorization and the declaration are the user's
    await pipe.orch.run_until_idle()
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "NEEDS_USER_INPUT"
        missing = unresolved_required(s, app_id)
        rows = {r.field_id: r for r in s.execute(select(FormFieldAnswerRow)).scalars()}
    assert "question_1003" in missing
    assert rows["question_1003"].decision == "USER_ONLY"
    assert "question_1004" in rows and rows["question_1004"].decision == "USER_ONLY"
    page = client.get(f"/ui/applications/{app_id}/answers").text
    assert "Are you legally authorized" in page

    data: dict[str, str] = {}
    for fid in missing:
        options = json.loads(rows[fid].options_json)
        data[f"v:{fid}"] = options[0] if options else "Synthetic answer for testing."
    data |= {"seen:question_1004": "1", "v:question_1004": "on"}  # the user ticks the box
    assert client.post(f"/ui/applications/{app_id}/answers", data=data,
                       follow_redirects=False).status_code == 303

    await pipe.orch.run_until_idle()  # VALIDATE
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "READY_FOR_REVIEW", app and app.status
        shot = s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.application_id == app_id,
            DocumentArtifactRow.type == "form_screenshot")).scalar_one()
        resume = s.get(DocumentArtifactRow, app.resume_artifact_id)
        assert resume is not None
    assert (cfg.storage_path / shot.path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert closed[-1] == {"submitted": False, "resume": [resume.path.rsplit("/", 1)[-1]]}
    assert all(c["submitted"] is False for c in closed)

    # batch -> approve with the hash that was presented -> simulated submission
    r = client.post("/ui/batches", follow_redirects=False)
    assert r.status_code == 303
    batch_url = r.headers["location"]
    batch_id = batch_url.rsplit("/", 1)[1]
    page = client.get(batch_url).text
    h = re.search(r'name="content_hash" value="([^"]+)"', page)
    assert h is not None and f'value="{app_id}" checked' in page
    assert client.post(f"/ui/batches/{batch_id}/approve",
                       data={"content_hash": h.group(1), "version": 1,
                             "ids": [app_id]}).status_code == 200
    await pipe.orch.run_until_idle()
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "CONFIRMED"
        submits = s.execute(select(TaskRow).where(
            TaskRow.kind == TaskKind.SUBMIT.value)).scalars().all()
    assert len(submits) == 1 and submits[0].status == "SUCCEEDED"
    assert pipe.cfg.dry_run is True
    assert form_posts == []  # no request ever reached the fixture's submit endpoint
