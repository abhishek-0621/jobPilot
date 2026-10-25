from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import ErrorCode
from jobpilot.data.tables import AuditEventRow
from jobpilot.ids import new_id
from jobpilot.observability.redaction import redact
from jobpilot.util.hashing import canonical_json


def record_event(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    actor: str,
    action: str,
    outcome: str,
    details: dict | None = None,
    run_id: str | None = None,
    evidence_ref: str | None = None,
    duration_ms: int | None = None,
    error_code: ErrorCode | None = None,
) -> AuditEventRow:
    row = AuditEventRow(
        event_id=new_id("evt"),
        entity_type=entity_type,
        entity_id=entity_id,
        run_id=run_id,
        actor=actor,
        action=action,
        outcome=outcome,
        redacted_details_json=canonical_json(redact(details or {})),
        evidence_ref=evidence_ref,
        duration_ms=duration_ms,
        error_code=error_code.value if error_code else None,
    )
    session.add(row)
    session.flush()
    return row


SUBMIT_CLICKED = "submit_clicked"


def click_needle(idempotency_key: str) -> str:
    """The exact ``"idempotency_key":"<key>"`` fragment of a submit_clicked event."""
    return canonical_json({"idempotency_key": idempotency_key})[1:-1]


def submit_click_recorded(session: Session, idempotency_key: str | None) -> bool:
    """True when a ``submit_clicked`` event was written for ``idempotency_key``."""
    if not idempotency_key:
        return False
    return session.execute(select(AuditEventRow.event_id).where(
        AuditEventRow.action == SUBMIT_CLICKED,
        AuditEventRow.redacted_details_json.contains(click_needle(idempotency_key)))
        .limit(1)).first() is not None
