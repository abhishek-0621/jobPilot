"""Select a MATCHED job -> TAILOR -> COVER_LETTER with real handlers and rendering."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pypdf import PdfReader
from sqlalchemy import select

from jobpilot.api.actions import select_job
from jobpilot.data.tables import ApplicationRow, DocumentArtifactRow
from jobpilot.documents.render import chromium_available
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
        docs = {"resume_tailoring": TAILOR, "cover_letter": LETTER}
        if route in docs:
            self.calls.append(route)
            return schema.model_validate(docs[route]), None
        return await super().generate(route, messages, schema, prompt_version, model_tag)


@pytest.mark.browser
@pytest.mark.skipif(not chromium_available(), reason="Chromium not installed")
async def test_select_to_form_in_progress(pipe: Pipeline) -> None:  # noqa: F811
    pipe.gateway.__class__ = DocsGateway  # same object the orchestrator holds
    shutil.copy(FIX / "profile_example.yaml",
                pipe.cfg.storage_path / "profiles" / "profile.yaml")
    pipe.import_profile(verify=True)
    await pipe.discover()
    job = pipe.jobs()["greenhouse:2001"]
    assert job.status == "MATCHED"
    with pipe.factory() as s:
        app_id = select_job(s, pipe.orch, job.job_id).application_id
    await pipe.orch.run_until_idle()
    with pipe.factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "FORM_IN_PROGRESS"
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
