"""Server-rendered dashboard. Reads via repositories; writes via ApprovalService,
profile.importer and data.mutations."""

import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.agents.form_prep import answer_rows, unresolved_required
from jobpilot.api.actions import (
    ANSWERABLE_STATUSES,
    MANUAL_STATUSES,
    ActionConflict,
    ConfirmationRequired,
    add_manual_job,
    approve_batch,
    mark_applied,
    not_applying,
    prepare_again,
    regenerate_documents,
    request_submit,
    reread_form,
    resolve_uncertain,
    save_manual_answers,
    save_user_answers,
    select_job,
    start_discovery,
    start_tailor,
    submit_candidates,
    user_editable,
)
from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ApplicationStatus, BatchStatus, FieldDecision, JobStatus
from jobpilot.contracts.models import is_http_url
from jobpilot.data.mutations import set_library_answer
from jobpilot.data.repositories import (
    batch_items,
    counts_by_status,
    jobs_with_scores,
    latest_batch,
    list_events,
    pending_batches,
    ready_unbatched_applications,
    recent_failures,
)
from jobpilot.data.tables import (
    AnswerLibraryRow,
    ApplicationRow,
    ApprovalBatchRow,
    AuditEventRow,
    BatchItemRow,
    CandidateFactRow,
    CandidateProfileRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    MatchAssessmentRow,
    OutreachDraftRow,
)
from jobpilot.discovery.board_finder import find_hits
from jobpilot.forms.classify import classify_sensitive
from jobpilot.forms.mapping import MODEL_SUGGESTED_RATIONALE
from jobpilot.observability.events import SUBMIT_CLICKED, last_event, prior_unresolved_click
from jobpilot.orchestrator.approval import ApprovalService, StaleApprovalError
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.orchestrator.state_machine import IllegalTransition
from jobpilot.profile.importer import PROFILE_ID, active_profile, verify_fact
from jobpilot.profile.preferences import (
    CompaniesFileError,
    append_companies,
    load_companies,
)

MANUAL_APPLIED_LABEL = "Applied (manual)"
EDITABLE_DECISIONS = ("FILL_AND_REVIEW", "ASK_USER")
NO_PROFILE_BANNER = "Import your profile: uv run jobpilot profile import"
UNVERIFIED_BANNER = "Verify your profile before matching — unverified facts are never used."
STALE_MESSAGE = "This batch changed since you viewed it — reload to review the latest version."

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _local(ts: datetime) -> str:
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _utc(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_href(url: str | None) -> str:
    return url if url and is_http_url(url) else "#"


templates.env.filters["safe_href"] = _safe_href
templates.env.filters["local"] = _local
templates.env.filters["utc"] = _utc


def _session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as s:
        yield s


def _orchestrator(request: Request) -> Orchestrator:
    orch: Orchestrator = request.app.state.orchestrator
    return orch


Db = Annotated[Session, Depends(_session)]
Orch = Annotated[Orchestrator, Depends(_orchestrator)]


def _profile_banner(s: Session) -> str | None:
    try:
        _, facts = active_profile(s)
    except LookupError:
        return NO_PROFILE_BANNER
    return None if any(f.verified_by_user for f in facts) else UNVERIFIED_BANNER


def _page(request: Request, s: Session, name: str, ctx: dict[str, Any]) -> HTMLResponse:
    cfg: AppConfig = request.app.state.cfg
    return templates.TemplateResponse(
        request, name, {**ctx, "profile_banner": _profile_banner(s),
                        "live_portals": cfg.effective_live_portals,
                        "uncertain_count": _uncertain_count(s)})


def _uncertain_count(s: Session) -> int:
    return s.execute(select(func.count()).select_from(ApplicationRow).where(
        ApplicationRow.status == ApplicationStatus.SUBMISSION_UNCERTAIN.value)).scalar_one()


def _last_event(s: Session, application_id: str, action: str) -> AuditEventRow | None:
    return last_event(s, "application", application_id, action)


def _submission(s: Session, app: ApplicationRow) -> dict[str, Any]:
    """Evidence of the latest submit attempt: screenshot, error codes, time of click."""
    finished = _last_event(s, app.application_id, "submit_finished")
    try:
        details = json.loads(finished.redacted_details_json) if finished else {}
    except ValueError:
        details = {}
    codes = details.get("error_codes") if isinstance(details, dict) else None
    clicked = _last_event(s, app.application_id, SUBMIT_CLICKED)
    return {
        "shot": _latest_artifact(s, app.application_id, "submit_confirmation"),
        "error_codes": [str(c) for c in codes] if isinstance(codes, list) else [],
        "error_code": finished.error_code if finished else None,
        "clicked_at": clicked.timestamp_utc if clicked else None,
        "finished_at": finished.timestamp_utc if finished else None,
        "requested_at": app.submit_requested_at,
    }


def _library_fields(aliases_path: Path) -> list[dict[str, str]]:
    """Answer-library keys from field_aliases.yaml with their decision."""
    data = yaml.safe_load(aliases_path.read_text(encoding="utf-8")) or {}
    out: list[dict[str, str]] = []
    for name, spec in (data.get("fields") or {}).items():
        profile = spec.get("profile")
        if isinstance(profile, str) and profile.startswith("answer_library."):
            out.append({"name": name, "key": profile.split(".", 1)[1],
                        "decision": spec.get("decision", "FILL_AND_REVIEW")})
    return out


def _counts(s: Session) -> dict[str, Any]:
    apps = counts_by_status(s, ApplicationRow)
    auto = apps.get(ApplicationStatus.CONFIRMED.value, 0)
    manual = apps.get(ApplicationStatus.SUBMITTED_BY_USER.value, 0)
    return {
        "applied": {"total": auto + manual, "automated": auto, "manual": manual},
        "sections": [
            ("Applications", apps),
            ("Jobs", counts_by_status(s, JobRow)),
            ("Batches", counts_by_status(s, ApprovalBatchRow)),
            ("Outreach", counts_by_status(s, OutreachDraftRow)),
        ],
        "pending": pending_batches(s),
    }


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, s: Db) -> HTMLResponse:
    return _page(request, s, "overview.html", {
        **_counts(s), "failures": recent_failures(s),
        "ready": ready_unbatched_applications(s),
    })


@router.get("/ui/overview/counts", response_class=HTMLResponse)
def overview_counts(request: Request, s: Db) -> HTMLResponse:
    return templates.TemplateResponse(request, "_counts.html", _counts(s))


def _active_version(s: Session) -> int | None:
    try:
        return active_profile(s)[0]
    except LookupError:
        return None


def _assessment(s: Session, job_id: str, version: int | None) -> MatchAssessmentRow | None:
    """The assessment for the active profile version, else the latest one."""
    if version is not None:
        row = s.get(MatchAssessmentRow, (job_id, version))
        if row is not None:
            return row
    return s.execute(select(MatchAssessmentRow).where(MatchAssessmentRow.job_id == job_id)
                     .order_by(MatchAssessmentRow.profile_version.desc())).scalars().first()


def _evidence(m: MatchAssessmentRow | None) -> dict[str, Any]:
    if m is None:
        return {}
    try:
        data = json.loads(m.evidence_json or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _one_line(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


@router.get("/ui/jobs", response_class=HTMLResponse)
def jobs(request: Request, s: Db, status: str | None = None,
         show_duplicates: int = 0, deleted: str | None = None) -> HTMLResponse:
    version = _active_version(s)
    selected = {a.job_id: a.application_id
                for a in s.execute(select(ApplicationRow)).scalars()}
    app_status = {a.job_id: a.status for a in s.execute(select(ApplicationRow)).scalars()}
    rows = []
    for job, _score in jobs_with_scores(s, status or None):
        if job.status == JobStatus.DUPLICATE.value and not show_duplicates \
                and status != JobStatus.DUPLICATE.value:
            continue
        m = _assessment(s, job.job_id, version)
        rows.append({"job": job, "label": MANUAL_APPLIED_LABEL if app_status.get(
                         job.job_id) == ApplicationStatus.SUBMITTED_BY_USER.value
                     else job.status, "score": None if m is None else m.total_score,
                     "application_id": selected.get(job.job_id),
                     "explanation": _one_line(str(_evidence(m).get("explanation") or "")),
                     "selectable": job.status == JobStatus.MATCHED.value
                     and job.job_id not in selected})
    return _page(request, s, "jobs.html", {
        "rows": rows, "status": status or "", "show_duplicates": bool(show_duplicates),
        "flash": f"Deleted the data of application {deleted}. Its audit history is kept."
        if deleted else None,
        "statuses": [st.value for st in JobStatus]})


@router.post("/ui/runs/discovery", response_class=HTMLResponse)
def run_discovery(request: Request, s: Db, orch: Orch) -> HTMLResponse:
    try:
        start_discovery(s, orch)
    except ActionConflict as e:
        return _message(request, str(e), 409)
    return _message(request, "Discovery started", 200, ok=True)


@router.get("/ui/jobs/new", response_class=HTMLResponse)
def new_job_form(request: Request, s: Db) -> HTMLResponse:
    return _page(request, s, "job_new.html", {"error": None, "form": {}})


@router.post("/ui/jobs/new", response_model=None)
def new_job(
    request: Request, s: Db, orch: Orch,
    url: Annotated[str, Form()] = "", title: Annotated[str, Form()] = "",
    company: Annotated[str, Form()] = "", location: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """Add a job found anywhere (LinkedIn, Naukri, ...). JobPilot never opens such sites;
    the application is prepared as a manual package."""
    try:
        job_id = add_manual_job(s, orch, url=url, title=title, company=company,
                                location=location, description=description)
    except ValueError as e:
        s.rollback()
        resp = _page(request, s, "job_new.html", {"error": str(e), "form": {
            "url": url, "title": title, "company": company, "location": location,
            "description": description}})
        resp.status_code = 422
        return resp
    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=303)


@router.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str, s: Db) -> HTMLResponse:
    job = s.get(JobRow, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    m = _assessment(s, job_id, _active_version(s))
    ev = _evidence(m)
    facts = []
    for fid in ev.get("matched_evidence_fact_ids") or []:
        f = s.get(CandidateFactRow, fid)
        facts.append({"fact_id": fid, "value": f.value if f is not None else "(missing)"})
    try:
        extracted = json.loads(job.extracted_json or "{}")
    except ValueError:
        extracted = {}
    app_id = s.execute(select(ApplicationRow.application_id)
                       .where(ApplicationRow.job_id == job_id)).scalar()
    return _page(request, s, "job_detail.html", {
        "job": job, "m": m, "evidence": facts,
        "requirements": extracted.get("requirements") if isinstance(extracted, dict)
        else None,
        "missing": ev.get("missing_requirements") or [], "concerns": ev.get("concerns") or [],
        "explanation": ev.get("explanation") or "",
        "events": list(reversed(list_events(s, job_id))),
        "application_id": app_id,
        "selectable": job.status == JobStatus.MATCHED.value and app_id is None})


@router.post("/ui/jobs/{job_id}/select", response_class=HTMLResponse)
def select_ui(request: Request, job_id: str, s: Db, orch: Orch) -> HTMLResponse:
    try:
        select_job(s, orch, job_id)
    except LookupError as e:
        return _message(request, str(e), 404)
    except ActionConflict as e:
        return _message(request, str(e), 409)
    return _message(request, "Selected — application created.", 200, ok=True)


@router.post("/ui/applications/{application_id}/tailor", response_class=HTMLResponse)
def tailor_ui(request: Request, application_id: str, s: Db, orch: Orch) -> HTMLResponse:
    try:
        start_tailor(s, orch, application_id)
    except LookupError as e:
        return _message(request, str(e), 404)
    except ActionConflict as e:
        return _message(request, str(e), 409)
    return _message(request, "Tailoring queued.", 200, ok=True)


def _latest_artifact(s: Session, application_id: str, type_: str) -> DocumentArtifactRow | None:
    return s.execute(select(DocumentArtifactRow).where(
        DocumentArtifactRow.application_id == application_id, DocumentArtifactRow.type == type_)
        .order_by(DocumentArtifactRow.version.desc())).scalars().first()


def _artifact_text(request: Request, row: DocumentArtifactRow | None) -> str | None:
    """File contents of an artifact, only if it resolves inside storage/artifacts."""
    if row is None:
        return None
    storage: Path = request.app.state.storage_path
    path = (storage / row.path).resolve()
    if not path.is_relative_to((storage / "artifacts").resolve()) or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _diff_rows(raw: str | None) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


@router.get("/ui/applications/{application_id}", response_class=HTMLResponse)
def application_page(request: Request, application_id: str, s: Db) -> HTMLResponse:
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise HTTPException(404, "application not found")
    job = s.get(JobRow, app.job_id)
    letter_txt = _latest_artifact(s, application_id, "cover_letter_text")
    try:
        facts = {f.fact_id: f.value for f in active_profile(s)[1]}
    except LookupError:
        facts = {}
    try:
        cited = json.loads(letter_txt.source_fact_ids_json) if letter_txt else []
    except ValueError:
        cited = []
    evidence = [{"fact_id": fid, "value": facts.get(fid, "(removed)")}
                for fid in (cited if isinstance(cited, list) else []) if isinstance(fid, str)]
    return _page(request, s, "application.html", {
        "app": app, "job": job,
        "resume": _latest_artifact(s, application_id, "resume"),
        "docx": _latest_artifact(s, application_id, "resume_docx"),
        "cover": _latest_artifact(s, application_id, "cover_letter"),
        "diff": _diff_rows(_artifact_text(request, _latest_artifact(
            s, application_id, "resume_diff"))),
        "facts": facts, "letter": _artifact_text(request, letter_txt), "evidence": evidence,
        "submission": _submission(s, app),
        "events": list(reversed(list_events(s, application_id)))})


@router.get("/ui/applications/{application_id}/export")
def export_application_ui(request: Request, application_id: str, s: Db) -> FileResponse:
    """Build the bundle in a temp dir and stream it; nothing is left in storage/exports."""
    import shutil
    import tempfile

    from starlette.background import BackgroundTask

    from jobpilot.ops.export import export_application

    if s.get(ApplicationRow, application_id) is None:
        raise HTTPException(404, "application not found")
    cfg: AppConfig = request.app.state.cfg
    tmp = Path(tempfile.mkdtemp(prefix="jobpilot-export-"))
    try:
        tmp.chmod(0o700)
        path = export_application(cfg, s, application_id, out_dir=tmp)
    except LookupError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(404, str(e)) from e
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return FileResponse(path, media_type="application/zip", filename=path.name,
                        content_disposition_type="attachment",
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


@router.post("/ui/applications/{application_id}/delete", response_class=HTMLResponse,
             response_model=None)
def delete_application_ui(request: Request, application_id: str, s: Db,
                          confirm: Annotated[str, Form()] = ""
                          ) -> HTMLResponse | RedirectResponse:
    from urllib.parse import quote

    from jobpilot.ops.lock import LockHeld
    from jobpilot.ops.purge import ConfirmationMismatch, DeleteRefused, delete_application

    if s.get(ApplicationRow, application_id) is None:
        raise HTTPException(404, "application not found")
    try:
        delete_application(request.app.state.cfg, s, application_id, confirm=confirm.strip())
    except ConfirmationMismatch as e:
        return _message(request, str(e), 422)
    except (DeleteRefused, LockHeld) as e:
        return _message(request, str(e), 409)
    return RedirectResponse(f"/ui/jobs?deleted={quote(application_id)}", status_code=303)


def _display(value: Any) -> str:
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


@router.get("/ui/applications/{application_id}/answers", response_class=HTMLResponse)
def answers_page(request: Request, application_id: str, s: Db) -> HTMLResponse:
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise HTTPException(404, "application not found")
    rows = []
    for r in answer_rows(s, application_id):
        value = json.loads(r.proposed_value_json)
        rows.append({
            "field_id": r.field_id, "label": r.label, "field_type": r.field_type,
            "decision": r.decision, "value": value, "display": _display(value),
            "source": r.source_fact_id, "rationale": r.rationale, "required": r.required,
            "options": json.loads(r.options_json),
            "editable": user_editable(r.decision, r.field_type),
            "declaration": r.field_type == "checkbox"
            and classify_sensitive(r.label, "", []) == "declaration"})
    return _page(request, s, "answers.html", {
        "app": app, "job": s.get(JobRow, app.job_id), "rows": rows,
        "missing": unresolved_required(s, application_id),
        "editable_status": app.status in ANSWERABLE_STATUSES})


@router.post("/ui/applications/{application_id}/answers")
async def save_answers_ui(request: Request, application_id: str, s: Db,
                          orch: Orch) -> RedirectResponse:
    form = await request.form()
    values: dict[str, Any] = {}
    for r in answer_rows(s, application_id):
        if not user_editable(r.decision, r.field_type):
            continue
        name, old = f"v:{r.field_id}", json.loads(r.proposed_value_json)
        if r.field_type == "checkbox":
            if form.get(f"seen:{r.field_id}") is None:
                continue
            ticked = form.get(name) == "on"
            if ticked != (old is True) and (ticked or old is not None):
                values[r.field_id] = ticked
        elif r.field_type == "checkbox_group":
            picked = [v for v in form.getlist(name) if isinstance(v, str)]
            if picked and picked != old:
                values[r.field_id] = picked
        else:
            raw = form.get(name)
            text = raw.strip() if isinstance(raw, str) else ""
            if text and text != old:
                values[r.field_id] = text
    try:
        save_user_answers(s, orch, application_id, values)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ActionConflict as e:
        raise HTTPException(409, str(e)) from e
    return RedirectResponse(f"/ui/applications/{application_id}/answers", status_code=303)


def _text_value(raw_json: str) -> str:
    value = json.loads(raw_json)
    return "" if value is None or value == "" else _display(value)


def _manual_app(s: Session, application_id: str) -> ApplicationRow:
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise HTTPException(404, "application not found")
    return app


@router.get("/ui/applications/{application_id}/apply", response_class=HTMLResponse)
def apply_page(request: Request, application_id: str, s: Db) -> HTMLResponse:
    app = _manual_app(s, application_id)
    if app.status not in MANUAL_STATUSES:
        raise HTTPException(409, f"No manual apply page for status {app.status}")
    rows = []
    for r in answer_rows(s, application_id):
        rows.append({"field_id": r.field_id, "label": r.label, "decision": r.decision,
                     "value": _text_value(r.proposed_value_json),
                     "editable": user_editable(r.decision, r.field_type)})
    return _page(request, s, "apply.html", {
        "app": app, "job": s.get(JobRow, app.job_id), "rows": rows,
        "resume": _latest_artifact(s, application_id, "resume"),
        "docx": _latest_artifact(s, application_id, "resume_docx"),
        "cover": _latest_artifact(s, application_id, "cover_letter"),
        "letter": _artifact_text(request, _latest_artifact(
            s, application_id, "cover_letter_text")),
        "ready": app.status == ApplicationStatus.MANUAL_APPLY_READY.value,
        "today": date.today().isoformat()})


@router.post("/ui/applications/{application_id}/apply/answers")
async def save_manual_answers_ui(request: Request, application_id: str,
                                 s: Db) -> RedirectResponse:
    form = await request.form()
    rows = {r.field_id: r for r in answer_rows(s, application_id)}
    values: dict[str, str] = {}
    for key, raw in form.items():
        if not key.startswith("v:") or not isinstance(raw, str):
            continue
        row = rows.get(key[2:])
        if row is None:  # unknown field: the action refuses it
            values[key[2:]] = raw.strip()
        elif raw.strip() != _text_value(row.proposed_value_json):
            values[row.field_id] = raw.strip()
    try:
        save_manual_answers(s, application_id, values)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ActionConflict as e:
        s.rollback()
        raise HTTPException(409, str(e)) from e
    return RedirectResponse(f"/ui/applications/{application_id}/apply", status_code=303)


@router.post("/ui/applications/{application_id}/mark-applied")
def mark_applied_ui(application_id: str, s: Db, reference: Annotated[str, Form()] = "",
                    applied_on: Annotated[str, Form()] = "") -> RedirectResponse:
    try:
        day = date.fromisoformat(applied_on.strip()) if applied_on.strip() else None
        mark_applied(s, application_id, reference, day)
    except ValueError as e:
        s.rollback()
        raise HTTPException(422, str(e)) from e
    except LookupError as e:
        s.rollback()
        raise HTTPException(404, str(e)) from e
    except (ActionConflict, IllegalTransition) as e:
        s.rollback()
        raise HTTPException(409, str(e)) from e
    return RedirectResponse(f"/ui/applications/{application_id}/apply", status_code=303)


@router.post("/ui/applications/{application_id}/not-applying")
def not_applying_ui(application_id: str, s: Db) -> RedirectResponse:
    try:
        not_applying(s, application_id)
    except LookupError as e:
        s.rollback()
        raise HTTPException(404, str(e)) from e
    except (ActionConflict, IllegalTransition) as e:
        s.rollback()
        raise HTTPException(409, str(e)) from e
    return RedirectResponse(f"/ui/applications/{application_id}", status_code=303)


@router.post("/ui/applications/{application_id}/reread")
def reread_ui(application_id: str, s: Db, orch: Orch) -> RedirectResponse:
    try:
        reread_form(s, orch, application_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ActionConflict as e:
        s.rollback()
        raise HTTPException(409, str(e)) from e
    return RedirectResponse(f"/ui/applications/{application_id}/answers", status_code=303)


@router.post("/ui/applications/{application_id}/regenerate", response_class=HTMLResponse)
def regenerate_ui(request: Request, application_id: str, s: Db) -> HTMLResponse:
    try:
        regenerate_documents(s, application_id)
    except LookupError as e:
        return _message(request, str(e), 404)
    except ActionConflict as e:
        s.rollback()
        return _message(request, str(e), 409)
    return _message(request, "Regeneration queued.", 200, ok=True)


@router.get("/ui/events", response_class=HTMLResponse)
def events(request: Request, s: Db) -> HTMLResponse:
    return _page(request, s, "events.html", {"events": list_events(s)})


@router.post("/ui/batches")
def build_batch(s: Db) -> RedirectResponse:
    ids = [a.application_id for a, _ in ready_unbatched_applications(s)]
    try:
        batch = ApprovalService(s).build_batch(ids)
    except ValueError as e:
        s.rollback()
        raise HTTPException(422, str(e)) from e
    batch_id = batch.batch_id
    s.commit()
    return RedirectResponse(f"/ui/batches/{batch_id}", status_code=303)


CHANGED_STATUSES = (ApplicationStatus.READY_FOR_REVIEW.value, ApplicationStatus.APPROVED.value)


def _is_model_suggested(a: FormFieldAnswerRow) -> bool:
    return a.rationale == MODEL_SUGGESTED_RATIONALE


@router.get("/ui/batches", response_class=HTMLResponse)
def batches_page(request: Request, s: Db) -> HTMLResponse:
    batches = pending_batches(s)
    counts: dict[tuple[str, int], list[int]] = {}
    for item in s.execute(select(BatchItemRow).where(
            BatchItemRow.batch_id.in_([b.batch_id for b in batches]))).scalars():
        c = counts.setdefault((item.batch_id, item.version), [0, 0])
        c[0] += 1
        c[1] += 1 if item.selected else 0
    return _page(request, s, "batches.html", {"rows": [
        {"batch": b, "total": counts.get((b.batch_id, b.version), [0, 0])[0],
         "approved": counts.get((b.batch_id, b.version), [0, 0])[1]} for b in batches]})


@router.get("/ui/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: str, s: Db) -> HTMLResponse:
    batch = latest_batch(s, batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    items = batch_items(s, batch.batch_id, batch.version)
    app_ids = [i.application_id for i in items]
    apps = {a.application_id: a for a in s.execute(
        select(ApplicationRow).where(ApplicationRow.application_id.in_(app_ids))).scalars()}
    jobs_by_id = {j.job_id: j for j in s.execute(select(JobRow).where(
        JobRow.job_id.in_([a.job_id for a in apps.values()]))).scalars()}
    shots: dict[str, DocumentArtifactRow] = {}
    for art in s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.application_id.in_(app_ids),
            DocumentArtifactRow.type == "form_screenshot")
            .order_by(DocumentArtifactRow.version)).scalars():
        shots[art.application_id] = art  # ascending, so the last one is the latest
    answers: dict[str, list[FormFieldAnswerRow]] = {}
    for a in s.execute(select(FormFieldAnswerRow).where(
            FormFieldAnswerRow.application_id.in_(app_ids))
            .order_by(FormFieldAnswerRow.field_id)).scalars():
        answers.setdefault(a.application_id, []).append(a)
    svc = ApprovalService(s)
    cfg: AppConfig = request.app.state.cfg
    live = set(cfg.effective_live_portals)
    submittable = submit_candidates(s, batch.batch_id)
    rows = []
    for item in items:
        app = apps.get(item.application_id)
        job = jobs_by_id.get(app.job_id) if app is not None else None
        mine = answers.get(item.application_id, [])
        summary: dict[str, int] = {}
        for a in mine:
            summary[a.decision] = summary.get(a.decision, 0) + 1
        changed = app is None or app.status not in CHANGED_STATUSES \
            or svc.application_hash(item.application_id) != item.item_hash
        rows.append({
            "item": item, "app": app, "job": job, "changed": changed,
            "live": app is not None and app.portal in live,
            "shot": shots.get(item.application_id), "summary": sorted(summary.items()),
            "answers": [{"label": a.label, "decision": a.decision, "source": a.source_fact_id,
                         "display": _display(json.loads(a.proposed_value_json))} for a in mine],
            "review_count": summary.get(FieldDecision.FILL_AND_REVIEW.value, 0),
            "suggested_count": sum(1 for a in mine if _is_model_suggested(a))})
    pending = batch.status in (BatchStatus.READY_FOR_REVIEW.value,
                               BatchStatus.PARTIALLY_APPROVED.value)
    return _page(request, s, "batch.html", {
        "batch": batch, "rows": rows, "pending": pending,
        "any_changed": any(r["changed"] for r in rows),
        "submittable": [{"app": a, "live": a.portal in live} for a in submittable],
        "submit_live": any(a.portal in live for a in submittable)})


@router.post("/ui/batches/{batch_id}/rebuild")
def rebuild(batch_id: str, s: Db) -> RedirectResponse:
    try:
        batch = ApprovalService(s).rebuild_batch(batch_id, only_ready=True)
    except ValueError as e:
        s.rollback()
        raise HTTPException(422, str(e)) from e
    new_id_ = batch.batch_id
    s.commit()
    return RedirectResponse(f"/ui/batches/{new_id_}", status_code=303)


def _message(request: Request, text: str, status: int, ok: bool = False) -> HTMLResponse:
    return templates.TemplateResponse(request, "_message.html", {"text": text, "ok": ok},
                                      status_code=status)


@router.post("/ui/batches/{batch_id}/approve", response_class=HTMLResponse)
def approve(
    request: Request, batch_id: str, s: Db, orch: Orch,
    content_hash: Annotated[str, Form()],
    version: Annotated[int, Form()],
    ids: Annotated[list[str], Form()],
) -> HTMLResponse:
    try:
        res = approve_batch(s, batch_id, version, content_hash, ids)
    except StaleApprovalError:
        return _message(request, STALE_MESSAGE, 409)
    except ValueError as e:
        return _message(request, f"Could not approve: {e}", 422)
    n = len(res.approved_ids)
    return _message(request, f"Approved {n} application{'s' if n != 1 else ''}. "
                             "Nothing is submitted until you press Submit — reload to "
                             "see the Submit button.", 200, ok=True)


@router.post("/ui/batches/{batch_id}/submit", response_class=HTMLResponse)
def submit_ui(
    request: Request, batch_id: str, s: Db, orch: Orch,
    confirm: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        queued = request_submit(s, orch, batch_id, None, confirm.strip() or None,
                                request.app.state.cfg)
    except LookupError as e:
        return _message(request, str(e), 404)
    except ActionConflict as e:
        return _message(request, str(e), 409)
    except ConfirmationRequired as e:
        return _message(request, str(e), 422)
    n = len(queued)
    return _message(request, f"Submission queued for {n} application"
                             f"{'s' if n != 1 else ''}.", 200, ok=True)


@router.get("/ui/uncertain", response_class=HTMLResponse)
def uncertain_page(request: Request, s: Db) -> HTMLResponse:
    def rows(status: ApplicationStatus) -> list[dict[str, Any]]:
        out = []
        for app in s.execute(select(ApplicationRow).where(
                ApplicationRow.status == status.value)
                .order_by(ApplicationRow.application_id)).scalars():
            out.append({"app": app, "job": s.get(JobRow, app.job_id),
                        "submission": _submission(s, app),
                        "unresolved_click": prior_unresolved_click(s, app.application_id)})
        return out
    return _page(request, s, "uncertain.html", {
        "uncertain": rows(ApplicationStatus.SUBMISSION_UNCERTAIN),
        "failed": rows(ApplicationStatus.SUBMISSION_FAILED)})


@router.post("/ui/applications/{application_id}/resolve")
def resolve_ui(application_id: str, s: Db,
               submitted: Annotated[str, Form()]) -> RedirectResponse:
    if submitted not in ("yes", "no"):
        raise HTTPException(422, "submitted must be yes or no")
    try:
        resolve_uncertain(s, application_id, submitted == "yes")
    except LookupError as e:
        s.rollback()
        raise HTTPException(404, str(e)) from e
    except (ActionConflict, IllegalTransition) as e:
        s.rollback()
        raise HTTPException(409, str(e)) from e
    return RedirectResponse("/ui/uncertain", status_code=303)


@router.post("/ui/applications/{application_id}/prepare-again")
def prepare_again_ui(application_id: str, s: Db, orch: Orch) -> RedirectResponse:
    try:
        prepare_again(s, orch, application_id)
    except LookupError as e:
        s.rollback()
        raise HTTPException(404, str(e)) from e
    except (ActionConflict, IllegalTransition) as e:
        s.rollback()
        raise HTTPException(409, str(e)) from e
    return RedirectResponse(f"/ui/applications/{application_id}", status_code=303)


def _profile_context(s: Session, aliases_path: Path) -> dict[str, Any]:
    try:
        version, facts = active_profile(s)
    except LookupError:
        return {"version": None, "sections": {}, "fields": [], "open_questions": []}
    sections: dict[str, list[CandidateFactRow]] = {}
    for f in facts:
        sections.setdefault(f.category, []).append(f)
    answers = {a.question_key: json.loads(a.value_json) for a in s.execute(
        select(AnswerLibraryRow).where(AnswerLibraryRow.profile_version == version)).scalars()}
    fields = [{**f, "value": answers.get(f["key"]),
               "editable": f["decision"] in EDITABLE_DECISIONS}
              for f in _library_fields(aliases_path)]
    prof = s.get(CandidateProfileRow, (PROFILE_ID, version))
    prefs = json.loads(prof.preferences_json) if prof is not None else {}
    return {"version": version, "sections": sections, "fields": fields,
            "open_questions": prefs.get("open_questions", [])}


@router.get("/ui/profile", response_class=HTMLResponse)
def profile(request: Request, s: Db) -> HTMLResponse:
    ctx = _profile_context(s, request.app.state.field_aliases_path)
    return _page(request, s, "profile.html", ctx)


@router.post("/ui/profile/facts/{fact_id}/verify", response_class=HTMLResponse)
def verify_one(request: Request, fact_id: str, s: Db,
               verified: Annotated[str, Form()] = "") -> HTMLResponse:
    try:
        verify_fact(s, fact_id, verified == "true", actor="user")
    except LookupError as e:
        s.rollback()
        raise HTTPException(404, str(e)) from e
    s.commit()
    return _message(request, "Verified." if verified == "true" else "Marked unverified.",
                    200, ok=True)


@router.post("/ui/profile/sections/{category}/verify")
def verify_section(category: str, s: Db) -> RedirectResponse:
    try:
        _, facts = active_profile(s)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    for f in facts:
        if f.category == category and not f.verified_by_user:
            verify_fact(s, f.fact_id, True, actor="user")
    s.commit()
    return RedirectResponse("/ui/profile", status_code=303)


@router.post("/ui/profile/answers")
async def save_answers(request: Request, s: Db) -> RedirectResponse:
    form = await request.form()
    try:
        active_profile(s)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    for f in _library_fields(request.app.state.field_aliases_path):
        if f["decision"] not in EDITABLE_DECISIONS:
            continue
        key = f["key"]
        if form.get(f"clear_{key}"):
            set_library_answer(s, key, None, actor="user")
            continue
        raw = form.get(key)
        if isinstance(raw, str) and raw.strip():
            set_library_answer(s, key, raw.strip(), actor="user")
    s.commit()
    return RedirectResponse("/ui/profile", status_code=303)


def _companies_page(request: Request, s: Session, ctx: dict[str, Any],
                    status: int = 200) -> HTMLResponse:
    cfg: AppConfig = request.app.state.cfg
    try:
        boards = load_companies(cfg)
    except (AttributeError, TypeError, ValueError, yaml.YAMLError):
        boards = []  # unusable companies.yaml: the add path reports it; keep the page up
    resp = _page(request, s, "companies.html", {
        "boards": boards, "error": None, "results": None, "added": 0, **ctx})
    resp.status_code = status
    return resp


@router.get("/ui/companies", response_class=HTMLResponse)
def companies(request: Request, s: Db) -> HTMLResponse:
    return _companies_page(request, s, {})


@router.post("/ui/companies", response_class=HTMLResponse)
async def add_company(request: Request, s: Db,
                      name: Annotated[str, Form()] = "") -> HTMLResponse:
    """Probe the public boards for a company name and append the live ones to
    storage/profiles/companies.yaml. Only public, login-free endpoints are contacted."""
    name = name.strip()
    if not name or len(name) > 100:
        return _companies_page(request, s, {"error": "Enter a company name (up to 100 characters)"},
                               422)
    async with httpx.AsyncClient() as client:
        hits = await find_hits([name], client)
    cfg: AppConfig = request.app.state.cfg
    try:
        added = append_companies(cfg, [h.board for h in hits])
    except CompaniesFileError as e:
        return _companies_page(request, s, {"error": str(e)}, 422)
    return _companies_page(request, s, {"results": hits, "added": len(added), "searched": name})
