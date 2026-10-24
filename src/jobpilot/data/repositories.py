from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import JobStatus
from jobpilot.contracts.models import JobListing
from jobpilot.data.tables import ApplicationRow, AuditEventRow, JobRow
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
