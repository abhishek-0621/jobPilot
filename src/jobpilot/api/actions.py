"""State-changing user actions shared by the JSON API and the dashboard."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from jobpilot.data.tables import ApprovalBatchRow
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator, TaskKind


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
