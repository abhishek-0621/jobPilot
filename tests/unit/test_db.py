import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from jobpilot.data.repositories import counts_by_status, list_events, upsert_job
from jobpilot.data.tables import ApplicationRow, JobRow
from jobpilot.observability.events import record_event
from tests.factories import make_listing


def test_audit_event_is_append_only(db_session):
    record_event(db_session, entity_type="job", entity_id="job_1", actor="test",
                 action="x", outcome="ok")
    db_session.commit()
    with pytest.raises((IntegrityError, OperationalError)):
        db_session.execute(text("UPDATE audit_event SET action='y'"))
        db_session.commit()
    db_session.rollback()
    with pytest.raises((IntegrityError, OperationalError)):
        db_session.execute(text("DELETE FROM audit_event"))
        db_session.commit()
    db_session.rollback()
    assert len(list_events(db_session, "job_1")) == 1


def test_record_event_redacts(db_session):
    ev = record_event(db_session, entity_type="job", entity_id="j", actor="t",
                      action="a", outcome="ok", details={"token": "s3cret"})
    assert "s3cret" not in ev.redacted_details_json


def test_one_application_per_job(db_session):
    job, _ = upsert_job(db_session, make_listing())
    db_session.add(ApplicationRow(application_id="app_1", job_id=job.job_id,
                                  status="NOT_STARTED"))
    db_session.commit()
    db_session.add(ApplicationRow(application_id="app_2", job_id=job.job_id,
                                  status="NOT_STARTED"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_upsert_job_dedupes_on_source_id(db_session):
    _, created1 = upsert_job(db_session, make_listing(source="greenhouse", source_job_id="42"))
    _, created2 = upsert_job(db_session, make_listing(source="greenhouse", source_job_id="42"))
    assert created1 and not created2
    assert counts_by_status(db_session, JobRow) == {"DISCOVERED": 1}


def test_datetimes_round_trip_aware(db_session):
    from datetime import UTC, datetime, timedelta, timezone

    job, _ = upsert_job(db_session, make_listing())
    db_session.commit()
    db_session.expire_all()
    assert job.first_seen_at.tzinfo is not None
    assert job.first_seen_at.utcoffset() == timedelta(0)
    ev = record_event(db_session, entity_type="job", entity_id="j", actor="t",
                      action="a", outcome="ok")
    db_session.commit()
    db_session.expire_all()
    assert ev.timestamp_utc.tzinfo is not None
    # aware non-UTC values are normalised to UTC
    est = timezone(timedelta(hours=-5))
    job.last_seen_at = datetime(2026, 1, 1, 7, tzinfo=est)
    db_session.commit()
    db_session.expire_all()
    assert job.last_seen_at == datetime(2026, 1, 1, 12, tzinfo=UTC)
    # naive values are rejected
    job.last_seen_at = datetime(2026, 1, 1)
    with pytest.raises(Exception, match="naive datetime"):
        db_session.commit()
    db_session.rollback()


def test_audit_insert_or_replace_blocked(db_session):
    ev = record_event(db_session, entity_type="job", entity_id="job_r", actor="test",
                      action="orig", outcome="ok")
    db_session.commit()
    with pytest.raises((IntegrityError, OperationalError)):
        db_session.execute(text(
            "INSERT OR REPLACE INTO audit_event (event_id, timestamp_utc, entity_type, entity_id, actor, "
            "action, outcome, redacted_details_json) VALUES (:i, CURRENT_TIMESTAMP, 'job', 'job_r', 'evil', 'forged', 'ok', "
            "'{}')"), {"i": ev.event_id})
        db_session.commit()
    db_session.rollback()
    rows = list_events(db_session, "job_r")
    assert len(rows) == 1 and rows[0].action == "orig"
