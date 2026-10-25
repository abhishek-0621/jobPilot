"""Domain rules for (re)generating an application's documents.

Documents are stamped with the profile version whose facts they used; a handler that
sees an older stamp than the active profile throws its output away and re-queues TAILOR.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import ApplicationStatus
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import (
    ApplicationRow,
    CandidateProfileRow,
    DocumentArtifactRow,
    TaskRow,
)
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import FollowUp, TaskKind, TaskStatus, add_task
from jobpilot.orchestrator.state_machine import transition

_A = ApplicationStatus
REGENERABLE = frozenset({_A.DOCUMENTS_PENDING.value, _A.FORM_IN_PROGRESS.value,
                         _A.NEEDS_USER_INPUT.value, _A.VALIDATION_FAILED.value,
                         _A.READY_FOR_REVIEW.value, _A.APPROVED.value,
                         _A.MANUAL_APPLY_READY.value})
_DOC_TASKS = (TaskKind.TAILOR.value, TaskKind.COVER_LETTER.value)
_ACTIVE = (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)


class DocumentsConflict(Exception):
    """Documents cannot be regenerated right now."""


class DocumentsBusy(DocumentsConflict):
    pass


def active_profile_version(s: Session) -> int | None:
    return s.execute(select(func.max(CandidateProfileRow.version))).scalar()


def documents_busy(s: Session, application_id: str) -> bool:
    return s.execute(select(TaskRow.task_id).where(
        TaskRow.kind.in_(_DOC_TASKS), TaskRow.entity_id == application_id,
        TaskRow.status.in_(_ACTIVE))).first() is not None


def _to_documents_pending(s: Session, app: ApplicationRow, reason: str, actor: str) -> None:
    ApprovalService(s).invalidate_application(app.application_id, reason)  # APPROVED -> RFR
    if app.status != _A.DOCUMENTS_PENDING.value:
        transition(s, app, _A.DOCUMENTS_PENDING, actor=actor, reason=reason)


def reset_documents(s: Session, application_id: str, reason: str, *, actor: str) -> str:
    """Revoke any approval, move back to DOCUMENTS_PENDING and queue TAILOR (no commit).

    Raises LookupError, DocumentsBusy, or DocumentsConflict (status not regenerable).
    """
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    if app.status not in REGENERABLE:
        raise DocumentsConflict(f"Documents cannot be regenerated (status is {app.status}).")
    if documents_busy(s, application_id):
        raise DocumentsBusy("Documents are already being generated for this application.")
    _to_documents_pending(s, app, reason, actor)
    task_id = add_task(s, TaskKind.TAILOR, "application", application_id)
    s.flush()
    return task_id


def stale_follow_up(s: Session, task: TaskRow, application_id: str,
                    versions: list[int | None], actor: str) -> list[FollowUp] | None:
    """None when every version equals the active profile version. Otherwise drop the
    output: back to DOCUMENTS_PENDING if needed, event documents_stale, re-queue TAILOR
    (unless another TAILOR is already queued)."""
    active = active_profile_version(s)
    if all(v == active for v in versions):
        return None
    app = get_application(s, application_id)
    if app.status in REGENERABLE:
        _to_documents_pending(s, app, "profile_fact_changed", actor)
    queued = s.execute(select(TaskRow.task_id).where(
        TaskRow.kind == TaskKind.TAILOR.value, TaskRow.entity_id == application_id,
        TaskRow.status == TaskStatus.QUEUED.value, TaskRow.task_id != task.task_id)).first()
    record_event(s, entity_type="application", entity_id=application_id, actor=actor,
                 action="documents_stale", outcome="requeued",
                 details={"task_id": task.task_id, "active_profile_version": active})
    return [] if queued else [(TaskKind.TAILOR, "application", application_id)]


def current_artifact_version(s: Session, artifact_id: str | None) -> int | None:
    row = None if artifact_id is None else s.get(DocumentArtifactRow, artifact_id)
    return None if row is None else row.profile_version
