"""Manual job add, apply-mode routing and the manual-apply state edges."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.api.actions import add_manual_job
from jobpilot.contracts.enums import ApplicationStatus as A
from jobpilot.data.tables import JobRow, TaskRow
from jobpilot.orchestrator.engine import TaskKind
from jobpilot.orchestrator.state_machine import TRANSITIONS, IllegalTransition, transition
from jobpilot.portals.registry import apply_mode


class _Orch:
    def enqueue_in(self, s: Session, kind: TaskKind, entity_type: str,
                   entity_id: str) -> str:
        from jobpilot.orchestrator.engine import add_task
        return add_task(s, kind, entity_type, entity_id)


def _job(source: str, url: str, application_url: str | None = None) -> dict[str, Any]:
    return {"source": source, "canonical_url": url, "application_url": application_url}


@pytest.mark.parametrize(("job", "mode"), [
    (_job("greenhouse", "https://boards.greenhouse.io/x/jobs/1"), "automated"),
    (_job("lever", "https://jobs.lever.co/x/abc"), "automated"),
    (_job("ashby", "https://jobs.ashbyhq.com/x/abc"), "automated"),
    (_job("greenhouse", "https://boards.greenhouse.io/x/jobs/1",
          "https://jobs.linkedin.com/apply/1"), "manual"),
    (_job("lever", "https://jobs.lever.co/x/abc", "https://www.naukri.com/job/1"), "manual"),
    (_job("ashby", "https://x.glassdoor.co.in/j"), "manual"),
    (_job("greenhouse", "https://a.myworkdayjobs.com/en/job/1"), "manual"),
    (_job("workday", "https://a.wd1.myworkdayjobs.com/job/1"), "manual"),
    (_job("smartrecruiters", "https://jobs.smartrecruiters.com/x/1"), "manual"),
    (_job("manual", "https://boards.greenhouse.io/x/jobs/1"), "manual"),
    (_job("greenhouse", "https://notlinkedin.com/x"), "automated"),
])
def test_apply_mode(job: dict[str, Any], mode: str) -> None:
    assert apply_mode(job) == mode


def test_apply_mode_accepts_rows() -> None:
    row = JobRow(source="greenhouse", canonical_url="https://www.linkedin.com/jobs/view/1",
                 application_url=None)
    assert apply_mode(row) == "manual"


def test_manual_state_edges() -> None:
    edges = TRANSITIONS[A]
    assert A.MANUAL_APPLY_READY in edges[A.FORM_IN_PROGRESS]
    assert edges[A.MANUAL_APPLY_READY] == {A.SUBMITTED_BY_USER, A.DOCUMENTS_PENDING,
                                           A.REJECTED_BY_USER}
    assert edges[A.SUBMITTED_BY_USER] == frozenset()


def test_submitted_by_user_needs_user_actor(db_session: Session) -> None:
    class Row:
        application_id = "app_1"
        status = A.MANUAL_APPLY_READY.value
    with pytest.raises(IllegalTransition):
        transition(db_session, Row(), A.SUBMITTED_BY_USER, actor="orchestrator", reason="x")
    row = Row()
    transition(db_session, row, A.SUBMITTED_BY_USER, actor="user", reason="marked_applied")
    assert row.status == A.SUBMITTED_BY_USER.value


def _add(s: Session, **kw: Any) -> str:
    args: dict[str, Any] = {"url": "https://www.linkedin.com/jobs/view/123?trk=abc",
                            "title": "GenAI Engineer", "company": "Example AI",
                            "location": "Bengaluru",
                            "description": "<p>Build <b>RAG</b> systems.</p>"} | kw
    return add_manual_job(s, _Orch(), **args)  # type: ignore[arg-type]


def test_add_manual_job_validation(db_session: Session) -> None:
    for bad in ("javascript:alert(1)", "ftp://x.com/a", "not a url", ""):
        with pytest.raises(ValueError):
            _add(db_session, url=bad)
    for field in ("title", "company"):
        with pytest.raises(ValueError):
            _add(db_session, **{field: "  "})
    job_id = _add(db_session)
    db_session.commit()
    job = db_session.get(JobRow, job_id)
    assert job is not None
    assert job.source == "manual" and job.status == "DISCOVERED"
    assert job.canonical_url == "https://linkedin.com/jobs/view/123"
    assert job.application_url == job.canonical_url
    assert job.description_text == "Build RAG systems."
    assert job.source_job_id and len(job.source_job_id) == 16
    tasks = db_session.execute(select(TaskRow)).scalars().all()
    assert [(t.kind, t.entity_id) for t in tasks] == [(TaskKind.EXTRACT.value, job_id)]
    # dedupe: the same URL (tracking params differ) returns the same job, nothing queued
    again = _add(db_session, url="https://linkedin.com/jobs/view/123/?utm_source=x")
    assert again == job_id
    # same company/title/location under another URL: a duplicate of the canonical job
    dup = _add(db_session, url="https://example.com/careers/genai")
    assert dup == job_id
    db_session.commit()
    assert len(db_session.execute(select(TaskRow)).scalars().all()) == 1
