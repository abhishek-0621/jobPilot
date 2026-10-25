"""State-changing user actions shared by the JSON API and the dashboard."""

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.agents.form_prep import USER_SOURCE, answer_rows, unresolved_required
from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ApplicationStatus, FieldDecision, JobStatus
from jobpilot.contracts.models import JobListing
from jobpilot.data.db import utcnow
from jobpilot.data.mutations import set_answer
from jobpilot.data.repositories import create_application, upsert_job
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalBatchRow,
    BatchItemRow,
    JobRow,
    TaskRow,
)
from jobpilot.discovery.normalize import html_to_text, normalize_url, require_http
from jobpilot.ids import new_id
from jobpilot.observability.events import (
    USER_RESOLVED_NOT_SUBMITTED,
    prior_unresolved_click,
    record_event,
)
from jobpilot.orchestrator.approval import ApprovalService, forget_live_intent
from jobpilot.orchestrator.documents_flow import DocumentsConflict, reset_documents
from jobpilot.orchestrator.engine import Orchestrator, TaskKind, TaskStatus
from jobpilot.orchestrator.state_machine import transition
from jobpilot.util.hashing import content_hash

DISCOVERY_RUNNING = "A discovery run is already queued or running."


class ActionConflict(Exception):
    """The requested action is not allowed in the current state (HTTP 409)."""


def start_discovery(s: Session, orch: Orchestrator) -> tuple[str, str]:
    """Queue a DISCOVER task for a new run; refuse while one is QUEUED/RUNNING."""
    active = s.execute(select(TaskRow.task_id).where(
        TaskRow.kind == TaskKind.DISCOVER.value,
        TaskRow.status.in_((TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)))).first()
    if active is not None:
        raise ActionConflict(DISCOVERY_RUNNING)
    run_id = new_id("run")
    task_id = orch.enqueue_in(s, TaskKind.DISCOVER, "run", run_id)
    s.commit()
    return task_id, run_id


MANUAL_SOURCE = "manual"


def add_manual_job(s: Session, orch: Orchestrator, *, url: str, title: str, company: str,
                   location: str | None, description: str) -> str:
    """Add a job the user found anywhere (LinkedIn, Naukri, ...). The pasted text is
    untrusted: it is reduced to plain text like a scraped description. A new canonical
    job gets EXTRACT in the same transaction; a known one returns its id. Commits.

    Raises ValueError (non-http(s) URL, missing title/company)."""
    checked = require_http(url)
    if checked is None:
        raise ValueError("URL must be an absolute http(s) URL")
    title, company = title.strip(), company.strip()
    if not title or not company:
        raise ValueError("Title and company are required")
    norm = normalize_url(checked)
    now = utcnow()
    listing = JobListing(
        task_id="manual", created_at=now, job_id="", source=MANUAL_SOURCE,
        source_job_id=content_hash(norm)[7:23], canonical_url=norm, application_url=norm,
        company=company, title=title, location=(location or "").strip() or None,
        description_text=html_to_text(description or ""), first_seen_at=now,
        last_seen_at=now, extraction_confidence=0.0)
    row, created = upsert_job(s, listing)
    job_id = row.canonical_job_id or row.job_id
    if created and row.canonical_job_id is None:
        orch.enqueue_in(s, TaskKind.EXTRACT, "job", row.job_id)
    details: dict[str, Any] = {"job_id": row.job_id, "canonical_job_id": job_id}
    existing = s.get(JobRow, job_id)
    if existing is not None and existing.source != MANUAL_SOURCE:
        details |= {"matched_existing": True, "existing_source": existing.source}
    record_event(s, entity_type="job", entity_id=job_id, actor="user",
                 action="job_added_manually", outcome="ok" if created else "known",
                 details=details)
    s.commit()
    return job_id


def select_job(s: Session, orch: Orchestrator, job_id: str) -> ApplicationRow:
    """Create an application for a MATCHED job that has none and queue TAILOR in the
    same transaction. Raises LookupError / ActionConflict."""
    job = s.get(JobRow, job_id)
    if job is None:
        raise LookupError("job not found")
    if job.status != JobStatus.MATCHED.value:
        raise ActionConflict(f"Only MATCHED jobs can be selected (job is {job.status}).")
    if s.execute(select(ApplicationRow.application_id)
                 .where(ApplicationRow.job_id == job_id)).first() is not None:
        raise ActionConflict("An application already exists for this job.")
    app = create_application(s, job)
    record_event(s, entity_type="job", entity_id=job_id, actor="user", action="job_selected",
                 outcome="ok", details={"application_id": app.application_id})
    orch.enqueue_in(s, TaskKind.TAILOR, "application", app.application_id)
    s.commit()
    return app


TAILOR_RUNNING = "Tailoring is already queued or running for this application."
_TAILORABLE = (ApplicationStatus.NOT_STARTED.value, ApplicationStatus.DOCUMENTS_PENDING.value)


def start_tailor(s: Session, orch: Orchestrator, application_id: str) -> str:
    """Queue TAILOR for an application. Raises LookupError / ActionConflict."""
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    if app.status not in _TAILORABLE:
        raise ActionConflict(f"Application cannot be tailored (status is {app.status}).")
    active = s.execute(select(TaskRow.task_id).where(
        TaskRow.kind == TaskKind.TAILOR.value, TaskRow.entity_id == application_id,
        TaskRow.status.in_((TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)))).first()
    if active is not None:
        raise ActionConflict(TAILOR_RUNNING)
    task_id = orch.enqueue_in(s, TaskKind.TAILOR, "application", application_id)
    s.commit()
    return task_id


def regenerate_documents(s: Session, application_id: str,
                         reason: str = "user_regenerate") -> str:
    """User action: regenerate resume + cover letter. Commits.

    Raises LookupError / ActionConflict (busy or status not regenerable)."""
    try:
        task_id = reset_documents(s, application_id, reason, actor="user")
    except DocumentsConflict as e:
        s.rollback()
        raise ActionConflict(str(e)) from e
    s.commit()
    return task_id


@dataclass(frozen=True)
class ApprovalResult:
    approval_id: str
    batch_status: str
    approved_ids: list[str]


def approve_batch(
    s: Session, batch_id: str, version: int, presented_hash: str,
    application_ids: list[str],
) -> ApprovalResult:
    """Approve exactly what was presented. Nothing is submitted: that is a separate,
    explicit user action (``request_submit``). Commits.

    Raises StaleApprovalError / ValueError (session rolled back) like ApprovalService.
    """
    try:
        approval = ApprovalService(s).approve(batch_id, version, presented_hash,
                                              application_ids)
        batch = s.get(ApprovalBatchRow, (batch_id, version))
        assert batch is not None
        result = ApprovalResult(approval.approval_id, batch.status,
                                sorted(set(application_ids)))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return result


CONFIRM_WORD = "SUBMIT"
CONFIRM_REQUIRED = "Type SUBMIT to confirm live submission"
NOTHING_TO_SUBMIT = "No approved applications are waiting to be submitted."


class ConfirmationRequired(Exception):
    """Live submission needs the typed confirmation word (HTTP 422)."""


def submit_candidates(s: Session, batch_id: str) -> list[ApplicationRow]:
    """APPROVED applications of any version of the batch with no QUEUED/RUNNING SUBMIT."""
    ids = set(s.execute(select(BatchItemRow.application_id)
                        .where(BatchItemRow.batch_id == batch_id)).scalars())
    busy = set(s.execute(select(TaskRow.entity_id).where(
        TaskRow.kind == TaskKind.SUBMIT.value,
        TaskRow.status.in_((TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)))).scalars())
    return list(s.execute(select(ApplicationRow).where(
        ApplicationRow.application_id.in_(ids - busy),
        ApplicationRow.status == ApplicationStatus.APPROVED.value)
        .order_by(ApplicationRow.application_id)).scalars())


def request_submit(s: Session, orch: Orchestrator, batch_id: str,
                   application_ids: list[str] | None, confirm: str | None,
                   cfg: AppConfig) -> list[str]:
    """User action: submit approved applications of a batch. Marks submit_requested_at
    and queues SUBMIT for each in one transaction. Live portals need ``confirm ==
    "SUBMIT"``, which records the live intent. Returns the queued ids. Commits.

    Raises LookupError / ActionConflict / ConfirmationRequired."""
    if s.execute(select(ApprovalBatchRow.batch_id)
                 .where(ApprovalBatchRow.batch_id == batch_id)).first() is None:
        raise LookupError("batch not found")
    eligible = submit_candidates(s, batch_id)
    if application_ids is not None:
        wanted = set(application_ids)
        eligible = [a for a in eligible if a.application_id in wanted]
    if not eligible:
        raise ActionConflict(NOTHING_TO_SUBMIT)
    live = set(cfg.effective_live_portals)
    if any(a.portal in live for a in eligible) and confirm != CONFIRM_WORD:
        raise ConfirmationRequired(CONFIRM_REQUIRED)
    now = utcnow()
    try:
        for app in eligible:
            app.submit_requested_at = now
            # Live intent is recorded only with the typed confirmation for a portal that
            # is live now; the engine refuses to run live (or to simulate) without a match.
            app.submit_live_intent = (app.portal if app.portal in live
                                      and confirm == CONFIRM_WORD else None)
            orch.enqueue_in(s, TaskKind.SUBMIT, "application", app.application_id)
        ids = [a.application_id for a in eligible]
        record_event(s, entity_type="approval_batch", entity_id=batch_id, actor="user",
                     action="submit_requested", outcome="ok",
                     details={"application_ids": ids,
                              "portals": {a.application_id: a.portal for a in eligible},
                              "live": {a.application_id: a.submit_live_intent is not None
                                       for a in eligible}})
        s.commit()  # the request and its SUBMIT tasks become durable together
    except Exception:
        s.rollback()
        raise
    return ids


def _app(s: Session, application_id: str) -> ApplicationRow:
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    return app


def resolve_uncertain(s: Session, application_id: str, submitted: bool) -> None:
    """User action on a SUBMISSION_UNCERTAIN application: it was / was not submitted. A
    SUBMISSION_FAILED application with an unresolved click may also be marked "not
    submitted" (no status change). Commits. Raises LookupError / ActionConflict."""
    app = _app(s, application_id)
    if (app.status == ApplicationStatus.SUBMISSION_FAILED.value and not submitted
            and prior_unresolved_click(s, application_id)):
        # A click was recorded before the failure: the user says nothing went out, so a
        # later Prepare again may submit once more. The status stays FAILED.
        record_event(s, entity_type="application", entity_id=application_id, actor="user",
                     action=USER_RESOLVED_NOT_SUBMITTED, outcome="ok",
                     details={"application_id": application_id})
        s.commit()
        return
    if app.status != ApplicationStatus.SUBMISSION_UNCERTAIN.value:
        raise ActionConflict(f"Only uncertain submissions can be resolved "
                             f"(status is {app.status}).")
    if submitted:
        transition(s, app, ApplicationStatus.CONFIRMED, actor="user",
                   reason="user_confirmed")
    else:
        transition(s, app, ApplicationStatus.SUBMISSION_FAILED, actor="user",
                   reason="user_not_submitted")
        record_event(s, entity_type="application", entity_id=application_id, actor="user",
                     action=USER_RESOLVED_NOT_SUBMITTED, outcome="ok",
                     details={"application_id": application_id})
    s.commit()


def prepare_again(s: Session, orch: Orchestrator, application_id: str) -> str:
    """User action on a SUBMISSION_FAILED application: revoke its approval, forget the
    form snapshot and the submit request, and queue PREPARE_FORM. Commits.

    Raises LookupError / ActionConflict."""
    app = _app(s, application_id)
    if app.status != ApplicationStatus.SUBMISSION_FAILED.value:
        raise ActionConflict(f"Only failed submissions can be prepared again "
                             f"(status is {app.status}).")
    ApprovalService(s).invalidate_application(application_id, "prepare_again")
    forget_live_intent(app)  # invalidation already did; explicit for a non-approved app
    app.submit_requested_at = None
    app.form_snapshot_hash = None
    app.idempotency_key = None
    app.confirmation_reference = None
    transition(s, app, ApplicationStatus.FORM_IN_PROGRESS, actor="user",
               reason="prepare_again")
    task_id = orch.enqueue_in(s, TaskKind.PREPARE_FORM, "application", application_id)
    s.commit()
    return task_id


USER_EDITABLE_DECISIONS = (FieldDecision.ASK_USER.value, FieldDecision.USER_ONLY.value,
                           FieldDecision.FILL_AND_REVIEW.value)
ANSWERABLE_STATUSES = (ApplicationStatus.NEEDS_USER_INPUT.value,
                       ApplicationStatus.VALIDATION_FAILED.value)
_CHOICE = ("select", "radio")


def user_editable(decision: str, field_type: str) -> bool:
    return decision in USER_EDITABLE_DECISIONS and field_type != "file"


def save_user_answers(s: Session, orch: Orchestrator, application_id: str,
                      values: dict[str, Any]) -> list[str]:
    """Store the user's own answers (source "user", decision USER_ONLY) and, once every
    required field is resolved, move back to FORM_IN_PROGRESS and queue VALIDATE in the
    same transaction. Returns the still-unresolved required field ids. Commits.

    Raises LookupError / ActionConflict (status, unknown or non-editable field, bad option).
    """
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    if app.status not in ANSWERABLE_STATUSES:
        raise ActionConflict(f"Answers cannot be changed now (status is {app.status}).")
    rows = {r.field_id: r for r in answer_rows(s, application_id)}
    for fid, value in values.items():
        row = rows.get(fid)
        if row is None or not user_editable(row.decision, row.field_type):
            s.rollback()
            raise ActionConflict(f"Field {fid} cannot be answered here.")
        options = json.loads(row.options_json)
        bad = (row.field_type in _CHOICE and options and value not in options) or (
            row.field_type == "checkbox_group"
            and not (isinstance(value, list) and set(value) <= set(options)))
        if bad:
            s.rollback()
            raise ActionConflict(f"Field {fid}: choose one of the listed options.")
        set_answer(s, application_id, fid, value, actor="user", source_fact_id=USER_SOURCE,
                   decision=FieldDecision.USER_ONLY, rationale="Answered by the user",
                   confidence=1.0)
    missing = unresolved_required(s, application_id)
    if not missing:
        transition(s, app, ApplicationStatus.FORM_IN_PROGRESS, actor="user",
                   reason="user_answered")
        kind = TaskKind.VALIDATE if app.form_snapshot_hash else TaskKind.PREPARE_FORM
        orch.enqueue_in(s, kind, "application", application_id)
    s.commit()
    return missing


def reread_form(s: Session, orch: Orchestrator, application_id: str) -> str:
    """User action: forget the form snapshot and queue PREPARE_FORM. Commits.

    Raises LookupError / ActionConflict."""
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError("application not found")
    if app.status not in ANSWERABLE_STATUSES:
        raise ActionConflict(f"The form cannot be re-read now (status is {app.status}).")
    app.form_snapshot_hash = None
    transition(s, app, ApplicationStatus.FORM_IN_PROGRESS, actor="user", reason="reread_form")
    task_id = orch.enqueue_in(s, TaskKind.PREPARE_FORM, "application", application_id)
    s.commit()
    return task_id


MAX_REFERENCE_CHARS = 100
MANUAL_STATUSES = (ApplicationStatus.MANUAL_APPLY_READY.value,
                   ApplicationStatus.SUBMITTED_BY_USER.value)


def _manual_ready(s: Session, application_id: str, what: str) -> ApplicationRow:
    app = _app(s, application_id)
    if app.status != ApplicationStatus.MANUAL_APPLY_READY.value:
        raise ActionConflict(f"{what} (status is {app.status}).")
    return app


def mark_applied(s: Session, application_id: str, reference: str | None,
                 applied_on: date | None) -> None:
    """The user states they applied themselves: MANUAL_APPLY_READY -> SUBMITTED_BY_USER
    (actor "user", no evidence). The optional reference is stored; the event keeps ids,
    whether a reference was given and the date only. Commits.

    Raises LookupError / ActionConflict (wrong status or already marked) / ValueError
    (reference over 100 chars, date in the future)."""
    ref = (reference or "").strip()
    if len(ref) > MAX_REFERENCE_CHARS:
        raise ValueError(f"Reference must be at most {MAX_REFERENCE_CHARS} characters")
    if applied_on is not None and applied_on > utcnow().date():
        raise ValueError("Applied date cannot be in the future")
    app = _manual_ready(s, application_id, "Only manual applications ready to apply can be "
                        "marked as applied")
    transition(s, app, ApplicationStatus.SUBMITTED_BY_USER, actor="user",
               reason="marked_applied")
    app.confirmation_reference = ref or None
    record_event(s, entity_type="application", entity_id=application_id, actor="user",
                 action="user_marked_applied", outcome="ok",
                 details={"application_id": application_id, "job_id": app.job_id,
                          "has_reference": bool(ref),
                          "applied_on": applied_on.isoformat() if applied_on else None})
    s.commit()


def not_applying(s: Session, application_id: str) -> None:
    """The user will not apply: MANUAL_APPLY_READY -> REJECTED_BY_USER. Commits."""
    app = _manual_ready(s, application_id, "Only manual applications ready to apply can be "
                        "dropped")
    transition(s, app, ApplicationStatus.REJECTED_BY_USER, actor="user",
               reason="user_not_applying")
    s.commit()


def save_manual_answers(s: Session, application_id: str, values: dict[str, str]) -> None:
    """Store the user's edits to drafted answers (source "user"; the decision and the
    application status stay as they are). Commits.

    Raises LookupError / ActionConflict (status, unknown or non-editable field)."""
    _manual_ready(s, application_id, "Answers can only be edited before you mark the "
                  "application as applied")
    rows = {r.field_id: r for r in answer_rows(s, application_id)}
    for fid, value in values.items():
        row = rows.get(fid)
        if row is None or not user_editable(row.decision, row.field_type):
            s.rollback()
            raise ActionConflict(f"Field {fid} cannot be edited here.")
        set_answer(s, application_id, fid, value, actor="user", source_fact_id=USER_SOURCE)
    s.commit()
