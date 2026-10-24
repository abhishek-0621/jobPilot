"""State-changing user actions shared by the JSON API and the dashboard."""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import ApplicationStatus, JobStatus
from jobpilot.data.repositories import create_application
from jobpilot.data.tables import ApplicationRow, ApprovalBatchRow, JobRow, TaskRow
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator, TaskKind, TaskStatus, add_task
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


DOCUMENTS_BUSY = "Documents are already being generated for this application."
A = ApplicationStatus
_REGENERABLE = frozenset({A.DOCUMENTS_PENDING.value, A.FORM_IN_PROGRESS.value,
                          A.NEEDS_USER_INPUT.value, A.VALIDATION_FAILED.value,
                          A.READY_FOR_REVIEW.value, A.APPROVED.value})
_DOC_TASKS = (TaskKind.TAILOR.value, TaskKind.COVER_LETTER.value)


def documents_busy(s: Session, application_id: str) -> bool:
    return s.execute(select(TaskRow.task_id).where(
        TaskRow.kind.in_(_DOC_TASKS), TaskRow.entity_id == application_id,
        TaskRow.status.in_((TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)))).first() \
        is not None


def reset_documents(s: Session, application_id: str, reason: str, *, actor: str,
                    enqueue: Callable[[Session, TaskKind, str, str], str] = add_task) -> str:
    """Revoke any approval, move back to DOCUMENTS_PENDING and queue TAILOR (no commit).

    Raises LookupError / ActionConflict.
    """
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    if app.status not in _REGENERABLE:
        raise ActionConflict(f"Documents cannot be regenerated (status is {app.status}).")
    if documents_busy(s, application_id):
        raise ActionConflict(DOCUMENTS_BUSY)
    ApprovalService(s).invalidate_application(application_id, reason)  # APPROVED -> RFR
    if app.status != A.DOCUMENTS_PENDING.value:
        transition(s, app, A.DOCUMENTS_PENDING, actor=actor, reason=reason)
    task_id = enqueue(s, TaskKind.TAILOR, "application", application_id)
    s.flush()
    return task_id


def regenerate_documents(s: Session, orch: Orchestrator, application_id: str,
                         reason: str = "user_regenerate") -> str:
    """User action: regenerate resume + cover letter. Commits; raises like reset_documents."""
    task_id = reset_documents(s, application_id, reason, actor="user",
                              enqueue=orch.enqueue_in)
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
