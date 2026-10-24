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
    """Approve exactly what was presented, commit, then queue SUBMIT per approved app.

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
    s.commit()
    for app_id in result.approved_ids:
        orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    return result
