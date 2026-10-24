from datetime import datetime
from typing import Annotated, Literal

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jobpilot.contracts.enums import (
    ApplicationStatus,
    FieldDecision,
    OutreachStatus,
)

Score = Annotated[float, Field(ge=0, le=100)]
Confidence = Annotated[float, Field(ge=0, le=1)]


def is_http_url(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentOutput(Contract):
    schema_version: str = "1"
    task_id: str
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)


class CandidateFact(Contract):
    fact_id: str
    category: str
    field: str
    value: str
    source_artifact: str
    source_location: str
    verified_by_user: bool
    allowed_for_resume: bool
    sensitivity: str
    updated_at: datetime


class JobListing(AgentOutput):
    job_id: str
    source: str
    source_job_id: str | None = None
    canonical_url: str
    application_url: str | None = None
    company: str
    title: str
    location: str | None = None
    work_mode: str | None = None
    description_text: str
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    experience_min_years: float | None = None
    experience_max_years: float | None = None
    posted_at: datetime | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    extraction_confidence: Confidence
    source_evidence: list[str] = Field(default_factory=list)

    @field_validator("canonical_url", "application_url")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        if v is not None and not is_http_url(v):
            raise ValueError("URL must be http(s)")
        return v


class MatchAssessment(AgentOutput):
    job_id: str
    profile_version: int
    total_score: Score
    skill_fit: Score
    experience_fit: Score
    project_fit: Score
    role_seniority_fit: Score
    location_fit: Score
    hard_filter_passed: bool
    matched_evidence_fact_ids: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    explanation: str
    confidence: Confidence


class FieldAnswer(Contract):
    field_id: str
    label: str
    field_type: str
    proposed_value: str | list[str] | bool | None = None
    decision: FieldDecision
    source_fact_id: str | None = None
    confidence: Confidence
    rationale: Annotated[str, Field(max_length=500)]
    validation_errors: list[str] = Field(default_factory=list)


class SubmitResult(AgentOutput):
    application_id: str
    status: Literal[
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.SUBMISSION_UNCERTAIN,
        ApplicationStatus.SUBMISSION_FAILED,
    ]
    submitted_at: datetime | None = None
    confirmation_reference: str | None = None
    evidence_url: str | None = None
    evidence_artifact_id: str | None = None
    error_code: str | None = None
    retry_allowed: bool
    next_action: str


class OutreachDraft(AgentOutput):
    outreach_id: str
    campaign_id: str
    company_id: str
    application_id: str
    contact_id: str | None = None
    channel: Literal["linkedin", "email", "manual"]
    contact_source_url: str | None = None
    message_text: str
    resume_artifact_id: str | None = None
    status: OutreachStatus
    content_hash: str
    user_approved_at: datetime | None = None
    sent_at: datetime | None = None
    evidence_ref: str | None = None
