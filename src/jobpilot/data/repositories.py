from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import ApplicationStatus, BatchStatus, JobStatus
from jobpilot.contracts.models import JobListing
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalBatchRow,
    AuditEventRow,
    BatchItemRow,
    JobRow,
    MatchAssessmentRow,
)
from jobpilot.ids import new_id
from jobpilot.util.hashing import canonical_json


def get_application(s: Session, application_id: str) -> ApplicationRow:
    return s.execute(
        select(ApplicationRow).where(ApplicationRow.application_id == application_id)
    ).scalar_one()


def list_events(
    s: Session, entity_id: str | None = None, limit: int = 200
) -> list[AuditEventRow]:
    q = select(AuditEventRow)
    if entity_id is not None:
        q = q.where(AuditEventRow.entity_id == entity_id)
    q = q.order_by(AuditEventRow.timestamp_utc.desc(), AuditEventRow.event_id.desc())
    return list(s.execute(q.limit(limit)).scalars())


def counts_by_status(s: Session, table: type) -> dict[str, int]:
    status = table.status  # type: ignore[attr-defined]
    rows = s.execute(select(status, func.count()).group_by(status)).all()
    return {k: v for k, v in rows}


def upsert_job(s: Session, listing: JobListing) -> tuple[JobRow, bool]:
    if listing.source_job_id is not None:
        existing = s.execute(
            select(JobRow).where(
                JobRow.source == listing.source,
                JobRow.source_job_id == listing.source_job_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.last_seen_at = listing.last_seen_at
            s.flush()
            return existing, False
    row = JobRow(
        job_id=new_id("job"),
        source=listing.source,
        source_job_id=listing.source_job_id,
        canonical_url=listing.canonical_url,
        application_url=listing.application_url,
        company=listing.company,
        title=listing.title,
        location=listing.location,
        work_mode=listing.work_mode,
        description_text=listing.description_text,
        extracted_json=canonical_json(listing),
        status=JobStatus.DISCOVERED.value,
        first_seen_at=listing.first_seen_at,
        last_seen_at=listing.last_seen_at,
    )
    s.add(row)
    s.flush()
    return row, True


def jobs_with_scores(
    s: Session, status: str | None = None, min_score: float | None = None
) -> list[tuple[JobRow, float | None]]:
    q = select(JobRow, func.max(MatchAssessmentRow.total_score)).outerjoin(
        MatchAssessmentRow, MatchAssessmentRow.job_id == JobRow.job_id
    ).group_by(JobRow.job_id).order_by(JobRow.first_seen_at.desc(), JobRow.job_id)
    if status is not None:
        q = q.where(JobRow.status == status)
    return [(job, score) for job, score in s.execute(q).all()
            if min_score is None or (score is not None and score >= min_score)]


def latest_batch(s: Session, batch_id: str) -> ApprovalBatchRow | None:
    return s.execute(select(ApprovalBatchRow).where(ApprovalBatchRow.batch_id == batch_id)
                     .order_by(ApprovalBatchRow.version.desc())).scalars().first()


def batch_items(s: Session, batch_id: str, version: int) -> list[BatchItemRow]:
    return list(s.execute(select(BatchItemRow).where(
        BatchItemRow.batch_id == batch_id, BatchItemRow.version == version,
    ).order_by(BatchItemRow.application_id)).scalars())


def pending_batches(s: Session) -> list[ApprovalBatchRow]:
    """Latest version of every batch still awaiting (more) approval."""
    latest = select(ApprovalBatchRow.batch_id, func.max(ApprovalBatchRow.version).label("v")
                    ).group_by(ApprovalBatchRow.batch_id).subquery()
    return list(s.execute(select(ApprovalBatchRow).join(
        latest, (ApprovalBatchRow.batch_id == latest.c.batch_id)
        & (ApprovalBatchRow.version == latest.c.v)
    ).where(ApprovalBatchRow.status.in_((BatchStatus.READY_FOR_REVIEW.value,
                                         BatchStatus.PARTIALLY_APPROVED.value)))
        .order_by(ApprovalBatchRow.created_at.desc())).scalars())


def recent_failures(s: Session, limit: int = 10) -> list[AuditEventRow]:
    return list(s.execute(select(AuditEventRow).where(AuditEventRow.error_code.is_not(None))
                          .order_by(AuditEventRow.timestamp_utc.desc(),
                                    AuditEventRow.event_id.desc()).limit(limit)).scalars())


def ready_unbatched_applications(s: Session) -> list[tuple[ApplicationRow, JobRow]]:
    """READY_FOR_REVIEW applications not in any batch still awaiting approval."""
    pending = {(b.batch_id, b.version) for b in pending_batches(s)}
    in_pending = {i.application_id for i in s.execute(select(BatchItemRow)).scalars()
                  if (i.batch_id, i.version) in pending}
    rows = s.execute(select(ApplicationRow, JobRow).join(
        JobRow, JobRow.job_id == ApplicationRow.job_id
    ).where(ApplicationRow.status == ApplicationStatus.READY_FOR_REVIEW.value)
        .order_by(ApplicationRow.application_id)).all()
    return [(a, j) for a, j in rows if a.application_id not in in_pending]
