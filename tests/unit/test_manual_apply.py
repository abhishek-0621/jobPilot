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
    assert edges[A.SUBMITTED_BY_USER] == frozenset({A.DELETED})


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


def _manual_app(s: Session, status: A = A.MANUAL_APPLY_READY) -> str:
    from tests.factories import make_app_row
    return make_app_row(s, status=status.value).application_id


def test_mark_applied_rules(db_session: Session) -> None:
    from datetime import timedelta

    from jobpilot.api.actions import ActionConflict, mark_applied
    from jobpilot.data.db import utcnow
    from jobpilot.data.tables import ApplicationRow, AuditEventRow

    app_id = _manual_app(db_session)
    db_session.commit()
    for bad in ("x" * 101,):
        with pytest.raises(ValueError):
            mark_applied(db_session, app_id, bad, None)
    with pytest.raises(ValueError):
        mark_applied(db_session, app_id, None, (utcnow() + timedelta(days=3)).date())
    db_session.rollback()
    assert db_session.get(ApplicationRow, app_id).status == A.MANUAL_APPLY_READY.value  # type: ignore[union-attr]
    day = (utcnow() - timedelta(days=2)).date()
    mark_applied(db_session, app_id, "  REF-42  ", day)
    app = db_session.get(ApplicationRow, app_id)
    assert app is not None and app.status == A.SUBMITTED_BY_USER.value
    assert app.confirmation_reference == "REF-42"
    ev = db_session.execute(select(AuditEventRow).where(
        AuditEventRow.action == "user_marked_applied")).scalar_one()
    assert ev.actor == "user" and ev.entity_id == app_id
    assert "REF-42" not in ev.redacted_details_json
    assert day.isoformat() in ev.redacted_details_json
    assert '"has_reference": true' in ev.redacted_details_json or \
        '"has_reference":true' in ev.redacted_details_json
    with pytest.raises(ActionConflict):  # twice
        mark_applied(db_session, app_id, None, None)
    db_session.rollback()
    for status in (A.NOT_STARTED, A.READY_FOR_REVIEW, A.CONFIRMED):
        other = _manual_app(db_session, status)
        with pytest.raises(ActionConflict):
            mark_applied(db_session, other, None, None)
        db_session.rollback()
    with pytest.raises(LookupError):
        mark_applied(db_session, "app_missing", None, None)
    blank = _manual_app(db_session)
    mark_applied(db_session, blank, "   ", None)
    row = db_session.get(ApplicationRow, blank)
    assert row is not None and row.confirmation_reference is None


def test_first_paragraph_skips_greeting() -> None:
    from jobpilot.agents.manual_package import first_paragraph
    assert first_paragraph("Dear X,\n\nBody one.\n\nBye") == "Body one."
    assert first_paragraph("Dear X,\n\nBody only.") == "Body only."
    assert first_paragraph("Single") == "Single"
    assert first_paragraph(None) is None


def test_form_url_refuses_manual_apply_mode() -> None:
    from jobpilot.portals.registry import PortalConfigError, form_url
    manual = {"source": "greenhouse", "canonical_url": "https://boards.greenhouse.io/x/jobs/1",
              "application_url": "https://jobs.linkedin.com/apply/1"}
    with pytest.raises(PortalConfigError) as e:
        form_url(manual)
    assert e.value.code == "manual_apply_mode"
    ok = {"source": "greenhouse", "canonical_url": "https://boards.greenhouse.io/x/jobs/1",
          "application_url": "https://boards.greenhouse.io/x/jobs/1#app"}
    assert form_url(ok) == "https://boards.greenhouse.io/x/jobs/1#app"


async def test_live_submitter_refuses_manual_app_before_browser(
        db_session: Session, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobpilot.config import AppConfig
    from jobpilot.portals import live_submit
    from jobpilot.portals.base import ApprovedApplication
    from jobpilot.portals.live_submit import LiveSubmitter, SubmitContext
    from jobpilot.portals.registry import PortalConfigError

    opened: list[str] = []

    def spy(*a: Any, **k: Any) -> Any:
        opened.append("browser")
        raise AssertionError("browser opened")

    monkeypatch.setattr(live_submit, "portal_page", spy)
    app = {"source": "greenhouse", "canonical_url": "https://boards.greenhouse.io/x/jobs/1",
           "application_url": "https://www.linkedin.com/jobs/view/1"}
    ctx = SubmitContext(app=app, answers=[], application_hash="h", active_version=1,
                        documents_intact=True)
    sub = LiveSubmitter(AppConfig(storage_path=tmp_path / "st", model_routes={}),
                        "greenhouse", lambda _id: ctx, lambda a, k: None,
                        lambda: db_session)  # type: ignore[arg-type]
    approved = ApprovedApplication.model_construct(
        application_id="app_1", approved_hash="h", idempotency_key="k")
    with pytest.raises(PortalConfigError) as e:
        await sub.submit(approved)
    assert e.value.code == "manual_apply_mode"
    assert opened == []


def test_add_manual_job_matched_existing_event(db_session: Session) -> None:
    from datetime import UTC, datetime

    from jobpilot.contracts.models import JobListing
    from jobpilot.data.repositories import upsert_job
    from jobpilot.data.tables import AuditEventRow
    now = datetime.now(UTC)
    row, _ = upsert_job(db_session, JobListing(
        task_id="t", created_at=now, job_id="", source="greenhouse", source_job_id="1",
        canonical_url="https://example.com/careers/genai", application_url=None,
        company="Example AI", title="GenAI Engineer", location="Bengaluru",
        description_text="x", first_seen_at=now, last_seen_at=now,
        extraction_confidence=0.0))
    db_session.commit()
    job_id = _add(db_session, url="https://example.com/careers/genai?utm_source=q")
    assert job_id == row.job_id
    events = db_session.execute(select(AuditEventRow).where(
        AuditEventRow.action == "job_added_manually")).scalars().all()
    assert len(events) == 1
    assert '"matched_existing":true' in events[0].redacted_details_json.replace(" ", "")
    assert '"existing_source":"greenhouse"' in events[0].redacted_details_json.replace(" ", "")
    assert "example.com" not in events[0].redacted_details_json

