"""Plan 10 Task 1: data & audit integrity."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from jobpilot.contracts.enums import ApplicationStatus
from jobpilot.data import repositories
from jobpilot.data.repositories import list_events, upsert_job
from jobpilot.data.tables import ApprovalRow
from jobpilot.observability.redaction import REDACTED, redact
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.state_machine import IllegalTransition, transition
from jobpilot.profile.schema import ProfileFile
from jobpilot.util.hashing import canonical_json
from tests.factories import make_app_row, make_listing

FIXTURE = Path("tests/fixtures/profile_example.yaml")


class _Secretive(BaseModel):
    api_key: str
    note: str


def test_redact_handles_sets_tuples_and_models():
    tok = "Bearer abc123"
    out = redact({"t": (tok, "ok"), "s": {tok}, "m": _Secretive(api_key="x", note=tok)})
    assert out["t"] == [REDACTED, "ok"]
    assert out["s"] == {REDACTED}
    assert out["m"] == {"api_key": REDACTED, "note": REDACTED}
    json.loads(canonical_json(out))


def test_naive_datetime_serialized_as_utc():
    assert canonical_json(datetime(2026, 1, 2, 3, 4, 5)) == '"2026-01-02T03:04:05Z"'
    assert canonical_json(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == \
        '"2026-01-02T03:04:05Z"'


def test_canonical_json_rejects_nan():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_transition_details_cannot_overwrite_from_to_reason(db_session):
    app = make_app_row(db_session, status="APPROVED")
    transition(db_session, app, ApplicationStatus.SUBMISSION_IN_PROGRESS, actor="orchestrator",
               reason="real", details={"from": "X", "to": "Y", "reason": "fake", "k": 1})
    d = json.loads(list_events(db_session, app.application_id)[0].redacted_details_json)
    assert (d["from"], d["to"], d["reason"], d["k"]) == (
        "APPROVED", "SUBMISSION_IN_PROGRESS", "real", 1)


def test_invalid_stored_status_clear_error(db_session):
    app = make_app_row(db_session)
    app.status = "BOGUS"
    with pytest.raises(IllegalTransition, match="BOGUS"):
        transition(db_session, app, ApplicationStatus.DOCUMENTS_PENDING, actor="system",
                   reason="t")


def test_upsert_job_race_insert_or_ignore(db_session, monkeypatch):
    listing = make_listing(source_job_id="race1", canonical_url="https://example.com/r1")
    first, created = upsert_job(db_session, listing)
    assert created
    # Simulate the race: the pre-check misses the row another worker just inserted.
    monkeypatch.setattr(repositories, "_by_source_id", lambda s, listing: None)
    again, created2 = upsert_job(db_session, listing)
    assert not created2 and again.job_id == first.job_id


def _profile_data() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def test_duplicate_fact_id_is_schema_error():
    data = _profile_data()
    fid = data["experience"][0]["facts"][0]["fact_id"]
    data["achievements"] = [*data.get("achievements", []), {"fact_id": fid, "value": "dup"}]
    with pytest.raises(ValidationError, match="duplicate fact_id"):
        ProfileFile.model_validate(data)


def test_approvals_containing_tolerates_malformed_json(db_session):
    app = make_app_row(db_session, status="READY_FOR_REVIEW")
    db_session.add(ApprovalRow(approval_id="apr_bad", batch_id="b", batch_version=1,
                               kind="APPLICATION", content_hash="h",
                               approved_item_ids_json="{not json"))
    db_session.flush()
    svc = ApprovalService(db_session)
    assert svc.active_approval_for(app.application_id) is None
    db_session.add(ApprovalRow(approval_id="apr_bad2", batch_id="b", batch_version=1,
                               kind="OUTREACH", content_hash="h",
                               approved_item_ids_json="nope"))
    db_session.flush()
    assert svc.active_outreach_approval_for("out_x") is None
