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
from jobpilot.observability.events import record_event, submit_click_recorded

J, A, B, O, R = JobStatus, ApplicationStatus, BatchStatus, OutreachStatus, AgentRunStatus  # noqa: E741 - short aliases keep the transition table readable


class IllegalTransition(Exception):
    pass


def _build(
    enum: type[StrEnum], edges: dict[StrEnum, set[StrEnum]]
) -> dict[StrEnum, frozenset[StrEnum]]:
    return {s: frozenset(edges.get(s, ())) for s in enum}


_PRE_SUBMISSION = {
    A.NOT_STARTED, A.DOCUMENTS_PENDING, A.FORM_IN_PROGRESS, A.NEEDS_USER_INPUT,
    A.VALIDATING, A.VALIDATION_FAILED, A.READY_FOR_REVIEW, A.APPROVED, A.MANUAL_APPLY_READY,
}

_APPLICATION: dict[StrEnum, set[StrEnum]] = {
    A.NOT_STARTED: {A.DOCUMENTS_PENDING},
    A.DOCUMENTS_PENDING: {A.FORM_IN_PROGRESS, A.NEEDS_USER_INPUT},
    # -> DOCUMENTS_PENDING edges: regenerate documents (user action or stale facts).
    A.FORM_IN_PROGRESS: {A.NEEDS_USER_INPUT, A.VALIDATING, A.DOCUMENTS_PENDING,
                         A.MANUAL_APPLY_READY},
    # Manual apply path: the user applies on the site themselves and says so.
    A.MANUAL_APPLY_READY: {A.SUBMITTED_BY_USER, A.DOCUMENTS_PENDING},
    A.NEEDS_USER_INPUT: {A.VALIDATING, A.FORM_IN_PROGRESS, A.DOCUMENTS_PENDING},
    A.VALIDATING: {A.VALIDATION_FAILED, A.READY_FOR_REVIEW},
    A.VALIDATION_FAILED: {A.FORM_IN_PROGRESS, A.DOCUMENTS_PENDING},
    A.READY_FOR_REVIEW: {A.APPROVED, A.FORM_IN_PROGRESS, A.DOCUMENTS_PENDING},
    A.APPROVED: {A.SUBMISSION_IN_PROGRESS, A.READY_FOR_REVIEW},
    # -> READY_FOR_REVIEW / NEEDS_USER_INPUT: pre-submit checks failed before any click
    # (actor "presubmit" only, and never once a submit_clicked event exists).
    A.SUBMISSION_IN_PROGRESS: {A.SUBMITTED, A.SUBMISSION_UNCERTAIN, A.SUBMISSION_FAILED,
                               A.READY_FOR_REVIEW, A.NEEDS_USER_INPUT},
    A.SUBMITTED: {A.CONFIRMED, A.SUBMISSION_UNCERTAIN},
    A.SUBMISSION_UNCERTAIN: {A.CONFIRMED, A.SUBMISSION_FAILED},
    # "Prepare again" after a failed submission (explicit user action only); straight to
    # DOCUMENTS_PENDING when retention pruned the documents.
    A.SUBMISSION_FAILED: {A.FORM_IN_PROGRESS, A.DOCUMENTS_PENDING},
}
for _s in _PRE_SUBMISSION:
    _APPLICATION[_s] = _APPLICATION.get(_s, set()) | {A.REJECTED_BY_USER}

# Terminal states: nothing more happens to these applications, so retention may prune them.
TERMINAL_APPLICATION = frozenset({A.CONFIRMED, A.SUBMITTED_BY_USER, A.REJECTED_BY_USER,
                                  A.SUBMISSION_FAILED, A.DELETED})
# Deleting an application's data (user only) is allowed from any terminal state but DELETED.
for _s in TERMINAL_APPLICATION - {A.DELETED}:
    _APPLICATION[_s] = _APPLICATION.get(_s, set()) | {A.DELETED}

_OUTREACH: dict[StrEnum, set[StrEnum]] = {
    O.NOT_ELIGIBLE: {O.CONTACT_RESEARCH},
    O.CONTACT_RESEARCH: {O.DRAFT_READY},
    O.DRAFT_READY: {O.NEEDS_REVIEW},
    O.NEEDS_REVIEW: {O.APPROVED, O.DRAFT_READY},
    O.APPROVED: {O.SEND_IN_PROGRESS, O.NEEDS_REVIEW},  # edit/contact change: review again
    O.SEND_IN_PROGRESS: {O.SENT, O.DELIVERY_UNKNOWN, O.FAILED},
}
# SKIPPED is reachable from every non-terminal state except those where a send
# is in flight or its outcome is unknown.
for _s in (O.NOT_ELIGIBLE, O.CONTACT_RESEARCH, O.DRAFT_READY, O.NEEDS_REVIEW, O.APPROVED):
    _OUTREACH[_s] = _OUTREACH[_s] | {O.SKIPPED}
# A draft skipped for lack of a public contact comes back when the user adds one.
_OUTREACH[O.SKIPPED] = {O.DRAFT_READY}

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
    (A.SUBMISSION_FAILED, A.FORM_IN_PROGRESS): frozenset({"user"}),
    (A.SUBMISSION_FAILED, A.DOCUMENTS_PENDING): frozenset({"user"}),
    (A.MANUAL_APPLY_READY, A.SUBMITTED_BY_USER): frozenset({"user"}),
    (A.SUBMISSION_IN_PROGRESS, A.READY_FOR_REVIEW): frozenset({"presubmit"}),
    (A.SUBMISSION_IN_PROGRESS, A.NEEDS_USER_INPUT): frozenset({"presubmit"}),
    **{(s, A.DELETED): frozenset({"user"}) for s in TERMINAL_APPLICATION - {A.DELETED}},
    # JobPilot never sends outreach: only the user's own "Mark as sent" takes these edges.
    (O.APPROVED, O.SEND_IN_PROGRESS): frozenset({"user"}),
    (O.SEND_IN_PROGRESS, O.SENT): frozenset({"user"}),
    (O.SKIPPED, O.DRAFT_READY): frozenset({"user"}),
}
# Edges that are illegal once the submit control may have been clicked.
_NO_CLICK_EDGES = frozenset({(A.SUBMISSION_IN_PROGRESS, A.READY_FOR_REVIEW),
                             (A.SUBMISSION_IN_PROGRESS, A.NEEDS_USER_INPUT)})

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
    if (current, new_status) in _NO_CLICK_EDGES and submit_click_recorded(
            session, getattr(row, "idempotency_key", None)):
        raise IllegalTransition(f"{current} -> {new_status}: the submit control was clicked")
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
