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
