import json

import pytest

from jobpilot.contracts.enums import (
    AgentRunStatus,
    ApplicationStatus,
    BatchStatus,
    JobStatus,
    OutreachStatus,
)
from jobpilot.data.repositories import list_events
from jobpilot.data.tables import JobRow
from jobpilot.orchestrator.state_machine import TRANSITIONS, IllegalTransition, transition
from tests.factories import make_app_row


@pytest.mark.parametrize("src,dst", [("APPROVED", "SUBMITTED"),
                                     ("READY_FOR_REVIEW", "SUBMISSION_IN_PROGRESS"),
                                     ("SUBMISSION_UNCERTAIN", "SUBMISSION_IN_PROGRESS")])
def test_illegal_application_transitions(db_session, src, dst):
    app = make_app_row(db_session, status=src)
    with pytest.raises(IllegalTransition):
        transition(db_session, app, ApplicationStatus(dst), actor="orchestrator", reason="t")
    assert app.status == src


def test_legal_transition_writes_event(db_session):
    app = make_app_row(db_session, status="APPROVED")
    transition(db_session, app, ApplicationStatus.SUBMISSION_IN_PROGRESS,
               actor="orchestrator", reason="t", details={"k": "v"})
    assert app.status == "SUBMISSION_IN_PROGRESS"
    ev = list_events(db_session, app.application_id)[-1]
    d = json.loads(ev.redacted_details_json)
    assert d["to"] == "SUBMISSION_IN_PROGRESS"
    assert d["from"] == "APPROVED" and d["reason"] == "t" and d["k"] == "v"
    assert ev.action == "status_changed" and ev.entity_type == "application"


def test_uncertain_exit_requires_user_or_verification(db_session):
    app = make_app_row(db_session, status="SUBMISSION_UNCERTAIN")
    with pytest.raises(IllegalTransition):
        transition(db_session, app, ApplicationStatus.CONFIRMED, actor="orchestrator", reason="t")
    with pytest.raises(IllegalTransition):
        transition(db_session, app, ApplicationStatus.SUBMISSION_FAILED, actor="agent", reason="t")
    transition(db_session, app, ApplicationStatus.CONFIRMED, actor="user", reason="t")
    assert app.status == "CONFIRMED"
    app2 = make_app_row(db_session, status="SUBMISSION_UNCERTAIN")
    transition(db_session, app2, ApplicationStatus.SUBMISSION_FAILED,
               actor="verification", reason="t")


def test_terminal_states_have_no_exits():
    app = TRANSITIONS[ApplicationStatus]
    # the only exit from a finished application is deleting its data
    for s in (ApplicationStatus.CONFIRMED, ApplicationStatus.REJECTED_BY_USER):
        assert app[s] == frozenset({ApplicationStatus.DELETED})
    assert app[ApplicationStatus.DELETED] == frozenset()
    # SUBMISSION_FAILED: the user's explicit "Prepare again", or delete
    assert app[ApplicationStatus.SUBMISSION_FAILED] == frozenset(
        {ApplicationStatus.FORM_IN_PROGRESS, ApplicationStatus.DELETED})
    assert TRANSITIONS[OutreachStatus][OutreachStatus.SENT] == frozenset()


def test_every_enum_member_has_an_entry():
    for enum in (JobStatus, ApplicationStatus, BatchStatus, OutreachStatus, AgentRunStatus):
        assert set(TRANSITIONS[enum]) == set(enum)


def test_non_terminal_pre_submission_can_be_rejected_by_user(db_session):
    for s in ("NOT_STARTED", "VALIDATING", "READY_FOR_REVIEW", "APPROVED"):
        app = make_app_row(db_session, status=s)
        transition(db_session, app, ApplicationStatus.REJECTED_BY_USER,
                   actor="user", reason="t")
    app = make_app_row(db_session, status="SUBMITTED")
    with pytest.raises(IllegalTransition):
        transition(db_session, app, ApplicationStatus.REJECTED_BY_USER, actor="user", reason="t")


def test_other_entities_and_wrong_enum_type(db_session):
    app = make_app_row(db_session, status="NOT_STARTED")
    job = db_session.get(JobRow, app.job_id)
    assert job.status == "DISCOVERED"
    transition(db_session, job, JobStatus.NORMALIZED, actor="orchestrator", reason="t")
    with pytest.raises(IllegalTransition):
        transition(db_session, job, JobStatus.DISCOVERED, actor="orchestrator", reason="t")
    ev = list_events(db_session, job.job_id)[0]
    assert ev.entity_type == "job"


def test_outreach_and_batch_edges():
    o = TRANSITIONS[OutreachStatus]
    assert OutreachStatus.DRAFT_READY in o[OutreachStatus.NEEDS_REVIEW]
    assert OutreachStatus.SKIPPED in o[OutreachStatus.DRAFT_READY]
    assert OutreachStatus.SKIPPED not in o[OutreachStatus.SENT]
    assert BatchStatus.PROCESSING in TRANSITIONS[BatchStatus][BatchStatus.APPROVED]
    assert AgentRunStatus.RUNNING in TRANSITIONS[AgentRunStatus][AgentRunStatus.WAITING_FOR_USER]


def test_regenerate_edges_back_to_documents_pending():
    A = ApplicationStatus  # noqa: N806
    edges = TRANSITIONS[ApplicationStatus]
    for src in (A.FORM_IN_PROGRESS, A.NEEDS_USER_INPUT, A.VALIDATION_FAILED,
                A.READY_FOR_REVIEW):
        assert A.DOCUMENTS_PENDING in edges[src]
    for src in (A.APPROVED, A.VALIDATING, A.SUBMITTED, A.SUBMISSION_IN_PROGRESS):
        assert A.DOCUMENTS_PENDING not in edges[src]


@pytest.mark.parametrize("dst", ["READY_FOR_REVIEW", "NEEDS_USER_INPUT"])
def test_presubmit_exit_only_for_presubmit_and_only_before_click(db_session, dst):
    from jobpilot.observability.events import record_event

    app = make_app_row(db_session, status="SUBMISSION_IN_PROGRESS")
    app.idempotency_key = "sha256:" + "a" * 64
    with pytest.raises(IllegalTransition):
        transition(db_session, app, ApplicationStatus(dst), actor="orchestrator", reason="t")
    transition(db_session, app, ApplicationStatus(dst), actor="presubmit", reason="t")
    assert app.status == dst

    clicked = make_app_row(db_session, status="SUBMISSION_IN_PROGRESS")
    clicked.idempotency_key = "sha256:" + "b" * 64
    record_event(db_session, entity_type="application", entity_id=clicked.application_id,
                 actor="orchestrator", action="submit_clicked", outcome="ok",
                 details={"idempotency_key": clicked.idempotency_key})
    with pytest.raises(IllegalTransition, match="clicked"):
        transition(db_session, clicked, ApplicationStatus(dst), actor="presubmit", reason="t")
    assert clicked.status == "SUBMISSION_IN_PROGRESS"
