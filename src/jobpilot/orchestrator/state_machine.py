from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from jobpilot.contracts.enums import (
    AgentRunStatus,
    ApplicationStatus,
    BatchStatus,
    JobStatus,
    OutreachStatus,
)
from jobpilot.observability.events import record_event

J, A, B, O, R = JobStatus, ApplicationStatus, BatchStatus, OutreachStatus, AgentRunStatus  # noqa: E741 - short aliases keep the transition table readable


class IllegalTransition(Exception):
    pass


def _build(
    enum: type[StrEnum], edges: dict[StrEnum, set[StrEnum]]
) -> dict[StrEnum, frozenset[StrEnum]]:
    return {s: frozenset(edges.get(s, ())) for s in enum}


_PRE_SUBMISSION = {
    A.NOT_STARTED, A.DOCUMENTS_PENDING, A.FORM_IN_PROGRESS, A.NEEDS_USER_INPUT,
    A.VALIDATING, A.VALIDATION_FAILED, A.READY_FOR_REVIEW, A.APPROVED,
}

_APPLICATION: dict[StrEnum, set[StrEnum]] = {
    A.NOT_STARTED: {A.DOCUMENTS_PENDING},
    A.DOCUMENTS_PENDING: {A.FORM_IN_PROGRESS, A.NEEDS_USER_INPUT},
    # -> DOCUMENTS_PENDING edges: regenerate documents (user action or stale facts).
    A.FORM_IN_PROGRESS: {A.NEEDS_USER_INPUT, A.VALIDATING, A.DOCUMENTS_PENDING},
    A.NEEDS_USER_INPUT: {A.VALIDATING, A.FORM_IN_PROGRESS, A.DOCUMENTS_PENDING},
    A.VALIDATING: {A.VALIDATION_FAILED, A.READY_FOR_REVIEW},
    A.VALIDATION_FAILED: {A.FORM_IN_PROGRESS, A.DOCUMENTS_PENDING},
    A.READY_FOR_REVIEW: {A.APPROVED, A.FORM_IN_PROGRESS, A.DOCUMENTS_PENDING},
    A.APPROVED: {A.SUBMISSION_IN_PROGRESS, A.READY_FOR_REVIEW},
    A.SUBMISSION_IN_PROGRESS: {A.SUBMITTED, A.SUBMISSION_UNCERTAIN, A.SUBMISSION_FAILED},
    A.SUBMITTED: {A.CONFIRMED, A.SUBMISSION_UNCERTAIN},
    A.SUBMISSION_UNCERTAIN: {A.CONFIRMED, A.SUBMISSION_FAILED},
}
for _s in _PRE_SUBMISSION:
    _APPLICATION[_s] = _APPLICATION.get(_s, set()) | {A.REJECTED_BY_USER}

_OUTREACH: dict[StrEnum, set[StrEnum]] = {
    O.NOT_ELIGIBLE: {O.CONTACT_RESEARCH},
    O.CONTACT_RESEARCH: {O.DRAFT_READY},
    O.DRAFT_READY: {O.NEEDS_REVIEW},
    O.NEEDS_REVIEW: {O.APPROVED, O.DRAFT_READY},
    O.APPROVED: {O.SEND_IN_PROGRESS},
    O.SEND_IN_PROGRESS: {O.SENT, O.DELIVERY_UNKNOWN, O.FAILED},
}
# SKIPPED is reachable from every non-terminal state except those where a send
# is in flight or its outcome is unknown.
for _s in (O.NOT_ELIGIBLE, O.CONTACT_RESEARCH, O.DRAFT_READY, O.NEEDS_REVIEW, O.APPROVED):
    _OUTREACH[_s] = _OUTREACH[_s] | {O.SKIPPED}

TRANSITIONS: dict[type[StrEnum], dict[StrEnum, frozenset[StrEnum]]] = {
    JobStatus: _build(J, {
        J.DISCOVERED: {J.NORMALIZED, J.DUPLICATE, J.CLOSED},
        J.NORMALIZED: {J.MATCHED, J.DUPLICATE, J.STALE, J.CLOSED},
        J.MATCHED: {J.REJECTED, J.STALE, J.CLOSED},
        J.REJECTED: {J.MATCHED},  # user override
    }),
    ApplicationStatus: _build(A, _APPLICATION),
    BatchStatus: _build(B, {
        B.BUILDING: {B.READY_FOR_REVIEW, B.CANCELLED},
        B.READY_FOR_REVIEW: {B.PARTIALLY_APPROVED, B.APPROVED, B.CANCELLED, B.EXPIRED},
        B.PARTIALLY_APPROVED: {B.APPROVED, B.PROCESSING, B.CANCELLED, B.EXPIRED},
        B.APPROVED: {B.PROCESSING, B.CANCELLED, B.EXPIRED},
        B.PROCESSING: {B.COMPLETED, B.COMPLETED_WITH_ERRORS, B.CANCELLED},
    }),
    OutreachStatus: _build(O, _OUTREACH),
    AgentRunStatus: _build(R, {
        R.QUEUED: {R.RUNNING, R.CANCELLED},
        R.RUNNING: {R.WAITING_FOR_USER, R.SUCCEEDED, R.PARTIAL, R.FAILED,
                    R.CANCELLED, R.TIMED_OUT},
        R.WAITING_FOR_USER: {R.RUNNING, R.CANCELLED},
    }),
}

# Edges that only specific actors may take.
RESTRICTED_EDGES: dict[tuple[StrEnum, StrEnum], frozenset[str]] = {
    (A.SUBMISSION_UNCERTAIN, A.CONFIRMED): frozenset({"user", "verification"}),
    (A.SUBMISSION_UNCERTAIN, A.SUBMISSION_FAILED): frozenset({"user", "verification"}),
}

_ENTITY: dict[type[StrEnum], tuple[str, str]] = {
    JobStatus: ("job", "job_id"),
    ApplicationStatus: ("application", "application_id"),
    BatchStatus: ("approval_batch", "batch_id"),
    OutreachStatus: ("outreach", "outreach_id"),
    AgentRunStatus: ("agent_run", "run_id"),
}


def transition(
    session: Session,
    row: Any,
    new_status: StrEnum,
    *,
    actor: str,
    reason: str,
    run_id: str | None = None,
    details: dict | None = None,
) -> None:
    enum = type(new_status)
    if enum not in TRANSITIONS:
        raise IllegalTransition(f"no state machine for {enum.__name__}")
    current = enum(row.status)
    if new_status not in TRANSITIONS[enum][current]:
        raise IllegalTransition(f"{enum.__name__}: {current} -> {new_status} not allowed")
    allowed_actors = RESTRICTED_EDGES.get((current, new_status))
    if allowed_actors is not None and actor not in allowed_actors:
        raise IllegalTransition(
            f"{current} -> {new_status} requires actor in {sorted(allowed_actors)}, got {actor!r}"
        )
    row.status = new_status.value
    entity_type, id_attr = _ENTITY[enum]
    record_event(
        session,
        entity_type=entity_type,
        entity_id=getattr(row, id_attr),
        actor=actor,
        action="status_changed",
        outcome="ok",
        run_id=run_id,
        details={"from": current.value, "to": new_status.value, "reason": reason,
                 **(details or {})},
    )
