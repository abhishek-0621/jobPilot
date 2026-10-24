"""Select a MATCHED job -> TAILOR -> COVER_LETTER with real handlers and rendering."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pypdf import PdfReader
from sqlalchemy import select

from jobpilot.agents.form_prep import make_form_opener
from jobpilot.api.actions import select_job
from jobpilot.data.tables import ApplicationRow, DocumentArtifactRow, JobRow, TaskRow
from jobpilot.documents.render import chromium_available
from jobpilot.orchestrator.engine import TaskKind, TaskStatus
from tests.agent_contracts.test_form_prep import ALIASES
from tests.e2e.test_discovery_pipeline import FIX, FakeGateway, Pipeline, pipe  # noqa: F401

TAILOR: dict[str, Any] = {
    "summary": "Built a retrieval pipeline serving 10k queries per day and a document "
               "question answering assistant.",
    "selected_fact_ids": ["fact_proj_001", "fact_exp_001"],
    "bullet_rewrites": [{"fact_id": "fact_exp_001",
                         "text": "Built a retrieval pipeline that serves 10k queries per day"}],
    "skills_order": ["RAG", "Python"],
    "priority": ["fact_exp_001", "fact_proj_001"],
}
LETTER: dict[str, Any] = {
    "paragraphs": [
        "I am applying for the GenAI Engineer role at Example AI.",
        "I built a retrieval pipeline serving 10k queries per day and a document "
        "question answering assistant.",
    ],
    "evidence_fact_ids": ["fact_exp_001", "fact_proj_001"],
}


class DocsGateway(FakeGateway):
    async def generate(self, route: str, messages: list[dict], schema: type[BaseModel],
                       prompt_version: str, model_tag: str | None = None
                       ) -> tuple[BaseModel, None]:
        docs = {"resume_tailoring": TAILOR, "cover_letter": LETTER,
                "form_mapping": {"suggestions": []}}  # keys only; none suggested
        if route in docs:
            self.calls.append(route)
            return schema.model_validate(docs[route]), None
        return await super().generate(route, messages, schema, prompt_version, model_tag)


@pytest.mark.browser
@pytest.mark.skipif(not chromium_available(), reason="Chromium not installed")
async def test_select_to_form_prepared(pipe: Pipeline,  # noqa: F811
                                       form_server: str) -> None:
    pipe.gateway.__class__ = DocsGateway  # same object the orchestrator holds
    headless = pipe.cfg.model_copy(update={"browser_headless": True,
                                           "field_aliases_path": ALIASES})
    pipe.runtime.tools["form_opener"] = make_form_opener(headless)
    shutil.copy(FIX / "profile_example.yaml",
                pipe.cfg.storage_path / "profiles" / "profile.yaml")
    pipe.import_profile(verify=True)
    await pipe.discover()
    job = pipe.jobs()["greenhouse:2001"]
    assert job.status == "MATCHED"
    with pipe.factory() as s:  # the fixture board's URL is a real portal: use the local form
        row = s.get(JobRow, job.job_id)
        assert row is not None
        row.application_url = f"{form_server}/greenhouse.html"
        s.commit()
    with pipe.factory() as s:
        app_id = select_job(s, pipe.orch, job.job_id).application_id
    await pipe.orch.run_until_idle()
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        # documents done -> PREPARE_FORM read the local form; work authorization is the
        # user's to answer, so the application waits for them
        assert app is not None and app.status == "NEEDS_USER_INPUT"
        assert app.form_snapshot_hash
        arts = {a.type: a for a in s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.application_id == app_id)).scalars()}
    assert {"resume", "resume_docx", "resume_diff", "cover_letter",
            "cover_letter_text"} <= set(arts)
    assert app.resume_artifact_id == arts["resume"].artifact_id
    assert app.cover_letter_artifact_id == arts["cover_letter"].artifact_id
    resume = pipe.cfg.storage_path / arts["resume"].path
    assert len(PdfReader(resume).pages) == 1
    assert len(PdfReader(pipe.cfg.storage_path / arts["cover_letter"].path).pages) == 1
    assert Path(resume).resolve().is_relative_to(
        (pipe.cfg.storage_path / "artifacts").resolve())
    assert pipe.gateway.calls.count("resume_tailoring") == 1
    assert pipe.gateway.calls.count("cover_letter") == 1
    assert pipe.cfg.dry_run is True and pipe.adapter.submit_calls == 0
    with pipe.factory() as s:
        prep = s.execute(select(TaskRow).where(
            TaskRow.kind == TaskKind.PREPARE_FORM.value,
            TaskRow.entity_id == app_id)).scalars().all()
    assert [p.status for p in prep] == [TaskStatus.SUCCEEDED.value]
