"""Real submission: re-verify the approved application on the live form, then click the
single submit control exactly once and look for confirmation evidence.

Every check runs before the click; any doubt raises ``PreSubmitMismatch`` or
``ManualHandoff`` with nothing clicked. ``before_click`` durably records the click
intent first, so a crash afterwards is always treated as uncertain, never retried."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ApplicationStatus, ErrorCode, PortalCapability
from jobpilot.contracts.models import FieldAnswer, SubmitResult
from jobpilot.data.tables import ApplicationRow, AuditEventRow
from jobpilot.forms.browser import portal_page
from jobpilot.forms.verify import verify_against_stored, with_bound_files
from jobpilot.observability.events import (
    SUBMIT_CLICKED,
    click_needle,
    prior_unresolved_click,
    record_event,
    submit_click_recorded,
)
from jobpilot.portals.base import (
    ApprovedApplication,
    FillReport,
    FormSnapshot,
    ManualHandoff,
    PortalCapabilities,
    PreSubmitMismatch,
    SubmissionEvidence,
    ValidationReport,
)
from jobpilot.portals.registry import ADAPTERS, allowed_hosts, form_url

_A = ApplicationStatus
_SHOT_RE = re.compile(r"^submit_v(\d+)\.png$")
CONFIRM_TIMEOUT_S = 30.0
LIVE_SUBMIT_TIMEOUT_S = 120.0  # engine wait_for budget for one live submit


@dataclass
class SubmitContext:
    """What was approved, freshly read from the DB."""
    app: dict[str, Any]  # application_loader output (URLs, snapshot hash, bound files)
    answers: list[FieldAnswer]
    application_hash: str
    active_version: int | None
    documents_intact: bool  # bound files on disk still match their recorded hashes


SubmitLoader = Callable[[str], SubmitContext | None]
BeforeClick = Callable[[str, str], None]  # (application_id, idempotency_key)


def make_before_click(session_factory: sessionmaker[Session]) -> BeforeClick:
    """Record ``submit_clicked`` in its own committed transaction."""
    def before_click(application_id: str, key: str) -> None:
        with session_factory() as s:
            record_event(s, entity_type="application", entity_id=application_id,
                         actor="orchestrator", action=SUBMIT_CLICKED, outcome="ok",
                         details={"idempotency_key": key})
            s.commit()
    return before_click


def _next_shot(directory: Path) -> int:
    found = [int(m.group(1)) for p in directory.iterdir() if (m := _SHOT_RE.match(p.name))]
    return max(found, default=0) + 1


def documents_current(ctx: SubmitContext) -> bool:
    if ctx.active_version is None or not ctx.documents_intact:
        return False
    app = ctx.app
    bound = [(app.get("resume_artifact_id"), app.get("resume_profile_version")),
             (app.get("cover_letter_artifact_id"), app.get("cover_letter_profile_version"))]
    return all(v == ctx.active_version for art, v in bound if art is not None)


class LiveSubmitter:
    def __init__(self, cfg: AppConfig, portal: str, loader: SubmitLoader,
                 before_click: BeforeClick, session_factory: sessionmaker[Session],
                 *, confirm_timeout_s: float = CONFIRM_TIMEOUT_S,
                 timeout_s: float = LIVE_SUBMIT_TIMEOUT_S) -> None:
        self.cfg = cfg
        self.timeout_s = timeout_s
        self.name = portal
        self._loader = loader
        self._before_click = before_click
        self._sf = session_factory
        self._confirm_timeout_s = confirm_timeout_s
        self._evidence: SubmissionEvidence | None = None

    async def capabilities(self) -> PortalCapabilities:
        return frozenset({PortalCapability.PREPARE_APPLICATION,
                          PortalCapability.SUBMIT_APPLICATION})

    def _load(self, application_id: str) -> SubmitContext:
        ctx = self._loader(application_id)
        if ctx is None:
            raise PreSubmitMismatch("APPLICATION_NOT_FOUND")
        return ctx

    async def submit(self, approved_application: ApprovedApplication) -> SubmitResult:
        app_id = approved_application.application_id
        ctx = self._load(app_id)
        cls = ADAPTERS.get(self.name)
        if cls is None or ctx.app.get("source") != self.name:
            raise PreSubmitMismatch("PORTAL_MISMATCH")
        url = form_url(ctx.app)  # PortalConfigError: mapped by the engine, nothing clicked
        upload_root = self.cfg.storage_path / "artifacts" / app_id
        upload_root.mkdir(parents=True, exist_ok=True)
        answers = with_bound_files(ctx.answers, ctx.app)
        async with portal_page(self.cfg, portal=self.name,
                               allowed_hosts=allowed_hosts(cls, url)) as session:
            adapter = cls(session.page, upload_root=upload_root)
            outcome = await verify_against_stored(adapter, url,
                                                  ctx.app.get("form_snapshot_hash"), answers)
            if outcome.blockers:
                raise ManualHandoff("BLOCKED:" + ",".join(outcome.blockers))
            if outcome.layout_changed:
                raise PreSubmitMismatch(ErrorCode.FORM_LAYOUT_CHANGED.value)
            if not outcome.ok:
                raise PreSubmitMismatch("VALUE_MISMATCH")
            fresh = self._load(app_id)  # re-read: nothing may have changed meanwhile
            if not documents_current(fresh):
                raise PreSubmitMismatch("STALE_DOCUMENTS")
            if (not approved_application.approved_hash
                    or fresh.application_hash != approved_application.approved_hash):
                raise PreSubmitMismatch("HASH_MISMATCH")
            late = await adapter.blockers()  # a challenge may appear while filling
            if late:
                raise ManualHandoff("BLOCKED:" + ",".join(sorted(late)))
            control = await adapter.locate_submit_control()
            baseline = await adapter.capture_baseline()
            self._before_click(app_id, approved_application.idempotency_key)
            # ---- past this line the application may have been submitted ----
            await adapter.click_submit_once(control)
            confirmation = await adapter.detect_confirmation(baseline,
                                                             self._confirm_timeout_s)
            shot: Path | None = upload_root / f"submit_v{_next_shot(upload_root)}.png"
            try:
                await adapter.screenshot(shot)  # type: ignore[arg-type]
            except Exception:  # evidence is best effort; the outcome stands
                shot = None
        now = datetime.now(UTC)
        self._evidence = SubmissionEvidence(confirmation_reference=confirmation.reference,
                                            evidence_url=None, captured_at=now)
        status = confirmation.status
        return SubmitResult(
            task_id=approved_application.idempotency_key, created_at=now,
            application_id=app_id, status=status,  # type: ignore[arg-type]
            submitted_at=now, confirmation_reference=confirmation.reference,
            evidence_path=None if shot is None else str(shot),
            error_code=None if status is _A.CONFIRMED
            else ErrorCode.SUBMIT_NO_CONFIRMATION.value,
            error_codes=list(confirmation.error_codes),
            retry_allowed=False,
            next_action="none" if status is _A.CONFIRMED else "check_application_history")

    async def verify_submission(self) -> SubmissionEvidence:
        if self._evidence is None:
            return SubmissionEvidence(confirmation_reference=None, evidence_url=None,
                                      captured_at=datetime.now(UTC))
        return self._evidence

    async def find_existing_submission(self, idempotency_key: str) -> SubmitResult | None:
        """A recorded click for this key, or any click for the application the user has
        not resolved as "not submitted", means the outcome is unknown: never click again."""
        with self._sf() as s:
            app_id = s.execute(select(ApplicationRow.application_id).where(
                ApplicationRow.idempotency_key == idempotency_key).limit(1)).scalar()
            if not submit_click_recorded(s, idempotency_key) and not (
                    app_id is not None and prior_unresolved_click(s, app_id)):
                return None
            if app_id is None:
                app_id = s.execute(select(AuditEventRow.entity_id).where(
                    AuditEventRow.action == SUBMIT_CLICKED,
                    AuditEventRow.redacted_details_json.contains(
                        click_needle(idempotency_key))).limit(1)).scalar()
        return SubmitResult(task_id=idempotency_key, created_at=datetime.now(UTC),
                            application_id=app_id or "", status=_A.SUBMISSION_UNCERTAIN,
                            error_code=ErrorCode.SUBMIT_NO_CONFIRMATION.value,
                            retry_allowed=False, next_action="check_application_history")

    # The engine only submits through this adapter; form reading lives in VALIDATE.
    async def open_job(self, url: str) -> None:
        raise NotImplementedError

    async def inspect_form(self) -> FormSnapshot:
        raise NotImplementedError

    async def fill_fields(self, answers: list[FieldAnswer]) -> FillReport:
        raise NotImplementedError

    async def validate_form(self, expected: dict[str, Any] | None = None) -> ValidationReport:
        raise NotImplementedError
