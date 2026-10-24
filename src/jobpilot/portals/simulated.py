from datetime import UTC, datetime
from typing import Literal

from jobpilot.contracts.enums import ApplicationStatus, PortalCapability
from jobpilot.contracts.models import FieldAnswer, JobListing, SubmitResult
from jobpilot.portals.base import (
    ApprovedApplication,
    FillReport,
    FormField,
    FormSnapshot,
    PortalAdapter,
    PortalCapabilities,
    SubmissionEvidence,
    ValidationReport,
)

_DEFAULT_FORM = FormSnapshot(
    url="simulated://form",
    fields=[
        FormField(field_id="full_name", label="Full name", field_type="text", required=True),
        FormField(field_id="email", label="Email", field_type="email", required=True),
        FormField(field_id="resume", label="Resume", field_type="file", required=True),
    ],
)


class SimulatedAdapter(PortalAdapter):
    name = "simulated"

    def __init__(
        self,
        fail_mode: Literal[None, "timeout", "no_confirmation"] = None,
        form: FormSnapshot | None = None,
    ) -> None:
        self.fail_mode = fail_mode
        self.form = (form or _DEFAULT_FORM).model_copy(deep=True)
        self.submit_calls: int = 0
        self.submissions: dict[str, SubmitResult] = {}
        self._filled: list[str] = []
        self._last_key: str | None = None

    @property
    def real_submissions(self) -> int:
        return 0

    @property
    def simulated_submissions(self) -> int:
        return len(self.submissions)

    async def capabilities(self) -> PortalCapabilities:
        return frozenset(PortalCapability) - {PortalCapability.SEND_OUTREACH}

    async def open_job(self, job: JobListing) -> None:
        self._filled = []

    async def inspect_form(self) -> FormSnapshot:
        return self.form

    async def fill_fields(self, answers: list[FieldAnswer]) -> FillReport:
        known = {f.field_id for f in self.form.fields}
        filled = [a.field_id for a in answers if a.field_id in known]
        failed = {a.field_id: "unknown field" for a in answers if a.field_id not in known}
        self._filled = filled
        return FillReport(filled=filled, failed=failed)

    async def validate_form(self) -> ValidationReport:
        errors = {
            f.field_id: "required"
            for f in self.form.fields
            if f.required and f.field_id not in self._filled
        }
        return ValidationReport(ok=not errors, errors=errors)

    async def submit(self, approved_application: ApprovedApplication) -> SubmitResult:
        self.submit_calls += 1
        key = approved_application.idempotency_key
        self._last_key = key
        if key in self.submissions:
            return self.submissions[key]
        if self.fail_mode == "timeout":
            raise TimeoutError
        now = datetime.now(UTC)
        short = key.split(":", 1)[-1][:8]
        uncertain = self.fail_mode == "no_confirmation"
        result = SubmitResult(
            task_id=key,
            created_at=now,
            application_id=approved_application.application_id,
            status=(
                ApplicationStatus.SUBMISSION_UNCERTAIN
                if uncertain
                else ApplicationStatus.CONFIRMED
            ),
            submitted_at=now,
            confirmation_reference=None if uncertain else f"SIM-{short}",
            evidence_url=None if uncertain else f"simulated://confirmation/{short}",
            retry_allowed=False,
            next_action="check_application_history" if uncertain else "none",
        )
        self.submissions[key] = result
        return result

    async def verify_submission(self) -> SubmissionEvidence:
        result = self.submissions.get(self._last_key) if self._last_key else None
        return SubmissionEvidence(
            confirmation_reference=result.confirmation_reference if result else None,
            evidence_url=result.evidence_url if result else None,
            captured_at=datetime.now(UTC),
        )

    async def find_existing_submission(self, idempotency_key: str) -> SubmitResult | None:
        return self.submissions.get(idempotency_key)
