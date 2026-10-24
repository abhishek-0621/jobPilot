from datetime import UTC, datetime
from typing import Any

from jobpilot.contracts.models import JobListing

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def valid_field_answer(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "field_id": "f1",
        "label": "First name",
        "field_type": "text",
        "proposed_value": "Ada",
        "decision": "AUTO_FILL",
        "source_fact_id": "fact_id_001",
        "confidence": 0.9,
        "rationale": "From verified profile.",
        "validation_errors": [],
    }
    return {**base, **overrides}


def valid_submit_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "task_1",
        "created_at": NOW,
        "application_id": "app_1",
        "status": "SUBMITTED",
        "submitted_at": NOW,
        "confirmation_reference": "CONF-1",
        "evidence_url": None,
        "evidence_artifact_id": None,
        "error_code": None,
        "retry_allowed": False,
        "next_action": "none",
    }
    return {**base, **overrides}


def valid_match(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "task_1",
        "created_at": NOW,
        "job_id": "job_1",
        "profile_version": 1,
        "total_score": 80.0,
        "skill_fit": 80.0,
        "experience_fit": 70.0,
        "project_fit": 60.0,
        "role_seniority_fit": 75.0,
        "location_fit": 100.0,
        "hard_filter_passed": True,
        "matched_evidence_fact_ids": ["fact_exp_004"],
        "missing_requirements": [],
        "concerns": [],
        "explanation": "Strong skill overlap.",
        "confidence": 0.8,
    }
    return {**base, **overrides}


def make_listing(**overrides: Any) -> JobListing:
    base: dict[str, Any] = {
        "task_id": "task_1",
        "created_at": NOW,
        "job_id": "job_1",
        "source": "greenhouse",
        "source_job_id": "123",
        "canonical_url": "https://example.com/jobs/123",
        "application_url": "https://example.com/jobs/123/apply",
        "company": "Acme",
        "title": "Backend Engineer",
        "location": "Remote",
        "work_mode": "remote",
        "description_text": "Build things.",
        "required_skills": ["python"],
        "preferred_skills": [],
        "experience_min_years": 2.0,
        "experience_max_years": 5.0,
        "posted_at": None,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "extraction_confidence": 0.9,
        "source_evidence": [],
    }
    return JobListing(**{**base, **overrides})
