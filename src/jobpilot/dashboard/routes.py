"""Server-rendered dashboard. Reads via repositories; writes via ApprovalService,
profile.importer and data.mutations."""

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.api.actions import (
    ActionConflict,
    approve_and_queue,
    select_job,
    start_discovery,
)
from jobpilot.contracts.enums import JobStatus
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
    CandidateFactRow,
    CandidateProfileRow,
    JobRow,
    MatchAssessmentRow,
    OutreachDraftRow,
)
from jobpilot.orchestrator.approval import ApprovalService, StaleApprovalError
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.profile.importer import PROFILE_ID, active_profile, verify_fact

FIELD_ALIASES_PATH = Path(__file__).resolve().parents[3] / "config" / "field_aliases.yaml"
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
    return templates.TemplateResponse(request, name,
                                      {**ctx, "profile_banner": _profile_banner(s)})


def _library_fields() -> list[dict[str, str]]:
    """Answer-library keys from field_aliases.yaml with their decision."""
    data = yaml.safe_load(FIELD_ALIASES_PATH.read_text(encoding="utf-8")) or {}
    out: list[dict[str, str]] = []
    for name, spec in (data.get("fields") or {}).items():
        profile = spec.get("profile")
        if isinstance(profile, str) and profile.startswith("answer_library."):
            out.append({"name": name, "key": profile.split(".", 1)[1],
                        "decision": spec.get("decision", "FILL_AND_REVIEW")})
    return out


def _counts(s: Session) -> dict[str, Any]:
    return {
        "sections": [
            ("Applications", counts_by_status(s, ApplicationRow)),
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
         show_duplicates: int = 0) -> HTMLResponse:
    version = _active_version(s)
    selected = {a.job_id for a in s.execute(select(ApplicationRow)).scalars()}
    rows = []
    for job, _score in jobs_with_scores(s, status or None):
        if job.status == JobStatus.DUPLICATE.value and not show_duplicates \
                and status != JobStatus.DUPLICATE.value:
            continue
        m = _assessment(s, job.job_id, version)
        rows.append({"job": job, "score": None if m is None else m.total_score,
                     "explanation": _one_line(str(_evidence(m).get("explanation") or "")),
                     "selectable": job.status == JobStatus.MATCHED.value
                     and job.job_id not in selected})
    return _page(request, s, "jobs.html", {
        "rows": rows, "status": status or "", "show_duplicates": bool(show_duplicates),
        "statuses": [st.value for st in JobStatus]})


@router.post("/ui/runs/discovery", response_class=HTMLResponse)
def run_discovery(request: Request, s: Db, orch: Orch) -> HTMLResponse:
    try:
        start_discovery(s, orch)
    except ActionConflict as e:
        return _message(request, str(e), 409)
    return _message(request, "Discovery started", 200, ok=True)


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
    has_app = s.execute(select(ApplicationRow.application_id)
                        .where(ApplicationRow.job_id == job_id)).first() is not None
    return _page(request, s, "job_detail.html", {
        "job": job, "m": m, "evidence": facts,
        "requirements": extracted.get("requirements") if isinstance(extracted, dict)
        else None,
        "missing": ev.get("missing_requirements") or [], "concerns": ev.get("concerns") or [],
        "explanation": ev.get("explanation") or "",
        "events": list(reversed(list_events(s, job_id))),
        "selectable": job.status == JobStatus.MATCHED.value and not has_app})


@router.post("/ui/jobs/{job_id}/select", response_class=HTMLResponse)
def select_ui(request: Request, job_id: str, s: Db) -> HTMLResponse:
    try:
        select_job(s, job_id)
    except LookupError as e:
        return _message(request, str(e), 404)
    except ActionConflict as e:
        return _message(request, str(e), 409)
    return _message(request, "Selected — application created.", 200, ok=True)


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


@router.get("/ui/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: str, s: Db) -> HTMLResponse:
    batch = latest_batch(s, batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    rows = []
    for item in batch_items(s, batch.batch_id, batch.version):
        app = s.get(ApplicationRow, item.application_id)
        job = s.get(JobRow, app.job_id) if app is not None else None
        rows.append({"item": item, "app": app, "job": job})
    return _page(request, s, "batch.html", {"batch": batch, "rows": rows})


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
        res = approve_and_queue(s, orch, batch_id, version, content_hash, ids)
    except StaleApprovalError:
        return _message(request, STALE_MESSAGE, 409)
    except ValueError as e:
        return _message(request, f"Could not approve: {e}", 422)
    n = len(res.approved_ids)
    return _message(request, f"Approved {n} application{'s' if n != 1 else ''}; "
                             "simulated submission queued.", 200, ok=True)


def _profile_context(s: Session) -> dict[str, Any]:
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
               "editable": f["decision"] in EDITABLE_DECISIONS} for f in _library_fields()]
    prof = s.get(CandidateProfileRow, (PROFILE_ID, version))
    prefs = json.loads(prof.preferences_json) if prof is not None else {}
    return {"version": version, "sections": sections, "fields": fields,
            "open_questions": prefs.get("open_questions", [])}


@router.get("/ui/profile", response_class=HTMLResponse)
def profile(request: Request, s: Db) -> HTMLResponse:
    return _page(request, s, "profile.html", _profile_context(s))


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
    for f in _library_fields():
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
