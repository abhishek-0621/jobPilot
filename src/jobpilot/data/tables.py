from datetime import datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from jobpilot.data.db import Base, UTCDateTime, utcnow


def _ts(**kw: object) -> Mapped[datetime]:
    return mapped_column(UTCDateTime(), **kw)  # type: ignore[arg-type]


class CandidateProfileRow(Base):
    __tablename__ = "candidate_profile"
    profile_id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    preferences_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = _ts(default=utcnow)


class CandidateFactRow(Base):
    __tablename__ = "candidate_fact"
    fact_id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_version: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String)
    field: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text)
    source_artifact: Mapped[str | None] = mapped_column(String, nullable=True)
    source_location: Mapped[str | None] = mapped_column(String, nullable=True)
    verified_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_for_resume: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitivity: Mapped[str] = mapped_column(String, default="normal")


class AnswerLibraryRow(Base):
    __tablename__ = "answer_library"
    answer_id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_version: Mapped[int] = mapped_column(Integer)
    question_key: Mapped[str] = mapped_column(String)
    value_json: Mapped[str] = mapped_column(Text)
    decision_default: Mapped[str] = mapped_column(String)


class JobRow(Base):
    __tablename__ = "job"
    __table_args__ = (UniqueConstraint("source", "source_job_id"),)
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String)
    source_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    canonical_url: Mapped[str] = mapped_column(String)
    application_url: Mapped[str | None] = mapped_column(String, nullable=True)
    company: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    description_text: Mapped[str] = mapped_column(Text)
    extracted_json: Mapped[str] = mapped_column(Text, default="{}")
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True)
    canonical_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job.job_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String)
    first_seen_at: Mapped[datetime] = _ts()
    last_seen_at: Mapped[datetime] = _ts()


class MatchAssessmentRow(Base):
    __tablename__ = "match_assessment"
    job_id: Mapped[str] = mapped_column(ForeignKey("job.job_id"), primary_key=True)
    profile_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    skill_fit: Mapped[float] = mapped_column(Float)
    experience_fit: Mapped[float] = mapped_column(Float)
    project_fit: Mapped[float] = mapped_column(Float)
    role_seniority_fit: Mapped[float] = mapped_column(Float)
    location_fit: Mapped[float] = mapped_column(Float)
    total_score: Mapped[float] = mapped_column(Float)
    hard_filter_passed: Mapped[bool] = mapped_column(Boolean)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    confidence: Mapped[float] = mapped_column(Float)


class ApplicationRow(Base):
    __tablename__ = "application"
    application_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.job_id"), unique=True)
    portal: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resume_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cover_letter_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    form_snapshot_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    submit_requested_at: Mapped[datetime | None] = _ts(nullable=True)
    # portal named when the user typed SUBMIT while that portal was live; else None
    submit_live_intent: Mapped[str | None] = mapped_column(String, nullable=True)


class DocumentArtifactRow(Base):
    __tablename__ = "document_artifact"
    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("application.application_id")
    )
    type: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_fact_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FormFieldAnswerRow(Base):
    __tablename__ = "form_field_answer"
    application_id: Mapped[str] = mapped_column(
        ForeignKey("application.application_id"), primary_key=True
    )
    field_id: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    field_type: Mapped[str] = mapped_column(String)
    proposed_value_json: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(String)
    source_fact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(String(500))
    validation_errors_json: Mapped[str] = mapped_column(Text, default="[]")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    options_json: Mapped[str] = mapped_column(Text, default="[]")


class ApprovalBatchRow(Base):
    __tablename__ = "approval_batch"
    batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class BatchItemRow(Base):
    __tablename__ = "batch_item"
    batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("application.application_id"), primary_key=True
    )
    item_hash: Mapped[str] = mapped_column(String)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)


class ApprovalRow(Base):
    __tablename__ = "approval"
    approval_id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String)
    batch_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String)
    approved_item_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    revoked_item_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    approved_at: Mapped[datetime] = _ts(default=utcnow)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)


class ContactCandidateRow(Base):
    __tablename__ = "contact_candidate"
    contact_id: Mapped[str] = mapped_column(String, primary_key=True)
    company: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    professional_title: Mapped[str | None] = mapped_column(String, nullable=True)
    public_source_url: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    verified_at: Mapped[datetime | None] = _ts(nullable=True)


class OutreachDraftRow(Base):
    __tablename__ = "outreach_draft"
    __table_args__ = (UniqueConstraint("company_key", "campaign_id"),)
    outreach_id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("application.application_id"), nullable=True
    )
    company_key: Mapped[str] = mapped_column(String)
    campaign_id: Mapped[str] = mapped_column(String)
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contact_candidate.contact_id"), nullable=True
    )
    channel: Mapped[str | None] = mapped_column(String, nullable=True)
    message_text: Mapped[str] = mapped_column(Text)
    resume_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    sent_at: Mapped[datetime | None] = _ts(nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(String, nullable=True)


class AgentRunRow(Base):
    __tablename__ = "agent_run"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    model_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    input_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    output_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)


class TaskRow(Base):
    __tablename__ = "task"
    __table_args__ = (Index("ix_task_status_not_before", "status", "not_before"),)
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    not_before: Mapped[datetime | None] = _ts(nullable=True)
    lease_until: Mapped[datetime | None] = _ts(nullable=True)


class AuditEventRow(Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_event_entity_id", "entity_id"),)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp_utc: Mapped[datetime] = _ts(default=utcnow)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String)
    redacted_details_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)


class ControlFlagRow(Base):
    __tablename__ = "control_flag"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = _ts(default=utcnow)
