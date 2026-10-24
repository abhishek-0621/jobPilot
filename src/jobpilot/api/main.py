"""Localhost FastAPI app: read models, batch approval, controls and health."""

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Literal

from alembic.runtime.migration import MigrationContext
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

from jobpilot.api import schemas as sc
from jobpilot.api.actions import (
    ActionConflict,
    approve_and_queue,
    select_job,
    start_discovery,
    start_tailor,
)
from jobpilot.config import AppConfig, load_config
from jobpilot.dashboard.routes import router as dashboard_router
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import (
    batch_items,
    counts_by_status,
    get_application,
    jobs_with_scores,
    latest_batch,
    list_events,
    pending_batches,
    recent_failures,
)
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalBatchRow,
    AuditEventRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    MatchAssessmentRow,
    OutreachDraftRow,
)
from jobpilot.llm.ollama_client import ModelGateway
from jobpilot.orchestrator.approval import ApprovalService, StaleApprovalError
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.runtime import Runtime, build_runtime

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
ARTIFACT_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".json": "application/json",
    ".txt": "text/plain",
    ".png": "image/png",
}


class LocalOriginMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests from other sites (CSRF / DNS-rebinding guard)."""

    def __init__(self, app: ASGIApp, port: int) -> None:
        super().__init__(app)
        self.allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if (origin is not None and origin not in self.allowed) or \
                    request.headers.get("sec-fetch-site") == "cross-site":
                return PlainTextResponse("cross-origin request rejected", status_code=403)
        return await call_next(request)


async def worker_loop(orch: Orchestrator, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            did_work = await orch.run_once()
        except Exception:  # the worker must never die; log and back off
            log.exception("worker loop iteration failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            continue
        if not did_work:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)


def _event_out(e: AuditEventRow) -> sc.EventOut:
    return sc.EventOut(
        event_id=e.event_id, timestamp_utc=e.timestamp_utc, entity_type=e.entity_type,
        entity_id=e.entity_id, actor=e.actor, action=e.action, outcome=e.outcome,
        redacted_details=json.loads(e.redacted_details_json), error_code=e.error_code,
    )


def _batch_out(s: Session, batch: ApprovalBatchRow) -> sc.BatchOut:
    items = batch_items(s, batch.batch_id, batch.version)
    return sc.BatchOut(
        batch_id=batch.batch_id, version=batch.version, status=batch.status,
        content_hash=batch.content_hash,
        items=[sc.BatchItemOut(application_id=i.application_id, item_hash=i.item_hash,
                               selected=i.selected) for i in items],
    )


def _job_summary(job: JobRow, score: float | None) -> dict[str, Any]:
    return dict(job_id=job.job_id, source=job.source, company=job.company, title=job.title,
                location=job.location, status=job.status, canonical_url=job.canonical_url,
                total_score=score)


def compute_health(cfg: AppConfig, session_factory: sessionmaker[Session],
            available: set[str] | None) -> sc.HealthOut:
    wanted: set[str] = set()
    for route in cfg.model_routes.values():
        wanted.add(route.model_tag)
        if route.fallback:
            wanted.add(route.fallback)
    missing = sorted(wanted - available) if available is not None else sorted(wanted)
    try:
        with session_factory() as s:
            ctx = MigrationContext.configure(s.connection())
            revision = ctx.get_current_revision() or ""
    except Exception:
        revision = ""
    try:
        target = cfg.storage_path if cfg.storage_path.exists() else cfg.storage_path.parent
        free = round(shutil.disk_usage(target).free / 1024**3, 2)
    except Exception:
        free = 0.0
    return sc.HealthOut(ollama=available is not None, missing_models=missing,
                        db_revision=revision, disk_free_gb=free, dry_run=cfg.dry_run)


def create_app(
    cfg: AppConfig,
    session_factory: sessionmaker[Session],
    orchestrator: Orchestrator,
    gateway: ModelGateway | None = None,
    start_worker: bool = True,
    runtime: Runtime | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        if start_worker:
            orchestrator.recover_on_startup()
            task = asyncio.create_task(worker_loop(orchestrator, stop))
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            if runtime is not None:
                await runtime.aclose()

    app = FastAPI(title="JobPilot", lifespan=lifespan)
    app.add_middleware(LocalOriginMiddleware, port=cfg.port)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.state.session_factory = session_factory
    app.state.orchestrator = orchestrator
    app.state.storage_path = cfg.storage_path
    app.state.field_aliases_path = cfg.field_aliases_path
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(dashboard_router)

    def get_session() -> Iterator[Session]:
        with session_factory() as s:
            yield s

    Db = Annotated[Session, Depends(get_session)]  # noqa: N806

    @app.get("/health", response_model=sc.HealthOut)
    async def health() -> sc.HealthOut:
        available: set[str] | None = None
        if gateway is not None:
            try:
                available = await gateway.available_models()
            except Exception:
                available = None
        return await asyncio.to_thread(compute_health, cfg, session_factory, available)

    @app.get("/jobs", response_model=list[sc.JobSummary])
    def jobs(s: Db, status: str | None = None,
             min_score: float | None = None) -> list[sc.JobSummary]:
        return [sc.JobSummary(**_job_summary(job, score))
                for job, score in jobs_with_scores(s, status, min_score)]

    @app.get("/jobs/{job_id}", response_model=sc.JobDetail)
    def job_detail(job_id: str, s: Db) -> sc.JobDetail:
        job = s.get(JobRow, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        m = s.execute(select(MatchAssessmentRow).where(MatchAssessmentRow.job_id == job_id)
                      .order_by(MatchAssessmentRow.profile_version.desc())).scalars().first()
        match = None if m is None else sc.MatchOut(
            total_score=m.total_score, skill_fit=m.skill_fit, experience_fit=m.experience_fit,
            project_fit=m.project_fit, role_seniority_fit=m.role_seniority_fit,
            location_fit=m.location_fit, hard_filter_passed=m.hard_filter_passed,
            confidence=m.confidence)
        return sc.JobDetail(
            **_job_summary(job, None if m is None else m.total_score),
            application_url=job.application_url, work_mode=job.work_mode,
            description_text=job.description_text, match=match,
            events=[_event_out(e) for e in list_events(s, job_id)],
        )

    @app.post("/runs/discovery")
    def run_discovery(s: Db) -> dict[str, str]:
        try:
            task_id, _run_id = start_discovery(s, orchestrator)
        except ActionConflict as e:
            raise HTTPException(409, str(e)) from e
        return {"task_id": task_id}

    @app.post("/jobs/{job_id}/select")
    def select_api(job_id: str, s: Db) -> dict[str, str]:
        try:
            a = select_job(s, orchestrator, job_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ActionConflict as e:
            raise HTTPException(409, str(e)) from e
        return {"application_id": a.application_id}

    @app.post("/applications/{application_id}/tailor")
    def tailor_api(application_id: str, s: Db) -> dict[str, str]:
        try:
            task_id = start_tailor(s, orchestrator, application_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ActionConflict as e:
            raise HTTPException(409, str(e)) from e
        return {"task_id": task_id}

    @app.get("/applications/{application_id}", response_model=sc.ApplicationDetail)
    def application_detail(application_id: str, s: Db) -> sc.ApplicationDetail:
        try:
            a = get_application(s, application_id)
        except NoResultFound:
            raise HTTPException(404, "application not found") from None
        job = s.get(JobRow, a.job_id)
        assert job is not None
        arts = s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.application_id == application_id)).scalars()
        answers = s.execute(select(FormFieldAnswerRow).where(
            FormFieldAnswerRow.application_id == application_id)
            .order_by(FormFieldAnswerRow.field_id)).scalars()
        return sc.ApplicationDetail(
            application_id=a.application_id, status=a.status, portal=a.portal,
            confirmation_reference=a.confirmation_reference,
            job=sc.JobInfo(job_id=job.job_id, title=job.title, company=job.company,
                           canonical_url=job.canonical_url,
                           application_url=job.application_url),
            artifacts=[sc.ArtifactOut(artifact_id=x.artifact_id, type=x.type,
                                      content_hash=x.content_hash, version=x.version)
                       for x in arts],
            answers=[sc.AnswerOut(
                field_id=x.field_id, label=x.label, field_type=x.field_type,
                proposed_value=json.loads(x.proposed_value_json), decision=x.decision,
                confidence=x.confidence, rationale=x.rationale,
                validation_errors=json.loads(x.validation_errors_json)) for x in answers],
            events=[_event_out(e) for e in list_events(s, application_id)],
        )

    @app.get("/artifacts/{artifact_id}")
    def artifact_file(artifact_id: str, s: Db) -> FileResponse:
        row = s.get(DocumentArtifactRow, artifact_id)
        if row is None:
            raise HTTPException(404, "artifact not found")
        base = (cfg.storage_path / "artifacts").resolve()
        path = (cfg.storage_path / row.path).resolve()
        media = ARTIFACT_MEDIA_TYPES.get(path.suffix.lower())
        if not path.is_relative_to(base) or not path.is_file() or media is None:
            raise HTTPException(404, "artifact not found")
        return FileResponse(path, media_type=media, filename=path.name,
                            content_disposition_type="inline")

    @app.post("/batches", response_model=sc.BatchOut)
    def create_batch(body: sc.BatchCreate, s: Db) -> sc.BatchOut:
        try:
            batch = ApprovalService(s).build_batch(body.application_ids)
        except ValueError as e:
            s.rollback()
            raise HTTPException(422, str(e)) from e
        out = _batch_out(s, batch)
        s.commit()
        return out

    @app.get("/batches/{batch_id}", response_model=sc.BatchOut)
    def get_batch(batch_id: str, s: Db) -> sc.BatchOut:
        batch = latest_batch(s, batch_id)
        if batch is None:
            raise HTTPException(404, "batch not found")
        return _batch_out(s, batch)

    @app.post("/batches/{batch_id}/approve", response_model=sc.ApprovalOut)
    def approve(batch_id: str, body: sc.ApproveIn, s: Db) -> sc.ApprovalOut:
        try:
            res = approve_and_queue(s, orchestrator, batch_id, body.version,
                                    body.content_hash, body.application_ids)
        except StaleApprovalError as e:
            raise HTTPException(409, "stale") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return sc.ApprovalOut(approval_id=res.approval_id, batch_status=res.batch_status)

    @app.get("/events", response_model=list[sc.EventOut])
    def events(s: Db, entity_id: str | None = None,
               limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[sc.EventOut]:
        return [_event_out(e) for e in list_events(s, entity_id, limit)]

    @app.post("/control/{action}", response_model=sc.ControlOut)
    def control(action: Literal["pause", "resume", "stop"]) -> sc.ControlOut:
        in_flight: list[str] = []
        if action == "pause":
            orchestrator.pause()
        elif action == "resume":
            orchestrator.resume()
        else:
            in_flight = orchestrator.stop()
        return sc.ControlOut(action=action, in_flight=in_flight)

    @app.get("/overview", response_model=sc.OverviewOut)
    def overview(s: Db) -> sc.OverviewOut:
        return sc.OverviewOut(
            jobs=counts_by_status(s, JobRow),
            applications=counts_by_status(s, ApplicationRow),
            outreach=counts_by_status(s, OutreachDraftRow),
            batches=counts_by_status(s, ApprovalBatchRow),
            pending_approvals=len(pending_batches(s)),
            recent_failures=[_event_out(e) for e in recent_failures(s)],
        )

    return app


def app_from_config(config_dir: Path = Path("config")) -> FastAPI:
    cfg = load_config(config_dir)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(cfg.db_path)
    factory = make_session_factory(make_engine(cfg.db_path))
    gateway = ModelGateway(cfg)
    rt = build_runtime(cfg, factory)
    orch = Orchestrator(factory, cfg, rt.agents, rt.adapters, handlers=rt.handlers,
                        gateway=gateway, tools=rt.tools)
    return create_app(cfg, factory, orch, gateway, runtime=rt)
