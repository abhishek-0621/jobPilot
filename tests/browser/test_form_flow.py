"""PREPARE_FORM -> user answers -> VALIDATE -> READY_FOR_REVIEW on the local Greenhouse
fixture in real Chromium. The fixture's submit button records any click; none happens."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobpilot.agents.form_prep import make_form_opener, unresolved_required
from jobpilot.api.main import create_app
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import ApplicationRow, DocumentArtifactRow, FormFieldAnswerRow
from jobpilot.documents.render import chromium_available
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator, TaskKind
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.runtime import build_runtime
from tests.agent_contracts.test_form_prep import make_cfg, seed_application

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium not installed"),
]


async def test_greenhouse_prepare_answer_validate(form_server: str, tmp_path: Path) -> None:
    db = tmp_path / "flow.db"
    upgrade_to_head(db)
    factory = make_session_factory(make_engine(db))
    cfg = make_cfg(tmp_path)
    app_id = seed_application(factory, cfg, f"{form_server}/greenhouse.html")
    rt = build_runtime(cfg, factory)
    real = make_form_opener(cfg)
    submitted: list[bool] = []

    @asynccontextmanager
    async def watched(source: str, url: str, application_id: str) -> AsyncIterator[Any]:
        async with real(source, url, application_id) as adapter:
            yield adapter
            submitted.append(await adapter.page.evaluate("window.__submitted === true"))

    rt.tools["form_opener"] = watched
    orch = Orchestrator(factory, cfg, rt.agents, {"simulated": SimulatedAdapter()},
                        handlers=rt.handlers, tools=rt.tools)

    orch.enqueue(TaskKind.PREPARE_FORM, "application", app_id)
    await orch.run_once()
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "NEEDS_USER_INPUT"
        missing = unresolved_required(s, app_id)
        assert "question_1003" in missing  # work authorization: the user answers it
        rows = {r.field_id: r for r in s.execute(select(FormFieldAnswerRow)).scalars()}
    assert rows["question_1003"].decision == "USER_ONLY"
    assert rows["job_application[resume]"].decision == "AUTO_FILL"

    data: dict[str, str] = {}
    for fid in missing:
        options = json.loads(rows[fid].options_json)
        data[f"v:{fid}"] = options[0] if options else "Synthetic answer for testing."
    data |= {"seen:question_1004": "1", "v:question_1004": "on"}  # tick the declaration
    client = TestClient(create_app(cfg, factory, orch, start_worker=False),
                        base_url="http://127.0.0.1")
    assert client.post(f"/ui/applications/{app_id}/answers", data=data,
                       follow_redirects=False).status_code == 303

    await orch.run_once()  # VALIDATE
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "READY_FOR_REVIEW", app.status
        assert app.content_hash == ApprovalService(s).application_hash(app_id)
        shot = s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.type == "form_screenshot")).scalar_one()
    assert (cfg.storage_path / shot.path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert submitted == [False, False]  # both runs closed without a submit click
