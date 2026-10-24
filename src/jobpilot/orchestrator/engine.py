"""Resumable orchestrator: durable task queue, validated agent dispatch,
write-ahead gated side effects, crash recovery and pause/stop control."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import (
    AgentRunStatus,
    ApplicationStatus,
    ErrorCode,
    FieldDecision,
    JobStatus,
    OutreachStatus,
    PortalCapability,
    Recovery,
    recovery_for,
)
from jobpilot.contracts.models import SubmitResult
from jobpilot.data.db import utcnow
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import (
    AgentRunRow,
    ApplicationRow,
    ControlFlagRow,
    FormFieldAnswerRow,
    JobRow,
    OutreachDraftRow,
    TaskRow,
)
from jobpilot.ids import new_id
from jobpilot.llm.ollama_client import ModelError, ModelGateway
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.policies import PolicyConfig, may_send_outreach, may_submit
from jobpilot.orchestrator.state_machine import transition
from jobpilot.portals.base import ApprovedApplication, PortalAdapter, idempotency_key
from jobpilot.portals.simulated import SimulatedAdapter

log = logging.getLogger(__name__)

I = TypeVar("I", contravariant=True)  # noqa: E741
O = TypeVar("O", covariant=True)  # noqa: E741

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 8, 32)
_ACTOR = "orchestrator"
_PAUSED_KEY = "paused"
_UNRESOLVED_DECISIONS = (FieldDecision.ASK_USER.value, FieldDecision.BLOCKED.value)


class TaskKind(StrEnum):
    DISCOVER = "DISCOVER"
    MATCH = "MATCH"
    TAILOR = "TAILOR"
    PREPARE_FORM = "PREPARE_FORM"
    VALIDATE = "VALIDATE"
    SUBMIT = "SUBMIT"
    OUTREACH_RESEARCH = "OUTREACH_RESEARCH"
    OUTREACH_SEND = "OUTREACH_SEND"


SIDE_EFFECT_KINDS = frozenset({TaskKind.SUBMIT, TaskKind.OUTREACH_SEND})


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskInput(BaseModel):
    """What an agent receives: the task's identity; agents load the rest via tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    kind: TaskKind
    entity_type: str
    entity_id: str
    attempt: int


@dataclass(frozen=True)
class AgentContext:
    task_id: str
    gateway: ModelGateway | None
    tools: dict[str, Any] = field(default_factory=dict)


class Agent(Protocol[I, O]):
    name: str
    prompt_version: str
    output_model: type[BaseModel]
    tools: tuple[str, ...]

    async def run(self, inp: I, ctx: AgentContext) -> O: ...


FollowUp = tuple[TaskKind, str, str]
Handler = Callable[[Session, TaskRow, BaseModel], list[FollowUp]]


class OutputRejected(Exception):
    """Raised by a handler when agent output fails business validation."""


def noop_handler(session: Session, task: TaskRow, output: BaseModel) -> list[FollowUp]:
    """Test-only default: accept output, write nothing, enqueue nothing."""
    return []


class Orchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cfg: AppConfig,
        agents: dict[TaskKind, Any],
        adapters: dict[str, PortalAdapter],
        clock: Callable[[], datetime] = utcnow,
        *,
        handlers: dict[TaskKind, Handler] | None = None,
        gateway: ModelGateway | None = None,
        tools: dict[str, Any] | None = None,
        lease_seconds: float = 300,
        agent_timeout_s: float = 300,
        adapter_timeout_s: float = 120,
    ) -> None:
        self._sf = session_factory
        self._cfg = cfg
        self._agents = agents
        self._adapters = adapters
        self._clock = clock
        self._handlers = handlers if handlers is not None else {
            k: noop_handler for k in agents
        }
        self._gateway = gateway
        self._tools = tools or {}
        self._lease = timedelta(seconds=lease_seconds)
        self._agent_timeout_s = agent_timeout_s
        self._adapter_timeout_s = adapter_timeout_s

    # ---- queue -----------------------------------------------------------
    def enqueue(
        self,
        kind: TaskKind,
        entity_type: str,
        entity_id: str,
        not_before: datetime | None = None,
    ) -> str:
        with self._sf() as s:
            task_id = self._add_task(s, kind, entity_type, entity_id, not_before)
            s.commit()
            return task_id

    def enqueue_in(
        self, s: Session, kind: TaskKind, entity_type: str, entity_id: str,
    ) -> str:
        """Queue a task inside the caller's transaction (caller commits)."""
        task_id = self._add_task(s, kind, entity_type, entity_id)
        s.flush()
        return task_id

    def _add_task(
        self, s: Session, kind: TaskKind, entity_type: str, entity_id: str,
        not_before: datetime | None = None,
    ) -> str:
        task_id = new_id("task")
        s.add(TaskRow(task_id=task_id, kind=TaskKind(kind).value, entity_type=entity_type,
                      entity_id=entity_id, status=TaskStatus.QUEUED.value, attempts=0,
                      not_before=not_before))
        record_event(s, entity_type="task", entity_id=task_id, actor=_ACTOR,
                     action="task_enqueued", outcome="ok",
                     details={"kind": kind, "entity_type": entity_type,
                              "entity_id": entity_id})
        return task_id

    def _lease_next(self) -> str | None:
        now = self._clock()
        with self._sf() as s:
            if self._is_paused(s):
                return None
            task = s.execute(
                select(TaskRow)
                .where(TaskRow.status == TaskStatus.QUEUED.value,
                       or_(TaskRow.not_before.is_(None), TaskRow.not_before <= now))
                .order_by(TaskRow.not_before.is_not(None), TaskRow.not_before,
                          TaskRow.task_id)
                .limit(1)
            ).scalar_one_or_none()
            if task is None:
                return None
            task.status = TaskStatus.RUNNING.value
            task.attempts += 1
            task.lease_until = now + self._lease
            s.commit()
            return task.task_id

    # ---- execution -------------------------------------------------------
    async def run_once(self) -> bool:
        task_id = self._lease_next()
        if task_id is None:
            return False
        with self._sf() as s:
            task = s.get(TaskRow, task_id)
            assert task is not None
            try:
                kind = TaskKind(task.kind)
                if kind is TaskKind.SUBMIT:
                    await self._run_submit(s, task)
                elif kind is TaskKind.OUTREACH_SEND:
                    await self._run_outreach_send(s, task)
                else:
                    await self._run_agent(s, task, kind)
            except Exception as exc:  # contain everything; the loop must keep going
                log.exception("task %s crashed", task_id)
                s.rollback()
                self._crash(s, task_id, exc)
        return True

    async def run_until_idle(self, max_steps: int = 1000) -> int:
        steps = 0
        while steps < max_steps and await self.run_once():
            steps += 1
        return steps

    def _finish(self, s: Session, task: TaskRow, status: TaskStatus) -> None:
        task.status = status.value
        task.lease_until = None

    def _crash(self, s: Session, task_id: str, exc: BaseException) -> None:
        task = s.get(TaskRow, task_id)
        if task is None:
            return
        self._finish(s, task, TaskStatus.FAILED)
        record_event(s, entity_type="task", entity_id=task_id, actor=_ACTOR,
                     action="task_crashed", outcome="error",
                     details={"error_type": type(exc).__name__})
        s.commit()

    def _record_run(
        self, s: Session, task: TaskRow, agent: Any, status: AgentRunStatus,
        started: float, error_code: ErrorCode | None = None,
    ) -> AgentRunRow:
        row = AgentRunRow(
            run_id=new_id("run"), agent_name=getattr(agent, "name", "unknown"),
            task_id=task.task_id, status=status.value,
            prompt_version=getattr(agent, "prompt_version", None),
            input_ref=f"task:{task.task_id}",
            latency_ms=int((time.monotonic() - started) * 1000),
            error_code=error_code.value if error_code else None,
        )
        s.add(row)
        return row

    def _fail(
        self, s: Session, task: TaskRow, action: str, details: dict,
        error_code: ErrorCode | None = None,
    ) -> None:
        self._finish(s, task, TaskStatus.FAILED)
        record_event(s, entity_type=task.entity_type, entity_id=task.entity_id,
                     actor=_ACTOR, action=action, outcome="error",
                     details={"task_id": task.task_id, "kind": task.kind, **details},
                     error_code=error_code)

    async def _run_agent(self, s: Session, task: TaskRow, kind: TaskKind) -> None:
        agent = self._agents.get(kind)
        handler = self._handlers.get(kind)
        if agent is None or handler is None:
            self._fail(s, task, "task_failed",
                       {"reason": "no_agent" if agent is None else "no_handler"})
            s.commit()
            return
        declared = tuple(getattr(agent, "tools", ()))
        ctx = AgentContext(task_id=task.task_id, gateway=self._gateway,
                           tools={k: v for k, v in self._tools.items() if k in declared})
        inp = TaskInput(task_id=task.task_id, kind=kind, entity_type=task.entity_type,
                        entity_id=task.entity_id, attempt=task.attempts)
        started = time.monotonic()
        try:
            output = await asyncio.wait_for(agent.run(inp, ctx), self._agent_timeout_s)
        except (ModelError, TimeoutError) as e:
            code = e.code if isinstance(e, ModelError) else ErrorCode.MODEL_TIMEOUT
            self._record_run(s, task, agent, AgentRunStatus.FAILED, started, code)
            self._handle_model_error(s, task, code)
            s.commit()
            return

        model = agent.output_model
        try:
            if type(output) is not model:
                raise OutputRejected(f"expected {model.__name__}, got {type(output).__name__}")
            validated = model.model_validate(output.model_dump(warnings=False))
            follow_ups = handler(s, task, validated)
        except (ValidationError, OutputRejected) as e:
            s.rollback()
            task = s.get(TaskRow, task.task_id)  # type: ignore[assignment]
            assert task is not None
            self._record_run(s, task, agent, AgentRunStatus.FAILED, started,
                             ErrorCode.MODEL_INVALID_SCHEMA)
            self._fail(s, task, "output_rejected", {"error_type": type(e).__name__},
                       ErrorCode.MODEL_INVALID_SCHEMA)
            s.commit()
            return
        for fk, fet, feid in follow_ups:
            self._add_task(s, fk, fet, feid)
        self._record_run(s, task, agent, AgentRunStatus.SUCCEEDED, started)
        self._finish(s, task, TaskStatus.SUCCEEDED)
        s.commit()

    def _handle_model_error(self, s: Session, task: TaskRow, code: ErrorCode) -> None:
        if recovery_for(code) is Recovery.RETRY_BACKOFF and task.attempts < MAX_ATTEMPTS:
            delay = BACKOFF_SECONDS[task.attempts - 1]
            task.status = TaskStatus.QUEUED.value
            task.lease_until = None
            task.not_before = self._clock() + timedelta(seconds=delay)
            record_event(s, entity_type="task", entity_id=task.task_id, actor=_ACTOR,
                         action="task_retry_scheduled", outcome="retry",
                         details={"attempt": task.attempts, "delay_s": delay},
                         error_code=code)
            return
        self._fail(s, task, "task_failed", {"attempts": task.attempts}, code)

    # ---- side effects ----------------------------------------------------
    def _adapter_for(self, app: ApplicationRow) -> tuple[PortalAdapter | None, bool]:
        if self._cfg.dry_run:
            return self._adapters.get("simulated") or SimulatedAdapter(), True
        adapter = self._adapters.get(app.portal or "")
        return adapter, isinstance(adapter, SimulatedAdapter)

    async def _run_submit(self, s: Session, task: TaskRow) -> None:
        app = get_application(s, task.entity_id)
        adapter, simulated = self._adapter_for(app)
        if self._cfg.dry_run and not isinstance(adapter, SimulatedAdapter):
            self._fail(s, task, "gate_denied", {"reasons": ["dry_run"]})
            s.commit()
            return
        svc = ApprovalService(s)
        approved_hash = svc.current_item_hash(app.application_id)
        caps = await adapter.capabilities() if adapter is not None else frozenset()
        policy = PolicyConfig(dry_run=self._cfg.dry_run,
                              portal_capabilities={app.portal or "": caps})
        unresolved = len(list(s.execute(select(FormFieldAnswerRow).where(
            FormFieldAnswerRow.application_id == app.application_id,
            FormFieldAnswerRow.decision.in_(_UNRESOLVED_DECISIONS),
        )).scalars()))
        job = s.get(JobRow, app.job_id)
        duplicate = job is not None and (
            job.status == JobStatus.DUPLICATE or job.canonical_job_id is not None)
        gate = may_submit(app, svc.active_approval_for(app.application_id),
                          svc.application_hash(app.application_id), unresolved, duplicate,
                          policy, simulated=simulated, approved_item_hash=approved_hash)
        if not gate.allowed or adapter is None:
            reasons = gate.reasons or ["no_adapter"]
            self._fail(s, task, "gate_denied", {"reasons": reasons})
            s.commit()
            return

        portal = adapter.name
        key = idempotency_key(app.application_id, portal, "submit", approved_hash or "")
        app.idempotency_key = key
        transition(s, app, ApplicationStatus.SUBMISSION_IN_PROGRESS, actor=_ACTOR,
                   reason="submit_started")
        record_event(s, entity_type="application", entity_id=app.application_id,
                     actor=_ACTOR, action="submit_started", outcome="ok",
                     details={"idempotency_key": key, "task_id": task.task_id,
                              "simulated": simulated})
        s.commit()  # write-ahead: never act before this is durable

        try:
            result = await asyncio.wait_for(
                adapter.find_existing_submission(key), self._adapter_timeout_s)
            if result is None:
                result = await asyncio.wait_for(adapter.submit(ApprovedApplication(
                    application_id=app.application_id, approved_hash=approved_hash or "",
                    idempotency_key=key)), self._adapter_timeout_s)
        except TimeoutError:
            transition(s, app, ApplicationStatus.SUBMISSION_UNCERTAIN, actor=_ACTOR,
                       reason="submit_timeout")
            self._fail(s, task, "submit_uncertain", {"idempotency_key": key},
                       ErrorCode.SUBMIT_TIMEOUT)
            s.commit()
            return
        except Exception as exc:
            self._submit_error(s, task.task_id, app.application_id, exc, None)
            return
        try:
            self._apply_submit_result(s, app, result)
            self._finish(s, task, TaskStatus.SUCCEEDED)
            s.commit()
        except Exception as exc:
            self._submit_error(s, task.task_id, app.application_id, exc, result)

    def _submit_error(self, s: Session, task_id: str, application_id: str,
                      exc: Exception, result: SubmitResult | None) -> None:
        """Adapter outcome unknown after write-ahead: never leave IN_PROGRESS."""
        log.exception("submit for %s errored after write-ahead", application_id)
        s.rollback()
        app = get_application(s, application_id)
        task = s.get(TaskRow, task_id)
        assert task is not None
        if app.status in (ApplicationStatus.SUBMISSION_IN_PROGRESS,
                          ApplicationStatus.SUBMITTED):
            transition(s, app, ApplicationStatus.SUBMISSION_UNCERTAIN, actor=_ACTOR,
                       reason="submit_error")
        details: dict[str, Any] = {"error_type": type(exc).__name__}
        if result is not None:
            details["confirmation_reference"] = result.confirmation_reference
        self._fail(s, task, "submit_error", details, ErrorCode.SUBMIT_NO_CONFIRMATION)
        s.commit()

    def _apply_submit_result(self, s: Session, app: ApplicationRow, r: SubmitResult) -> None:
        details = {"confirmation_reference": r.confirmation_reference}
        if r.status in (ApplicationStatus.SUBMITTED, ApplicationStatus.CONFIRMED):
            transition(s, app, ApplicationStatus.SUBMITTED, actor=_ACTOR,
                       reason="submitted", details=details)
        if r.status is ApplicationStatus.CONFIRMED:
            transition(s, app, ApplicationStatus.CONFIRMED, actor=_ACTOR,
                       reason="confirmed", details=details)
        elif r.status in (ApplicationStatus.SUBMISSION_UNCERTAIN,
                          ApplicationStatus.SUBMISSION_FAILED):
            transition(s, app, ApplicationStatus(r.status), actor=_ACTOR,
                       reason="adapter_result", details=details)
        elif r.status is not ApplicationStatus.SUBMITTED:
            raise ValueError(f"unexpected submit status {r.status!r}")
        app.confirmation_reference = r.confirmation_reference
        record_event(s, entity_type="application", entity_id=app.application_id,
                     actor=_ACTOR, action="submit_finished", outcome=str(r.status),
                     evidence_ref=r.evidence_url, details=details)

    async def _run_outreach_send(self, s: Session, task: TaskRow) -> None:
        draft = s.get(OutreachDraftRow, task.entity_id)
        if draft is None:
            self._fail(s, task, "task_failed", {"reason": "unknown_outreach"})
            s.commit()
            return
        svc = ApprovalService(s)
        already_sent = s.execute(select(OutreachDraftRow.outreach_id).where(
            OutreachDraftRow.company_key == draft.company_key,
            OutreachDraftRow.outreach_id != draft.outreach_id,
            OutreachDraftRow.status == OutreachStatus.SENT.value,
        )).first() is not None
        simulated = self._cfg.dry_run  # no real outreach adapter exists yet
        caps = {draft.channel or "": frozenset({PortalCapability.SEND_OUTREACH})} \
            if simulated else {}
        gate = may_send_outreach(
            draft, svc.active_outreach_approval_for(draft.outreach_id),
            svc.outreach_hash(draft.outreach_id), already_sent,
            PolicyConfig(dry_run=self._cfg.dry_run, portal_capabilities=caps),
            simulated=simulated)
        if not gate.allowed:
            self._fail(s, task, "gate_denied", {"reasons": gate.reasons})
            s.commit()
            return
        transition(s, draft, OutreachStatus.SEND_IN_PROGRESS, actor=_ACTOR,
                   reason="send_started")
        record_event(s, entity_type="outreach", entity_id=draft.outreach_id, actor=_ACTOR,
                     action="send_started", outcome="ok",
                     details={"task_id": task.task_id, "simulated": True})
        s.commit()  # write-ahead
        task_id, outreach_id = task.task_id, draft.outreach_id
        try:
            self._simulated_send(s, draft)
            self._finish(s, task, TaskStatus.SUCCEEDED)
            s.commit()
        except Exception as exc:
            log.exception("send for %s errored after write-ahead", outreach_id)
            s.rollback()
            d = s.get(OutreachDraftRow, outreach_id)
            t = s.get(TaskRow, task_id)
            assert d is not None and t is not None
            if d.status == OutreachStatus.SEND_IN_PROGRESS:
                transition(s, d, OutreachStatus.DELIVERY_UNKNOWN, actor=_ACTOR,
                           reason="send_error")
            self._fail(s, t, "send_error", {"error_type": type(exc).__name__},
                       ErrorCode.OUTREACH_DELIVERY_UNKNOWN)
            s.commit()

    def _simulated_send(self, s: Session, draft: OutreachDraftRow) -> None:
        draft.sent_at = self._clock()
        draft.evidence_ref = "simulated"
        transition(s, draft, OutreachStatus.SENT, actor=_ACTOR, reason="simulated_send")

    # ---- recovery & control ---------------------------------------------
    def recover_on_startup(self) -> list[str]:
        now = self._clock()
        affected: list[str] = []
        with self._sf() as s:
            expired = s.execute(select(TaskRow).where(
                TaskRow.status == TaskStatus.RUNNING.value,
                TaskRow.lease_until.is_not(None), TaskRow.lease_until < now,
            ).order_by(TaskRow.task_id)).scalars().all()
            for task in expired:
                affected.append(task.task_id)
                kind = TaskKind(task.kind)
                if kind not in SIDE_EFFECT_KINDS:
                    task.status = TaskStatus.QUEUED.value
                    task.lease_until = None
                    record_event(s, entity_type="task", entity_id=task.task_id,
                                 actor=_ACTOR, action="task_requeued", outcome="ok",
                                 details={"reason": "lease_expired"})
                    continue
                self._recover_side_effect(s, task, kind)
                self._finish(s, task, TaskStatus.FAILED)
                record_event(s, entity_type="task", entity_id=task.task_id, actor=_ACTOR,
                             action="task_interrupted", outcome="error",
                             details={"reason": "lease_expired", "kind": kind})
            self._sweep_stranded(s)
            self._requeue_orphaned_approvals(s)
            s.commit()
        return affected

    def _sweep_stranded(self, s: Session) -> None:
        """Entities left mid-side-effect with no RUNNING task to own them."""
        running = {(t.kind, t.entity_id) for t in s.execute(select(TaskRow).where(
            TaskRow.status == TaskStatus.RUNNING.value)).scalars()}
        apps = s.execute(select(ApplicationRow).where(
            ApplicationRow.status == ApplicationStatus.SUBMISSION_IN_PROGRESS.value)).scalars()
        for app in apps.all():
            if (TaskKind.SUBMIT.value, app.application_id) not in running:
                transition(s, app, ApplicationStatus.SUBMISSION_UNCERTAIN, actor=_ACTOR,
                           reason="stranded_in_progress")
        drafts = s.execute(select(OutreachDraftRow).where(
            OutreachDraftRow.status == OutreachStatus.SEND_IN_PROGRESS.value)).scalars()
        for d in drafts.all():
            if (TaskKind.OUTREACH_SEND.value, d.outreach_id) not in running:
                transition(s, d, OutreachStatus.DELIVERY_UNKNOWN, actor=_ACTOR,
                           reason="stranded_in_progress")

    def _requeue_orphaned_approvals(self, s: Session) -> None:
        """APPROVED apps with a valid approval and no live SUBMIT task get one; those
        whose approval was item-revoked return to review."""
        live = set(s.execute(select(TaskRow.entity_id).where(
            TaskRow.kind == TaskKind.SUBMIT.value,
            TaskRow.status.in_((TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)),
        )).scalars())
        apps = s.execute(select(ApplicationRow.application_id).where(
            ApplicationRow.status == ApplicationStatus.APPROVED.value
        ).order_by(ApplicationRow.application_id)).scalars().all()
        svc = ApprovalService(s)
        for app_id in apps:
            if svc.active_approval_for(app_id) is None:
                transition(s, get_application(s, app_id), ApplicationStatus.READY_FOR_REVIEW,
                           actor=_ACTOR, reason="approval_item_revoked")
            elif app_id not in live:
                self._add_task(s, TaskKind.SUBMIT, "application", app_id)

    def _recover_side_effect(self, s: Session, task: TaskRow, kind: TaskKind) -> None:
        if kind is TaskKind.SUBMIT:
            app = s.get(ApplicationRow, task.entity_id)
            if app is not None and app.status in (ApplicationStatus.SUBMISSION_IN_PROGRESS,
                                                  ApplicationStatus.SUBMITTED):
                transition(s, app, ApplicationStatus.SUBMISSION_UNCERTAIN, actor=_ACTOR,
                           reason="crash_recovery")
        else:
            draft = s.get(OutreachDraftRow, task.entity_id)
            if draft is not None and draft.status == OutreachStatus.SEND_IN_PROGRESS:
                transition(s, draft, OutreachStatus.DELIVERY_UNKNOWN, actor=_ACTOR,
                           reason="crash_recovery")

    def _is_paused(self, s: Session) -> bool:
        flag = s.get(ControlFlagRow, _PAUSED_KEY)
        return flag is not None and flag.value == "true"

    def _set_paused(self, paused: bool) -> None:
        with self._sf() as s:
            flag = s.get(ControlFlagRow, _PAUSED_KEY)
            value = "true" if paused else "false"
            if flag is None:
                s.add(ControlFlagRow(key=_PAUSED_KEY, value=value, updated_at=self._clock()))
            else:
                flag.value, flag.updated_at = value, self._clock()
            record_event(s, entity_type="control", entity_id=_PAUSED_KEY, actor="user",
                         action="paused" if paused else "resumed", outcome="ok")
            s.commit()

    def pause(self) -> None:
        self._set_paused(True)

    def resume(self) -> None:
        self._set_paused(False)

    def stop(self) -> list[str]:
        with self._sf() as s:
            tasks = s.execute(select(TaskRow).where(TaskRow.status.in_(
                (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)))).scalars().all()
            in_flight: list[str] = []
            for t in tasks:
                if t.status == TaskStatus.RUNNING:
                    in_flight.append(t.task_id)
                    continue
                t.status = TaskStatus.CANCELLED.value
                record_event(s, entity_type="task", entity_id=t.task_id, actor="user",
                             action="task_cancelled", outcome="ok")
            s.commit()
            return sorted(in_flight)
