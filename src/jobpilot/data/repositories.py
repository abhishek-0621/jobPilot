from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import ApplicationStatus, BatchStatus, JobStatus
from jobpilot.contracts.models import JobListing
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalBatchRow,
    AuditEventRow,
    BatchItemRow,
    CandidateProfileRow,
    JobRow,
    MatchAssessmentRow,
)
from jobpilot.discovery.normalize import dedupe_key, normalize_url
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
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


def _by_source_id(s: Session, listing: JobListing) -> JobRow | None:
    if listing.source_job_id is None:
        return None
    return s.execute(select(JobRow).where(
        JobRow.source == listing.source, JobRow.source_job_id == listing.source_job_id,
    )).scalar_one_or_none()


def _seen_again(s: Session, existing: JobRow, listing: JobListing) -> tuple[JobRow, bool]:
    existing.last_seen_at = listing.last_seen_at
    s.flush()
    return existing, False


def upsert_job(s: Session, listing: JobListing) -> tuple[JobRow, bool]:
    existing = _by_source_id(s, listing)
    if existing is not None:
        return _seen_again(s, existing, listing)
    url = normalize_url(listing.canonical_url)
    key = dedupe_key(listing.company, listing.title, listing.location)
    canonical = _find_canonical(s, JobRow.canonical_url == url)
    match = "url"
    if canonical is None:
        canonical = _find_canonical(s, JobRow.dedupe_key == key)
        match = "dedupe_key"
    job_id = new_id("job")
    values = dict(
        job_id=job_id,
        source=listing.source,
        source_job_id=listing.source_job_id,
        canonical_url=url,
        application_url=listing.application_url,
        company=listing.company,
        title=listing.title,
        location=listing.location,
        work_mode=listing.work_mode,
        description_text=listing.description_text,
        extracted_json=canonical_json(listing),
        dedupe_key=key,
        canonical_job_id=canonical.job_id if canonical is not None else None,
        status=(JobStatus.DUPLICATE if canonical is not None else JobStatus.DISCOVERED).value,
        first_seen_at=listing.first_seen_at,
        last_seen_at=listing.last_seen_at,
    )
    # Insert-or-ignore on (source, source_job_id): a concurrent writer that inserted the
    # same posting between the check above and here wins, and we update its row instead.
    s.flush()
    result = s.execute(sqlite_insert(JobRow).values(**values)
                       .on_conflict_do_nothing(index_elements=["source", "source_job_id"]))
    if result.rowcount == 0:
        existing = s.execute(select(JobRow).where(
            JobRow.source == listing.source,
            JobRow.source_job_id == listing.source_job_id)
            .execution_options(populate_existing=True)).scalar_one()
        return _seen_again(s, existing, listing)
    row = s.get(JobRow, job_id)
    assert row is not None
    if canonical is not None:
        record_event(s, entity_type="job", entity_id=row.job_id, actor="discovery",
                     action="job_duplicate", outcome="ok",
                     details={"canonical_job_id": canonical.job_id, "match": match})
        return row, False
    return row, True


def _find_canonical(s: Session, clause: ColumnElement[bool]) -> JobRow | None:
    return s.execute(
        select(JobRow).where(clause, JobRow.status != JobStatus.DUPLICATE.value)
        .order_by(JobRow.first_seen_at, JobRow.job_id).limit(1)
    ).scalar_one_or_none()


def active_profile_version(s: Session) -> int | None:
    return s.execute(select(func.max(CandidateProfileRow.version))).scalar()


def assessments_for(s: Session, job_ids: list[str] | None = None,
                    version: int | None = None) -> dict[str, MatchAssessmentRow]:
    """One query: per job, the assessment for ``version`` (the active profile version)
    when there is one, else its latest. ``job_ids`` None means every job."""
    q = select(MatchAssessmentRow).order_by(MatchAssessmentRow.profile_version)
    if job_ids is not None:
        q = q.where(MatchAssessmentRow.job_id.in_(job_ids))
    out: dict[str, MatchAssessmentRow] = {}
    for m in s.execute(q).scalars():
        if out.get(m.job_id) is None or out[m.job_id].profile_version != version:
            out[m.job_id] = m  # ascending: ends on the latest unless the active one won
    return out


def jobs_with_scores(
    s: Session, status: str | None = None, min_score: float | None = None
) -> list[tuple[JobRow, float | None]]:
    """Jobs, newest first, with the same score the job page shows: the assessment for the
    active profile version, else the latest one."""
    q = select(JobRow).order_by(JobRow.first_seen_at.desc(), JobRow.job_id)
    if status is not None:
        q = q.where(JobRow.status == status)
    jobs = list(s.execute(q).scalars())
    scores = assessments_for(s, None, active_profile_version(s))
    out = []
    for job in jobs:
        m = scores.get(job.job_id)
        score = None if m is None else m.total_score
        if min_score is None or (score is not None and score >= min_score):
            out.append((job, score))
    return out


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


def create_application(s: Session, job: JobRow) -> ApplicationRow:
    """Start an application for a selected job (caller checks eligibility and commits)."""
    row = ApplicationRow(application_id=new_id("app"), job_id=job.job_id,
                         status=ApplicationStatus.NOT_STARTED.value, portal=job.source)
    s.add(row)
    s.flush()
    return row
