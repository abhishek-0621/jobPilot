"""Server-rendered dashboard. Reads via repositories; writes only via ApprovalService."""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from jobpilot.api.actions import approve_and_queue
from jobpilot.contracts.models import is_http_url
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
from jobpilot.data.tables import ApplicationRow, ApprovalBatchRow, JobRow, OutreachDraftRow
from jobpilot.orchestrator.approval import ApprovalService, StaleApprovalError
from jobpilot.orchestrator.engine import Orchestrator

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
    return templates.TemplateResponse(request, "overview.html", {
        **_counts(s), "failures": recent_failures(s),
        "ready": ready_unbatched_applications(s),
    })


@router.get("/ui/overview/counts", response_class=HTMLResponse)
def overview_counts(request: Request, s: Db) -> HTMLResponse:
    return templates.TemplateResponse(request, "_counts.html", _counts(s))


@router.get("/ui/jobs", response_class=HTMLResponse)
def jobs(request: Request, s: Db) -> HTMLResponse:
    return templates.TemplateResponse(request, "jobs.html", {"jobs": jobs_with_scores(s)})


@router.get("/ui/events", response_class=HTMLResponse)
def events(request: Request, s: Db) -> HTMLResponse:
    return templates.TemplateResponse(request, "events.html", {"events": list_events(s)})


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
    return templates.TemplateResponse(request, "batch.html", {"batch": batch, "rows": rows})


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
