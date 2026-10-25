"""The only sanctioned writers of approval-relevant application content.

Every function flushes but never commits, and records events that carry ids/keys only,
never values. Content changes invalidate any approval covering the application.
"""

import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import FieldDecision, OutreachStatus
from jobpilot.contracts.models import FieldAnswer, is_http_url
from jobpilot.data.db import utcnow
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import (
    AnswerLibraryRow,
    ContactCandidateRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    OutreachDraftRow,
)
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.state_machine import transition
from jobpilot.outreach.contacts import (
    USER_CONFIDENCE,
    FoundContact,
    email_domain_ok,
    public_http_url,
)
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


# ---- outreach --------------------------------------------------------------------
def _contact_row(company: str, application_id: str | None, c: FoundContact,
                 verified: bool = False) -> ContactCandidateRow:
    return ContactCandidateRow(
        contact_id=new_id("con"), company=company, name=c.name[:200],
        professional_title=(c.professional_title or None) and c.professional_title[:200],
        public_source_url=c.public_source_url, channel=c.channel, confidence=c.confidence,
        address=c.address, kind=c.kind, application_id=application_id,
        verified_at=utcnow() if verified else None, created_at=utcnow())


def create_outreach_draft(s: Session, *, application_id: str, company: str, company_key: str,
                          campaign_id: str, contacts: list[FoundContact], message_text: str,
                          actor: str) -> OutreachDraftRow:
    """Store the researched contacts (best first) and one NOT_ELIGIBLE draft bound to the
    best one. The (company_key, campaign_id) constraint allows one draft per company."""
    rows = [_contact_row(company, application_id, c) for c in contacts]
    s.add_all(rows)
    s.flush()
    draft = OutreachDraftRow(
        outreach_id=new_id("out"), application_id=application_id, company_key=company_key,
        campaign_id=campaign_id, contact_id=rows[0].contact_id if rows else None,
        channel=rows[0].channel if rows else None, message_text=message_text,
        status=OutreachStatus.NOT_ELIGIBLE.value, created_at=utcnow())
    s.add(draft)
    s.flush()
    record_event(s, entity_type="outreach", entity_id=draft.outreach_id, actor=actor,
                 action="outreach_draft_created", outcome="ok",
                 details={"application_id": application_id, "contacts_n": len(rows)})
    return draft


def detach_contacts(s: Session, contact_ids: list[str], *, reason: str) -> int:
    """Unbind contacts (about to be removed) from any draft; approvals are revoked."""
    if not contact_ids:
        return 0
    drafts = list(s.execute(select(OutreachDraftRow).where(
        OutreachDraftRow.contact_id.in_(contact_ids))).scalars())
    for d in drafts:
        ApprovalService(s).invalidate_outreach(d.outreach_id, reason)
        d.contact_id = None
        d.channel = None
        record_event(s, entity_type="outreach", entity_id=d.outreach_id, actor="system",
                     action="outreach_contact_detached", outcome="ok",
                     details={"reason": reason})
    s.flush()
    return len(drafts)


def delete_outreach_for_application(s: Session, application_id: str) -> tuple[int, int]:
    """Data deletion: remove the application's drafts and contacts (events stay).
    Returns (contacts removed, drafts removed)."""
    drafts = list(s.execute(select(OutreachDraftRow).where(
        OutreachDraftRow.application_id == application_id)).scalars())
    svc = ApprovalService(s)
    for d in drafts:
        svc.invalidate_outreach(d.outreach_id, "data_deleted")
        s.delete(d)
    s.flush()
    contacts = list(s.execute(select(ContactCandidateRow).where(
        ContactCandidateRow.application_id == application_id)).scalars())
    detach_contacts(s, [c.contact_id for c in contacts], reason="data_deleted")
    for c in contacts:
        s.delete(c)
    s.flush()
    return len(contacts), len(drafts)


MAX_MESSAGE_CHARS = 3000
EDITABLE_OUTREACH = (OutreachStatus.DRAFT_READY.value, OutreachStatus.NEEDS_REVIEW.value,
                     OutreachStatus.APPROVED.value)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)")
_LINKEDIN_HOSTS = ("linkedin.com",)


def _draft(s: Session, outreach_id: str) -> OutreachDraftRow:
    d = s.get(OutreachDraftRow, outreach_id)
    if d is None:
        raise LookupError(f"unknown outreach draft {outreach_id}")
    return d


def _editable(d: OutreachDraftRow) -> None:
    if d.status not in EDITABLE_OUTREACH:
        raise PermissionError(f"outreach draft is {d.status}; it cannot be changed")


def set_outreach_message(s: Session, outreach_id: str, text: str, *, actor: str) -> None:
    """The user's edit of the message; any outreach approval is revoked."""
    d = _draft(s, outreach_id)
    _editable(d)
    text = text.replace("\r\n", "\n").strip()
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message must be 1-{MAX_MESSAGE_CHARS} characters")
    d.message_text = text
    record_event(s, entity_type="outreach", entity_id=outreach_id, actor=actor,
                 action="outreach_message_changed", outcome="ok", details={})
    s.flush()
    ApprovalService(s).invalidate_outreach(outreach_id, "message_changed")


def choose_contact(s: Session, outreach_id: str, contact_id: str, *, actor: str) -> None:
    """Switch to another contact of the same application; approval is revoked."""
    d = _draft(s, outreach_id)
    _editable(d)
    c = s.get(ContactCandidateRow, contact_id)
    if c is None or c.application_id is None or c.application_id != d.application_id:
        raise PermissionError("that contact does not belong to this draft")
    d.contact_id, d.channel = c.contact_id, c.channel
    record_event(s, entity_type="outreach", entity_id=outreach_id, actor=actor,
                 action="outreach_contact_changed", outcome="ok",
                 details={"contact_id": contact_id})
    s.flush()
    ApprovalService(s).invalidate_outreach(outreach_id, "contact_changed")


def add_contact(s: Session, outreach_id: str, *, name: str, title: str | None,
                channel: str, address: str, source_url: str, company_domain: str | None,
                company: str, actor: str = "user") -> str:
    """A contact the user found themselves (e.g. a LinkedIn recruiter). Emails must be on
    the company's domain; LinkedIn contacts must be linkedin.com profile URLs. The draft
    switches to it (approval revoked); a draft skipped for no public contact returns to
    review. Returns the contact id."""
    d = _draft(s, outreach_id)
    revive = d.status == OutreachStatus.SKIPPED.value and d.skip_reason == "no_public_contact"
    if not revive:
        _editable(d)
    name, address = name.strip()[:200], address.strip()
    if not name:
        raise ValueError("name is required")
    if public_http_url(source_url.strip()) is None:
        raise ValueError("source URL must be a public http(s) URL")
    if channel == "email":
        m = _EMAIL.fullmatch(address)
        if m is None:
            raise ValueError("enter a valid email address")
        if not email_domain_ok(m.group(1), company_domain, company):
            raise ValueError("email must be on the company's own domain")
        address = address.lower()
    elif channel == "linkedin":
        url = public_http_url(address)
        host = "" if url is None else (urlsplit(url).hostname or "").lower()
        if url is None or not any(host == h or host.endswith("." + h)
                                  for h in _LINKEDIN_HOSTS):
            raise ValueError("LinkedIn contacts need a linkedin.com profile URL")
    else:
        raise ValueError("channel must be email or linkedin")
    row = _contact_row(company, d.application_id, FoundContact(
        kind="user", name=name, professional_title=(title or "").strip() or None,
        channel=channel, address=address,  # type: ignore[arg-type]
        public_source_url=source_url.strip(), confidence=USER_CONFIDENCE), verified=True)
    s.add(row)
    s.flush()
    record_event(s, entity_type="outreach", entity_id=outreach_id, actor=actor,
                 action="outreach_contact_added", outcome="ok",
                 details={"contact_id": row.contact_id, "channel": channel})
    d.contact_id, d.channel = row.contact_id, row.channel
    if revive:
        d.skip_reason = None
        transition(s, d, OutreachStatus.DRAFT_READY, actor=actor, reason="user_contact")
        transition(s, d, OutreachStatus.NEEDS_REVIEW, actor=actor, reason="user_contact")
    s.flush()
    ApprovalService(s).invalidate_outreach(outreach_id, "contact_added")
    return row.contact_id
