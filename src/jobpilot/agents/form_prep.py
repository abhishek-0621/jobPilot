"""PREPARE_FORM and VALIDATE: read the portal form, decide every field, fill stored
answers and validate them, gate to READY_FOR_REVIEW. Nothing here ever submits."""

import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.agents.essay import draft_essay_answers, essay_candidates, guard_essay_answers
from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ApplicationStatus, ErrorCode, FieldDecision
from jobpilot.contracts.models import AgentOutput, FieldAnswer
from jobpilot.data import mutations
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import FormFieldAnswerRow, JobRow, TaskRow
from jobpilot.documents import store
from jobpilot.forms.browser import portal_page
from jobpilot.forms.essay import classify_essay
from jobpilot.forms.mapping import apply_suggestions, map_fields, suggest_keys
from jobpilot.forms.model import MappingContext
from jobpilot.forms.verify import verify_against_stored, with_bound_files
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.documents_flow import current_artifact_version, stale_follow_up
from jobpilot.orchestrator.engine import (
    AgentContext,
    FollowUp,
    Handler,
    OutputRejected,
    TaskInput,
    TaskKind,
)
from jobpilot.orchestrator.state_machine import transition
from jobpilot.portals.playwright_form import PlaywrightFormAdapter
from jobpilot.portals.registry import ADAPTERS, PortalConfigError, allowed_hosts, apply_mode
from jobpilot.portals.registry import form_url as registry_form_url
from jobpilot.util.hashing import canonical_json

PROMPT_VERSION = "form_prep_v1"
_ACTOR = "orchestrator"
USER_SOURCE = "user"
_A = ApplicationStatus
PREPARABLE = (_A.FORM_IN_PROGRESS.value, _A.NEEDS_USER_INPUT.value)
_USER_GATED = (FieldDecision.ASK_USER.value, FieldDecision.USER_ONLY.value,
               FieldDecision.BLOCKED.value)
_LAYOUT = ErrorCode.FORM_LAYOUT_CHANGED.value
_FORM_KEY = "_form"
_SHOT_RE = re.compile(r"^form_v(\d+)\.png$")
_CODE_RE = re.compile(r"^[A-Z][A-Z_]*(:[a-z_,]+)?$")

FormOpener = Callable[[str, str, str], AbstractAsyncContextManager[PlaywrightFormAdapter]]


# ---- shared helpers -------------------------------------------------------------------


def make_form_opener(cfg: AppConfig) -> FormOpener:
    """Open ``url`` in the portal's isolated browser profile and yield its adapter."""
    @asynccontextmanager
    async def open_form(source: str, url: str,
                        application_id: str) -> AsyncIterator[PlaywrightFormAdapter]:
        cls = ADAPTERS.get(source)
        if cls is None:
            raise OutputRejected("unsupported_portal")
        upload_root = cfg.storage_path / "artifacts" / application_id
        upload_root.mkdir(parents=True, exist_ok=True)
        async with portal_page(cfg, portal=source,
                               allowed_hosts=allowed_hosts(cls, url)) as session:
            yield cls(session.page, upload_root=upload_root)
    return open_form


def form_url(app: dict[str, Any]) -> str:
    """The form to open; the registry refuses (before any browser opens) manual-path apps."""
    try:
        return registry_form_url(app)
    except PortalConfigError as e:
        raise OutputRejected(e.code) from e


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == []


def is_resolved(row: FormFieldAnswerRow) -> bool:
    """A user-gated field is resolved only by a user answer; declarations need a tick."""
    value = json.loads(row.proposed_value_json)
    if row.decision == FieldDecision.NOT_APPLICABLE.value:
        return True
    if row.decision in _USER_GATED and row.source_fact_id != USER_SOURCE:
        return False
    if row.field_type == "checkbox" and row.source_fact_id == USER_SOURCE:
        return value is True
    return not _empty(value)


def answer_rows(s: Session, application_id: str) -> list[FormFieldAnswerRow]:
    return list(s.execute(select(FormFieldAnswerRow).where(
        FormFieldAnswerRow.application_id == application_id)
        .order_by(FormFieldAnswerRow.field_id)).scalars())


def unresolved_required(s: Session, application_id: str) -> list[str]:
    return [r.field_id for r in answer_rows(s, application_id)
            if r.required and not is_resolved(r)]


def safe_code(message: str) -> str:
    """Error codes only: page-reported text and expected/found values never pass."""
    if _CODE_RE.match(message):
        return message
    if message.startswith("expected "):
        return "VALUE_MISMATCH"
    return "FIELD_ERROR"


# ---- PREPARE_FORM ---------------------------------------------------------------------


class FormPlan(AgentOutput):
    application_id: str
    profile_version: int
    fields: list[FieldAnswer]
    required_ids: list[str] = []
    options: dict[str, list[str]] = {}
    blockers: list[str] = []
    snapshot_hash: str


class PrepareFormAgent:
    name = "form_prep"
    prompt_version = PROMPT_VERSION
    output_model = FormPlan
    tools = ("application_loader", "mapping_context", "active_profile_version",
             "form_opener", "essay_context")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> FormPlan:
        load_app: Callable[[str], dict | None] = ctx.tools["application_loader"]
        mapping_context: Callable[[str], MappingContext] = ctx.tools["mapping_context"]
        active_version: Callable[[], int | None] = ctx.tools["active_profile_version"]
        open_form: FormOpener = ctx.tools["form_opener"]

        app = load_app(inp.entity_id)
        if app is None:
            raise OutputRejected("application_not_found")
        if app["status"] not in PREPARABLE:
            raise OutputRejected("not_form_in_progress")
        version = active_version()
        if version is None:
            raise OutputRejected("no_profile")
        url = form_url(app)
        mctx = mapping_context(inp.entity_id)
        async with open_form(app.get("source") or "", url, inp.entity_id) as adapter:
            await adapter.open_job(url)
            blockers = await adapter.blockers()
            if blockers:
                return FormPlan(task_id=ctx.task_id, created_at=datetime.now(UTC),
                                application_id=inp.entity_id, profile_version=version,
                                fields=[], blockers=blockers, snapshot_hash="")
            snap = await adapter.inspect_form()
            snap_hash = adapter.snapshot_hash(snap)
        if not snap.fields:  # no form found (expired posting, login page, ...): hand off
            return FormPlan(task_id=ctx.task_id, created_at=datetime.now(UTC),
                            application_id=inp.entity_id, profile_version=version,
                            fields=[], blockers=["no_form"], snapshot_hash="")
        answers, unmatched = map_fields(snap, mctx)
        warnings = list(snap.warnings)
        # essay questions are drafted from stories, never mapped to a library key by a model
        unmatched = [f for f in unmatched if classify_essay(
            f.label, f.help_text, field_type=f.field_type, options=f.options) is None]
        if unmatched:
            if ctx.gateway is None:
                warnings.append("no_model_gateway: suggestions skipped")
            else:
                suggestions = await suggest_keys(unmatched, ctx.gateway, mctx.aliases)
                better = {a.field_id: a for a in apply_suggestions(snap, unmatched,
                                                                   suggestions, mctx)}
                answers = [better.get(a.field_id, a) for a in answers]
        answers = await _with_essays(snap.fields, answers, ctx, inp.entity_id)
        return FormPlan(
            task_id=ctx.task_id, created_at=datetime.now(UTC), warnings=warnings,
            application_id=inp.entity_id, profile_version=version, fields=answers,
            required_ids=[f.field_id for f in snap.fields if f.required],
            options={f.field_id: f.options for f in snap.fields if f.options},
            snapshot_hash=snap_hash)


async def _with_essays(fields: list[Any], answers: list[FieldAnswer], ctx: AgentContext,
                       application_id: str) -> list[FieldAnswer]:
    load = ctx.tools.get("essay_context")
    if load is None or not essay_candidates(fields, answers):
        return answers
    return await draft_essay_answers(fields, answers, ctx.gateway, load(application_id))


def _differs(row: FormFieldAnswerRow, a: FieldAnswer) -> bool:
    return (json.loads(row.proposed_value_json), row.decision, row.source_fact_id,
            row.rationale) != (a.proposed_value, a.decision.value, a.source_fact_id,
                               a.rationale)


def _store_plan(s: Session, app_id: str, out: FormPlan) -> None:
    existing = {r.field_id: r for r in answer_rows(s, app_id)}
    required = set(out.required_ids)
    planned = {a.field_id for a in out.fields}
    for fid in sorted(set(existing) - planned):
        mutations.remove_answer(s, app_id, fid, actor=_ACTOR)
    for a in out.fields:
        row = existing.get(a.field_id)
        options = out.options.get(a.field_id, [])
        if row is None:
            mutations.upsert_answer_initial(s, app_id, a, required=a.field_id in required,
                                            options=options, actor=_ACTOR)
            continue
        # layout metadata (not in the approval hash); a label/type change also changes the
        # snapshot hash, so VALIDATE never fills against a layout nobody re-read
        row.label, row.field_type = a.label, a.field_type
        row.required = a.field_id in required
        row.options_json = canonical_json(options)
        if row.source_fact_id == USER_SOURCE:
            continue  # the user's own answer is never overwritten by automation
        if _differs(row, a):
            mutations.set_answer(s, app_id, a.field_id, a.proposed_value, actor=_ACTOR,
                                 source_fact_id=a.source_fact_id, decision=a.decision,
                                 rationale=a.rationale, confidence=a.confidence)
            row.source_fact_id = a.source_fact_id  # may legitimately be None
    s.flush()


def _to_needs_input(s: Session, app: Any, reason: str) -> None:
    if app.status == _A.NEEDS_USER_INPUT.value:
        record_event(s, entity_type="application", entity_id=app.application_id,
                     actor=_ACTOR, action="needs_user_input", outcome="ok",
                     details={"reason": reason})
    else:
        transition(s, app, _A.NEEDS_USER_INPUT, actor=_ACTOR, reason=reason)


def _doc_versions(s: Session, app: Any, version: int | None) -> list[int | None]:
    return [version, current_artifact_version(s, app.resume_artifact_id),
            current_artifact_version(s, app.cover_letter_artifact_id)]


def prepare_form_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
    if not isinstance(out, FormPlan) or out.application_id != task.entity_id:
        raise OutputRejected("form plan does not match task application")
    app = get_application(s, out.application_id)
    if app.status not in PREPARABLE:
        raise OutputRejected("not_form_in_progress")
    job = s.get(JobRow, app.job_id)
    if job is None or apply_mode(job) != "automated":
        raise OutputRejected("manual_apply_mode")
    stale = stale_follow_up(s, task, app.application_id,
                            _doc_versions(s, app, out.profile_version), _ACTOR)
    if stale is not None:
        return stale
    if out.blockers:
        app.form_snapshot_hash = None  # the form must be read again once unblocked
        _to_needs_input(s, app, "manual_handoff:" + ",".join(sorted(out.blockers)))
        return []
    if ApprovalService(s).active_approval_for(app.application_id) is not None:
        raise OutputRejected("approval_active")
    out = out.model_copy(update={"fields": guard_essay_answers(s, out.fields)})
    _store_plan(s, app.application_id, out)
    app.form_snapshot_hash = out.snapshot_hash
    missing = unresolved_required(s, app.application_id)
    if missing:
        _to_needs_input(s, app, "needs_user_input:" + ",".join(missing))
        return []
    if app.status == _A.NEEDS_USER_INPUT.value:
        transition(s, app, _A.FORM_IN_PROGRESS, actor=_ACTOR, reason="form_prepared")
    return [(TaskKind.VALIDATE, "application", app.application_id)]


# ---- VALIDATE -------------------------------------------------------------------------


class ValidationResult(AgentOutput):
    application_id: str
    profile_version: int | None
    ok: bool
    errors: dict[str, str] = {}
    screenshot_path: str | None = None
    filled: list[str] = []
    failed: dict[str, str] = {}


def _next_shot(directory: Path) -> int:
    found = [int(m.group(1)) for p in directory.iterdir() if (m := _SHOT_RE.match(p.name))]
    return max(found, default=0) + 1


class ValidateAgent:
    name = "form_validate"
    prompt_version = PROMPT_VERSION
    output_model = ValidationResult
    tools = ("application_loader", "stored_answers", "active_profile_version",
             "form_opener", "artifact_dir")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> ValidationResult:
        load_app: Callable[[str], dict | None] = ctx.tools["application_loader"]
        stored: Callable[[str], list[FieldAnswer]] = ctx.tools["stored_answers"]
        active_version: Callable[[], int | None] = ctx.tools["active_profile_version"]
        open_form: FormOpener = ctx.tools["form_opener"]
        artifact_dir: Callable[[str], Path] = ctx.tools["artifact_dir"]

        app = load_app(inp.entity_id)
        if app is None:
            raise OutputRejected("application_not_found")
        if app["status"] != _A.FORM_IN_PROGRESS.value:
            raise OutputRejected("not_form_in_progress")
        version = active_version()

        def result(**kw: Any) -> ValidationResult:
            return ValidationResult(task_id=ctx.task_id, created_at=datetime.now(UTC),
                                    application_id=inp.entity_id, profile_version=version,
                                    **kw)

        docs = [app.get("resume_profile_version"), app.get("cover_letter_profile_version")]
        if version is None or any(v != version for v in docs):
            return result(ok=False, errors={"_documents": "DOCUMENTS_STALE"})
        answers = with_bound_files(stored(inp.entity_id), app)
        url = form_url(app)
        out_dir = artifact_dir(inp.entity_id)
        shot = out_dir / f"form_v{_next_shot(out_dir)}.png"
        async with open_form(app.get("source") or "", url, inp.entity_id) as adapter:
            outcome = await verify_against_stored(adapter, url, app.get("form_snapshot_hash"),
                                                  answers)
            await adapter.screenshot(shot)
        if outcome.blockers:
            return result(ok=False, screenshot_path=str(shot),
                          errors={_FORM_KEY: "BLOCKED:" + ",".join(outcome.blockers)})
        if outcome.layout_changed or outcome.report is None:
            return result(ok=False, screenshot_path=str(shot), errors={_FORM_KEY: _LAYOUT})
        report = outcome.report
        errors = {k: safe_code(v) for k, v in outcome.errors.items()}
        return result(ok=not errors, errors=errors, screenshot_path=str(shot),
                      filled=report.filled,
                      failed={k: safe_code(v) for k, v in report.failed.items()})


def make_validate_handler(storage_path: Path) -> Handler:
    """Stores the screenshot, then FORM_IN_PROGRESS -> VALIDATING -> READY_FOR_REVIEW or
    VALIDATION_FAILED (errors carry field ids and codes only)."""
    def validate_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
        if not isinstance(out, ValidationResult) or out.application_id != task.entity_id:
            raise OutputRejected("validation output does not match task application")
        app = get_application(s, out.application_id)
        if app.status != _A.FORM_IN_PROGRESS.value:
            raise OutputRejected("not_form_in_progress")
        stale = stale_follow_up(s, task, app.application_id,
                                _doc_versions(s, app, out.profile_version), _ACTOR)
        if stale is not None:
            return stale
        shot_id = None
        if out.screenshot_path:
            root = (storage_path / "artifacts" / app.application_id).resolve()
            path = Path(out.screenshot_path)
            if not path.resolve().is_relative_to(root) or not path.is_file():
                raise OutputRejected("artifact_path_invalid")
            shot_id = store.write_artifact(s, app.application_id, "form_screenshot", path, [],
                                           root=storage_path,
                                           profile_version=out.profile_version).artifact_id
        errors = {k: safe_code(v) for k, v in out.errors.items()}
        form_error = errors.get(_FORM_KEY, "")
        if form_error == _LAYOUT or form_error.startswith("BLOCKED:"):
            app.form_snapshot_hash = None  # next user action re-reads the form
        if form_error == _LAYOUT:
            transition(s, app, _A.NEEDS_USER_INPUT, actor=_ACTOR, reason="form_layout_changed")
            return []
        if form_error.startswith("BLOCKED:"):
            transition(s, app, _A.NEEDS_USER_INPUT, actor=_ACTOR,
                       reason="manual_handoff:" + form_error.split(":", 1)[1])
            return []
        transition(s, app, _A.VALIDATING, actor=_ACTOR, reason="validation_started")
        filled = set(out.filled)
        rows = answer_rows(s, app.application_id)
        for r in rows:
            if r.required and not is_resolved(r):
                errors.setdefault(r.field_id, "UNRESOLVED")
            elif r.required and r.decision != FieldDecision.NOT_APPLICABLE.value \
                    and r.field_id not in filled:
                errors.setdefault(r.field_id, "REQUIRED_UNFILLED")
        if not out.ok and not errors:
            errors[_FORM_KEY] = "FIELD_ERROR"
        for r in rows:
            r.validation_errors_json = canonical_json([errors[r.field_id]]
                                                      if r.field_id in errors else [])
        details = {"screenshot_artifact_id": shot_id}
        if errors:
            transition(s, app, _A.VALIDATION_FAILED, actor=_ACTOR, reason="validation_failed",
                       details={**details, "errors": errors})
            return []
        transition(s, app, _A.READY_FOR_REVIEW, actor=_ACTOR, reason="validated",
                   details=details)
        app.content_hash = ApprovalService(s).application_hash(app.application_id)
        return []
    return validate_handler
