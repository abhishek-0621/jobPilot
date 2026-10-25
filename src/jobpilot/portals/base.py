from datetime import datetime
from typing import Any, Protocol

from jobpilot.contracts.enums import PortalCapability
from jobpilot.contracts.models import Contract, FieldAnswer, SubmitResult
from jobpilot.util.hashing import content_hash

PortalCapabilities = frozenset[PortalCapability]


class PreSubmitMismatch(Exception):
    """Pre-submit verification failed before any click; ``code`` is a reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ManualHandoff(Exception):
    """The user must act on the portal (blocker, ambiguous control); nothing was clicked."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FormField(Contract):
    field_id: str
    label: str
    field_type: str
    required: bool
    options: list[str] = []
    help_text: str = ""


class FormSnapshot(Contract):
    url: str
    fields: list[FormField]
    warnings: list[str] = []


class FillReport(Contract):
    filled: list[str]
    failed: dict[str, str]


class ValidationReport(Contract):
    ok: bool
    errors: dict[str, str]


class SubmissionEvidence(Contract):
    confirmation_reference: str | None
    evidence_url: str | None
    captured_at: datetime


class ApprovedApplication(Contract):
    application_id: str
    approved_hash: str
    idempotency_key: str


class PortalAdapter(Protocol):
    name: str

    async def capabilities(self) -> PortalCapabilities: ...
    async def open_job(self, url: str) -> None: ...
    async def inspect_form(self) -> FormSnapshot: ...
    async def fill_fields(self, answers: list[FieldAnswer]) -> FillReport: ...
    async def validate_form(self, expected: dict[str, Any] | None = None) -> ValidationReport: ...
    async def submit(self, approved_application: ApprovedApplication) -> SubmitResult: ...
    async def verify_submission(self) -> SubmissionEvidence: ...
    async def find_existing_submission(self, idempotency_key: str) -> SubmitResult | None: ...


def idempotency_key(application_id: str, portal: str, action: str, version: str) -> str:
    return content_hash([application_id, portal, action, version])
