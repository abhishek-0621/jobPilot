"""The only sanctioned writers of approval-relevant application content.

Every function flushes but never commits, and records events that carry ids/keys only,
never values. Content changes invalidate any approval covering the application.
"""

import json
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.contracts.models import is_http_url
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import AnswerLibraryRow, DocumentArtifactRow, FormFieldAnswerRow, JobRow
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.profile.importer import _DEFAULT_DECISION, _decision_defaults, active_profile
from jobpilot.util.hashing import canonical_json


def set_answer(s: Session, application_id: str, field_id: str, value: Any, *,
               actor: str) -> None:
    row = s.execute(select(FormFieldAnswerRow).where(
        FormFieldAnswerRow.application_id == application_id,
        FormFieldAnswerRow.field_id == field_id,
    )).scalar_one_or_none()
    if row is None:
        raise LookupError(f"unknown field {field_id} on {application_id}")
    row.proposed_value_json = canonical_json(value)
    record_event(s, entity_type="application", entity_id=application_id, actor=actor,
                 action="answer_changed", outcome="ok", details={"field_id": field_id})
    s.flush()
    ApprovalService(s).invalidate_application(application_id, reason="answer_changed")


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
