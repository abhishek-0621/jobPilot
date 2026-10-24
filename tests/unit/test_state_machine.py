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
    for s in (ApplicationStatus.CONFIRMED, ApplicationStatus.SUBMISSION_FAILED,
              ApplicationStatus.REJECTED_BY_USER):
        assert app[s] == frozenset()
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
