"""The only sanctioned writers of approval-relevant application content.

Every function flushes but never commits, and records events that carry ids/keys only,
never values. Content changes invalidate any approval covering the application.
"""

import json
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import FieldDecision
from jobpilot.contracts.models import FieldAnswer, is_http_url
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import AnswerLibraryRow, DocumentArtifactRow, FormFieldAnswerRow, JobRow
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.profile.importer import _DEFAULT_DECISION, _decision_defaults, active_profile
from jobpilot.util.hashing import canonical_json


def _answer_row(s: Session, application_id: str, field_id: str) -> FormFieldAnswerRow | None:
    return s.execute(select(FormFieldAnswerRow).where(
        FormFieldAnswerRow.application_id == application_id,
        FormFieldAnswerRow.field_id == field_id,
    )).scalar_one_or_none()


def set_answer(s: Session, application_id: str, field_id: str, value: Any, *,
               actor: str, source_fact_id: str | None = None,
               decision: FieldDecision | None = None, rationale: str | None = None,
               confidence: float | None = None) -> None:
    """Change one stored answer (value, plus optional provenance/decision overrides)."""
    row = _answer_row(s, application_id, field_id)
    if row is None:
        raise LookupError(f"unknown field {field_id} on {application_id}")
    row.proposed_value_json = canonical_json(value)
    if source_fact_id is not None:
        row.source_fact_id = source_fact_id
    if decision is not None:
        row.decision = FieldDecision(decision).value
    if rationale is not None:
        row.rationale = rationale[:500]
    if confidence is not None:
        row.confidence = confidence
    record_event(s, entity_type="application", entity_id=application_id, actor=actor,
                 action="answer_changed", outcome="ok", details={"field_id": field_id})
    s.flush()
    ApprovalService(s).invalidate_application(application_id, reason="answer_changed")


def upsert_answer_initial(s: Session, application_id: str, answer: FieldAnswer, *,
                          required: bool, options: list[str], actor: str) -> None:
    """Insert a first-time answer row. Only allowed while nothing about the application
    has been approved and the field has no row yet (so nothing needs invalidating)."""
    if _answer_row(s, application_id, answer.field_id) is not None:
        raise ValueError(f"field {answer.field_id} already has an answer")
    if ApprovalService(s).active_approval_for(application_id) is not None:
        raise ValueError(f"application {application_id} has an active approval")
    s.add(FormFieldAnswerRow(
        application_id=application_id, field_id=answer.field_id, label=answer.label,
        field_type=answer.field_type, proposed_value_json=canonical_json(answer.proposed_value),
        decision=answer.decision.value, source_fact_id=answer.source_fact_id,
        confidence=answer.confidence, rationale=answer.rationale[:500],
        required=required, options_json=canonical_json(options)))
    record_event(s, entity_type="application", entity_id=application_id, actor=actor,
                 action="answer_created", outcome="ok", details={"field_id": answer.field_id})
    s.flush()


def remove_answer(s: Session, application_id: str, field_id: str, *, actor: str) -> None:
    """Drop the answer of a field that no longer exists on the form."""
    row = _answer_row(s, application_id, field_id)
    if row is None:
        raise LookupError(f"unknown field {field_id} on {application_id}")
    s.delete(row)
    record_event(s, entity_type="application", entity_id=application_id, actor=actor,
                 action="answer_removed", outcome="ok", details={"field_id": field_id})
    s.flush()
    ApprovalService(s).invalidate_application(application_id, reason="answer_removed")


def set_artifact(s: Session, application_id: str,
                 kind: Literal["resume", "cover_letter"], artifact_id: str, *,
                 actor: str) -> None:
    app = get_application(s, application_id)
    artifact = s.get(DocumentArtifactRow, artifact_id)
    if artifact is None:
        raise LookupError(f"unknown artifact {artifact_id}")
    if artifact.application_id != application_id or artifact.type != kind:
        raise ValueError(f"artifact {artifact_id} is not a {kind} of {application_id}")
    if kind == "resume":
        app.resume_artifact_id = artifact_id
    elif kind == "cover_letter":
        app.cover_letter_artifact_id = artifact_id
    else:
        raise ValueError(f"unknown artifact kind {kind}")
    record_event(s, entity_type="application", entity_id=application_id, actor=actor,
                 action="artifact_changed", outcome="ok",
                 details={"kind": kind, "artifact_id": artifact_id})
    s.flush()
    ApprovalService(s).invalidate_application(application_id, reason="artifact_changed")


def set_application_url(s: Session, application_id: str, url: str, *, actor: str) -> None:
    if not is_http_url(url):
        raise ValueError("application URL must be http(s)")
    app = get_application(s, application_id)
    job = s.get(JobRow, app.job_id)
    if job is None:
        raise LookupError(f"unknown job for {application_id}")
    job.application_url = url
    record_event(s, entity_type="application", entity_id=application_id, actor=actor,
                 action="application_url_changed", outcome="ok", details={})
    s.flush()
    ApprovalService(s).invalidate_application(application_id, reason="application_url_changed")


def set_library_answer(s: Session, question_key: str, value: Any, *,
                       actor: str = "user") -> None:
    """Upsert (or, for ``None``, delete) an answer on the active profile version.

    Intentionally does NOT invalidate approvals: an approval binds each application's own
    copied form answers (FormFieldAnswerRow), not the library, so editing the library
    only affects applications prepared afterwards.
    """
    version, _ = active_profile(s)
    row = s.execute(select(AnswerLibraryRow).where(
        AnswerLibraryRow.profile_version == version,
        AnswerLibraryRow.question_key == question_key,
    )).scalar_one_or_none()
    if value is None:
        if row is not None:
            s.delete(row)
    elif row is None:
        s.add(AnswerLibraryRow(
            answer_id=new_id("ans"), profile_version=version, question_key=question_key,
            value_json=json.dumps(value),
            decision_default=_decision_defaults().get(question_key, _DEFAULT_DECISION)))
    else:
        row.value_json = json.dumps(value)
    record_event(s, entity_type="answer_library", entity_id=question_key, actor=actor,
                 action="answer_library_changed", outcome="ok",
                 details={"question_key": question_key, "cleared": value is None})
    s.flush()


def clear_application_content(s: Session, application_id: str) -> None:
    """Data deletion: drop the user content an application row holds. The idempotency key
    stays (submit-click checks key on it). Callers invalidate approvals and record the
    deletion event."""
    app = get_application(s, application_id)
    app.confirmation_reference = None
    app.content_hash = None
    app.form_snapshot_hash = None
    app.submit_live_intent = None
    app.resume_artifact_id = None
    app.cover_letter_artifact_id = None
    s.flush()


def clear_artifact_refs(s: Session, application_id: str) -> None:
    """Retention/deletion removed the documents: unbind them from the application."""
    app = get_application(s, application_id)
    app.resume_artifact_id = None
    app.cover_letter_artifact_id = None
    s.flush()
