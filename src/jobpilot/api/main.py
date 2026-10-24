"""Localhost FastAPI app: read models, batch approval, controls and health."""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Literal

from alembic.runtime.migration import MigrationContext
from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api import schemas as sc
from jobpilot.config import AppConfig, load_config
from jobpilot.contracts.enums import BatchStatus
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import (
    counts_by_status,
    get_application,
    list_events,
)
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalBatchRow,
    AuditEventRow,
    BatchItemRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    MatchAssessmentRow,
    OutreachDraftRow,
)
from jobpilot.llm.ollama_client import ModelGateway
from jobpilot.orchestrator.approval import ApprovalService, StaleApprovalError
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter


async def worker_loop(orch: Orchestrator, stop: asyncio.Event) -> None:
    while not stop.is_set():
        did_work = await orch.run_once()
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
    items = s.execute(select(BatchItemRow).where(
        BatchItemRow.batch_id == batch.batch_id, BatchItemRow.version == batch.version,
    ).order_by(BatchItemRow.application_id)).scalars()
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


def _health(cfg: AppConfig, session_factory: sessionmaker[Session],
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

    app = FastAPI(title="JobPilot", lifespan=lifespan)

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
        return await asyncio.to_thread(_health, cfg, session_factory, available)

    @app.get("/jobs", response_model=list[sc.JobSummary])
    def jobs(s: Db, status: str | None = None,
             min_score: float | None = None) -> list[sc.JobSummary]:
        q = select(JobRow, func.max(MatchAssessmentRow.total_score)).outerjoin(
            MatchAssessmentRow, MatchAssessmentRow.job_id == JobRow.job_id
        ).group_by(JobRow.job_id).order_by(JobRow.first_seen_at.desc(), JobRow.job_id)
        if status is not None:
            q = q.where(JobRow.status == status)
        out: list[sc.JobSummary] = []
        for job, score in s.execute(q).all():
            if min_score is not None and (score is None or score < min_score):
                continue
            out.append(sc.JobSummary(**_job_summary(job, score)))
        return out

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

    @app.get("/applications/{application_id}", response_model=sc.ApplicationDetail)
    def application_detail(application_id: str, s: Db) -> sc.ApplicationDetail:
        try:
            a = get_application(s, application_id)
        except Exception:
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
        batch = s.execute(select(ApprovalBatchRow).where(ApprovalBatchRow.batch_id == batch_id)
                          .order_by(ApprovalBatchRow.version.desc())).scalars().first()
        if batch is None:
            raise HTTPException(404, "batch not found")
        return _batch_out(s, batch)

    @app.post("/batches/{batch_id}/approve", response_model=sc.ApprovalOut)
    def approve(batch_id: str, body: sc.ApproveIn, s: Db) -> sc.ApprovalOut:
        try:
            approval = ApprovalService(s).approve(
                batch_id, body.version, body.content_hash, body.application_ids)
        except StaleApprovalError as e:
            s.rollback()
            raise HTTPException(409, "stale") from e
        except ValueError as e:
            s.rollback()
            raise HTTPException(422, str(e)) from e
        batch = s.get(ApprovalBatchRow, (batch_id, body.version))
        assert batch is not None
        out = sc.ApprovalOut(approval_id=approval.approval_id, batch_status=batch.status)
        s.commit()
        return out

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
        latest = select(ApprovalBatchRow.batch_id, func.max(ApprovalBatchRow.version).label("v")
                        ).group_by(ApprovalBatchRow.batch_id).subquery()
        pending = s.execute(select(func.count()).select_from(ApprovalBatchRow).join(
            latest, (ApprovalBatchRow.batch_id == latest.c.batch_id)
            & (ApprovalBatchRow.version == latest.c.v)
        ).where(ApprovalBatchRow.status.in_((BatchStatus.READY_FOR_REVIEW.value,
                                             BatchStatus.PARTIALLY_APPROVED.value)))
        ).scalar_one()
        failures = s.execute(select(AuditEventRow).where(AuditEventRow.error_code.is_not(None))
                             .order_by(AuditEventRow.timestamp_utc.desc(),
                                       AuditEventRow.event_id.desc()).limit(10)).scalars()
        return sc.OverviewOut(
            jobs=counts_by_status(s, JobRow),
            applications=counts_by_status(s, ApplicationRow),
            outreach=counts_by_status(s, OutreachDraftRow),
            batches=counts_by_status(s, ApprovalBatchRow),
            pending_approvals=pending,
            recent_failures=[_event_out(e) for e in failures],
        )

    return app


def app_from_config() -> FastAPI:
    cfg = load_config(Path("config"))
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(cfg.db_path)
    factory = make_session_factory(make_engine(cfg.db_path))
    gateway = ModelGateway(cfg)
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()},
                        handlers={}, gateway=gateway)
    return create_app(cfg, factory, orch, gateway)
