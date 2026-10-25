"""Response/request models for the localhost API; ORM rows are never returned directly."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthOut(_Out):
    ollama: bool
    missing_models: list[str]
    db_revision: str
    disk_free_gb: float
    dry_run: bool
    live_portals: list[str]


class JobSummary(_Out):
    job_id: str
    source: str
    company: str
    title: str
    location: str | None
    status: str
    canonical_url: str
    total_score: float | None


class MatchOut(_Out):
    total_score: float
    skill_fit: float
    experience_fit: float
    project_fit: float
    role_seniority_fit: float
    location_fit: float
    hard_filter_passed: bool
    confidence: float


class EventOut(_Out):
    event_id: str
    timestamp_utc: datetime
    entity_type: str
    entity_id: str
    actor: str
    action: str
    outcome: str
    redacted_details: dict[str, Any]
    error_code: str | None


class JobDetail(JobSummary):
    application_url: str | None
    work_mode: str | None
    description_text: str
    match: MatchOut | None
    events: list[EventOut]


class JobInfo(_Out):
    job_id: str
    title: str
    company: str
    canonical_url: str
    application_url: str | None


class ArtifactOut(_Out):
    artifact_id: str
    type: str
    content_hash: str
    version: int


class AnswerOut(_Out):
    field_id: str
    label: str
    field_type: str
    proposed_value: Any
    decision: str
    confidence: float
    rationale: str
    validation_errors: list[Any]


class ApplicationDetail(_Out):
    application_id: str
    status: str
    portal: str | None
    confirmation_reference: str | None
    job: JobInfo
    artifacts: list[ArtifactOut]
    answers: list[AnswerOut]
    events: list[EventOut]


class BatchCreate(BaseModel):
    application_ids: list[str]


class BatchItemOut(_Out):
    application_id: str
    item_hash: str
    selected: bool


class BatchOut(_Out):
    batch_id: str
    version: int
    status: str
    content_hash: str | None
    items: list[BatchItemOut]


class ApproveIn(BaseModel):
    version: int
    content_hash: str
    application_ids: list[str]


class SubmitIn(BaseModel):
    application_ids: list[str] | None = None
    confirm: str | None = None


class SubmitOut(_Out):
    queued: list[str]


class ApprovalOut(_Out):
    approval_id: str
    batch_status: str


class ControlOut(_Out):
    action: str
    in_flight: list[str]


class OverviewOut(_Out):
    jobs: dict[str, int]
    applications: dict[str, int]
    outreach: dict[str, int]
    batches: dict[str, int]
    pending_approvals: int
    recent_failures: list[EventOut]
