"""State-changing user actions shared by the JSON API and the dashboard."""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.agents.form_prep import USER_SOURCE, answer_rows, unresolved_required
from jobpilot.contracts.enums import ApplicationStatus, FieldDecision, JobStatus
from jobpilot.data.mutations import set_answer
from jobpilot.data.repositories import create_application
from jobpilot.data.tables import ApplicationRow, ApprovalBatchRow, JobRow, TaskRow
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.documents_flow import DocumentsConflict, reset_documents
from jobpilot.orchestrator.engine import Orchestrator, TaskKind, TaskStatus
from jobpilot.orchestrator.state_machine import transition

DISCOVERY_RUNNING = "A discovery run is already queued or running."


class ActionConflict(Exception):
    """The requested action is not allowed in the current state (HTTP 409)."""


def start_discovery(s: Session, orch: Orchestrator) -> tuple[str, str]:
    """Queue a DISCOVER task for a new run; refuse while one is QUEUED/RUNNING."""
    active = s.execute(select(TaskRow.task_id).where(
        TaskRow.kind == TaskKind.DISCOVER.value,
        TaskRow.status.in_((TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)))).first()
    if active is not None:
        raise ActionConflict(DISCOVERY_RUNNING)
    run_id = new_id("run")
    task_id = orch.enqueue_in(s, TaskKind.DISCOVER, "run", run_id)
    s.commit()
    return task_id, run_id


def select_job(s: Session, orch: Orchestrator, job_id: str) -> ApplicationRow:
    """Create an application for a MATCHED job that has none and queue TAILOR in the
    same transaction. Raises LookupError / ActionConflict."""
    job = s.get(JobRow, job_id)
    if job is None:
        raise LookupError("job not found")
    if job.status != JobStatus.MATCHED.value:
        raise ActionConflict(f"Only MATCHED jobs can be selected (job is {job.status}).")
    if s.execute(select(ApplicationRow.application_id)
                 .where(ApplicationRow.job_id == job_id)).first() is not None:
        raise ActionConflict("An application already exists for this job.")
    app = create_application(s, job)
    record_event(s, entity_type="job", entity_id=job_id, actor="user", action="job_selected",
                 outcome="ok", details={"application_id": app.application_id})
    orch.enqueue_in(s, TaskKind.TAILOR, "application", app.application_id)
    s.commit()
    return app


TAILOR_RUNNING = "Tailoring is already queued or running for this application."
_TAILORABLE = (ApplicationStatus.NOT_STARTED.value, ApplicationStatus.DOCUMENTS_PENDING.value)


def start_tailor(s: Session, orch: Orchestrator, application_id: str) -> str:
    """Queue TAILOR for an application. Raises LookupError / ActionConflict."""
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    if app.status not in _TAILORABLE:
        raise ActionConflict(f"Application cannot be tailored (status is {app.status}).")
    active = s.execute(select(TaskRow.task_id).where(
        TaskRow.kind == TaskKind.TAILOR.value, TaskRow.entity_id == application_id,
        TaskRow.status.in_((TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)))).first()
    if active is not None:
        raise ActionConflict(TAILOR_RUNNING)
    task_id = orch.enqueue_in(s, TaskKind.TAILOR, "application", application_id)
    s.commit()
    return task_id


def regenerate_documents(s: Session, application_id: str,
                         reason: str = "user_regenerate") -> str:
    """User action: regenerate resume + cover letter. Commits.

    Raises LookupError / ActionConflict (busy or status not regenerable)."""
    try:
        task_id = reset_documents(s, application_id, reason, actor="user")
    except DocumentsConflict as e:
        s.rollback()
        raise ActionConflict(str(e)) from e
    s.commit()
    return task_id


@dataclass(frozen=True)
class ApprovalResult:
    approval_id: str
    batch_status: str
    approved_ids: list[str]


def approve_and_queue(
    s: Session, orch: Orchestrator, batch_id: str, version: int,
    presented_hash: str, application_ids: list[str],
) -> ApprovalResult:
    """Approve exactly what was presented and queue SUBMIT per app in one transaction.

    Raises StaleApprovalError / ValueError (session rolled back) like ApprovalService.
    """
    try:
        approval = ApprovalService(s).approve(batch_id, version, presented_hash,
                                              application_ids)
    except Exception:
        s.rollback()
        raise
    batch = s.get(ApprovalBatchRow, (batch_id, version))
    assert batch is not None
    result = ApprovalResult(approval.approval_id, batch.status,
                            sorted(set(application_ids)))
    try:
        for app_id in result.approved_ids:
            orch.enqueue_in(s, TaskKind.SUBMIT, "application", app_id)
        s.commit()  # approval and its SUBMIT tasks become durable together
    except Exception:
        s.rollback()
        raise
    return result


USER_EDITABLE_DECISIONS = (FieldDecision.ASK_USER.value, FieldDecision.USER_ONLY.value,
                           FieldDecision.FILL_AND_REVIEW.value)
ANSWERABLE_STATUSES = (ApplicationStatus.NEEDS_USER_INPUT.value,
                       ApplicationStatus.VALIDATION_FAILED.value)
_CHOICE = ("select", "radio")


def user_editable(decision: str, field_type: str) -> bool:
    return decision in USER_EDITABLE_DECISIONS and field_type != "file"


def save_user_answers(s: Session, orch: Orchestrator, application_id: str,
                      values: dict[str, Any]) -> list[str]:
    """Store the user's own answers (source "user", decision USER_ONLY) and, once every
    required field is resolved, move back to FORM_IN_PROGRESS and queue VALIDATE in the
    same transaction. Returns the still-unresolved required field ids. Commits.

    Raises LookupError / ActionConflict (status, unknown or non-editable field, bad option).
    """
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    if app.status not in ANSWERABLE_STATUSES:
        raise ActionConflict(f"Answers cannot be changed now (status is {app.status}).")
    rows = {r.field_id: r for r in answer_rows(s, application_id)}
    for fid, value in values.items():
        row = rows.get(fid)
        if row is None or not user_editable(row.decision, row.field_type):
            s.rollback()
            raise ActionConflict(f"Field {fid} cannot be answered here.")
        options = json.loads(row.options_json)
        bad = (row.field_type in _CHOICE and options and value not in options) or (
            row.field_type == "checkbox_group"
            and not (isinstance(value, list) and set(value) <= set(options)))
        if bad:
            s.rollback()
            raise ActionConflict(f"Field {fid}: choose one of the listed options.")
        set_answer(s, application_id, fid, value, actor="user", source_fact_id=USER_SOURCE,
                   decision=FieldDecision.USER_ONLY, rationale="Answered by the user",
                   confidence=1.0)
    missing = unresolved_required(s, application_id)
    if not missing:
        transition(s, app, ApplicationStatus.FORM_IN_PROGRESS, actor="user",
                   reason="user_answered")
        kind = TaskKind.VALIDATE if app.form_snapshot_hash else TaskKind.PREPARE_FORM
        orch.enqueue_in(s, kind, "application", application_id)
    s.commit()
    return missing


def reread_form(s: Session, orch: Orchestrator, application_id: str) -> str:
    """User action: forget the form snapshot and queue PREPARE_FORM. Commits.

    Raises LookupError / ActionConflict."""
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    if app.status not in ANSWERABLE_STATUSES:
        raise ActionConflict(f"The form cannot be re-read now (status is {app.status}).")
    app.form_snapshot_hash = None
    transition(s, app, ApplicationStatus.FORM_IN_PROGRESS, actor="user", reason="reread_form")
    task_id = orch.enqueue_in(s, TaskKind.PREPARE_FORM, "application", application_id)
    s.commit()
    return task_id
