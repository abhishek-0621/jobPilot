import pytest
from pydantic import ValidationError

from jobpilot.contracts.enums import (
    ApplicationStatus,
    ErrorCode,
    JobStatus,
    OutreachStatus,
    Recovery,
    recovery_for,
)
from jobpilot.contracts.models import FieldAnswer, MatchAssessment, SubmitResult
from tests.factories import make_listing, valid_field_answer, valid_match, valid_submit_result


def test_application_status_members_match_spec() -> None:
    assert [s.value for s in ApplicationStatus] == [
        "NOT_STARTED", "DOCUMENTS_PENDING", "FORM_IN_PROGRESS", "NEEDS_USER_INPUT",
        "VALIDATING", "VALIDATION_FAILED", "READY_FOR_REVIEW", "APPROVED",
        "SUBMISSION_IN_PROGRESS", "SUBMITTED", "CONFIRMED", "SUBMISSION_UNCERTAIN",
        "SUBMISSION_FAILED", "REJECTED_BY_USER", "MANUAL_APPLY_READY", "SUBMITTED_BY_USER",
    ]


def test_other_enum_members_match_spec() -> None:
    assert [s.value for s in JobStatus] == [
        "DISCOVERED", "NORMALIZED", "MATCHED", "REJECTED", "STALE", "CLOSED", "DUPLICATE",
    ]
    assert [s.value for s in OutreachStatus] == [
        "NOT_ELIGIBLE", "CONTACT_RESEARCH", "DRAFT_READY", "NEEDS_REVIEW", "APPROVED",
        "SEND_IN_PROGRESS", "SENT", "DELIVERY_UNKNOWN", "FAILED", "SKIPPED",
    ]


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        FieldAnswer(**valid_field_answer(), submit_now=True)


def test_rationale_max_length() -> None:
    with pytest.raises(ValidationError):
        FieldAnswer(**valid_field_answer(rationale="x" * 501))


def test_submit_result_rejects_non_terminal_status() -> None:
    with pytest.raises(ValidationError):
        SubmitResult(**valid_submit_result(status="APPROVED"))


def test_submit_result_accepts_terminal_status() -> None:
    assert SubmitResult(**valid_submit_result()).status is ApplicationStatus.SUBMITTED


def test_score_bounds() -> None:
    with pytest.raises(ValidationError):
        MatchAssessment(**valid_match(total_score=101))
    with pytest.raises(ValidationError):
        MatchAssessment(**valid_match(confidence=1.5))


def test_agent_output_envelope_defaults() -> None:
    listing = make_listing()
    assert listing.schema_version == "1" and listing.warnings == []


def test_recovery_mapping() -> None:
    assert recovery_for(ErrorCode.SUBMIT_TIMEOUT) is Recovery.MARK_UNCERTAIN
    assert recovery_for(ErrorCode.SOURCE_UNAVAILABLE) is Recovery.DISABLE_SOURCE
    assert recovery_for(ErrorCode.SOURCE_TIMEOUT) is Recovery.RETRY_BACKOFF
    assert recovery_for(ErrorCode.MODEL_INVALID_SCHEMA) is Recovery.RETRY_BACKOFF
    assert recovery_for(ErrorCode.AUTH_MFA) is Recovery.USER_INPUT
    assert recovery_for(ErrorCode.OUTREACH_DELIVERY_UNKNOWN) is Recovery.MARK_UNCERTAIN
    assert recovery_for(ErrorCode.OUTREACH_NO_CONTACT) is Recovery.FAIL


def test_every_error_code_has_recovery() -> None:
    for code in ErrorCode:
        assert isinstance(recovery_for(code), Recovery)


def test_job_listing_rejects_non_http_urls() -> None:
    import pytest
    from pydantic import ValidationError

    from tests.factories import make_listing

    for field in ("canonical_url", "application_url"):
        with pytest.raises(ValidationError):
            make_listing(**{field: "javascript:alert(1)"})
    ok = make_listing(application_url=None)
    assert isinstance(ok.canonical_url, str)
