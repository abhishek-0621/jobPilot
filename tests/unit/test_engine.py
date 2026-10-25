from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ErrorCode
from jobpilot.contracts.models import AgentOutput
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import get_application, list_events
from jobpilot.data.tables import (
    AgentRunRow,
    ContactCandidateRow,
    JobRow,
    OutreachDraftRow,
    TaskRow,
)
from jobpilot.ids import new_id
from jobpilot.llm.ollama_client import ModelError
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import (
    AgentContext,
    Orchestrator,
    TaskKind,
    TaskStatus,
)
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import make_app_row, seed_ready_apps

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class MatchOut(AgentOutput):
    job_id: str
    score: float


class Other(BaseModel):
    job_id: str


class StubAgent:
    name = "matcher"
    prompt_version = "v1"
    output_model: type[BaseModel] = MatchOut

    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.calls = 0
        self.seen_ctx: AgentContext | None = None

    async def run(self, inp: Any, ctx: AgentContext) -> Any:
        self.calls += 1
        self.seen_ctx = ctx
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour(inp, ctx)


def good_output(inp: Any, ctx: AgentContext) -> MatchOut:
    return MatchOut(task_id=ctx.task_id, created_at=T0, job_id=inp.entity_id, score=80)


class MatchOutExtra(MatchOut):
    bogus: str


def returns_extra_field(inp: Any, ctx: AgentContext) -> Any:
    return MatchOutExtra(task_id=ctx.task_id, created_at=T0, job_id=inp.entity_id,
                         score=80, bogus="x")


def returns_invalid_value(inp: Any, ctx: AgentContext) -> Any:
    return MatchOut.model_construct(task_id=ctx.task_id, created_at=T0,
                                    job_id=inp.entity_id, score="high", warnings=[],
                                    schema_version="1")


def returns_wrong_type(inp: Any, ctx: AgentContext) -> Any:
    return Other(job_id=inp.entity_id)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "e.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


def cfg(dry_run: bool = True) -> AppConfig:
    return AppConfig(dry_run=dry_run, model_routes={})


def make_orch(factory: sessionmaker[Session], clock: FakeClock, agents: dict | None = None,
              handlers: dict | None = None, adapter: SimulatedAdapter | None = None,
              **kw: Any) -> Orchestrator:
    return Orchestrator(factory, cfg(), agents or {},
                        {"simulated": adapter or SimulatedAdapter()},
                        clock=clock, handlers=handlers, **kw)


def seed_job(factory: sessionmaker[Session]) -> str:
    with factory() as s:
        app = make_app_row(s)
        s.commit()
        return app.job_id


def seed_approved_app(factory: sessionmaker[Session], approve: bool = True) -> str:
    with factory() as s:
        (a1,) = seed_ready_apps(s, 1)
        get_application(s, a1).portal = "simulated"
        if approve:
            svc = ApprovalService(s)
            b = svc.build_batch([a1])
            svc.approve(b.batch_id, b.version, b.content_hash, [a1])
        s.commit()
        return a1


def task(factory: sessionmaker[Session], task_id: str) -> TaskRow:
    with factory() as s:
        row = s.get(TaskRow, task_id)
        assert row is not None
        return row


def actions(factory: sessionmaker[Session], entity_id: str) -> list[str]:
    with factory() as s:
        return [e.action for e in list_events(s, entity_id)]


async def test_valid_output_runs_handler_and_enqueues_followup(factory) -> None:
    clock = FakeClock()
    job_id = seed_job(factory)
    seen: list[BaseModel] = []

    def handler(s: Session, t: TaskRow, out: BaseModel) -> list[tuple[TaskKind, str, str]]:
        seen.append(out)
        return [(TaskKind.TAILOR, "job", t.entity_id)]

    agent = StubAgent(good_output)
    orch = make_orch(factory, clock, {TaskKind.MATCH: agent}, {TaskKind.MATCH: handler},
                     tools={"search": object(), "secret_tool": object()})
    agent.tools = ("search",)  # type: ignore[attr-defined]
    tid = orch.enqueue(TaskKind.MATCH, "job", job_id)
    assert await orch.run_once() is True
    assert task(factory, tid).status == TaskStatus.SUCCEEDED
    assert isinstance(seen[0], MatchOut)
    assert agent.seen_ctx is not None and set(agent.seen_ctx.tools) == {"search"}
    with factory() as s:
        kinds = [t.kind for t in s.execute(select(TaskRow)).scalars()]
        runs = list(s.execute(select(AgentRunRow)).scalars())
    assert TaskKind.TAILOR in kinds
    assert runs[0].status == "SUCCEEDED" and runs[0].task_id == tid


@pytest.mark.parametrize(
    "behaviour", [returns_extra_field, returns_invalid_value, returns_wrong_type])
async def test_invalid_agent_output_rejected(factory, behaviour) -> None:
    clock = FakeClock()
    job_id = seed_job(factory)
    called: list[int] = []
    orch = make_orch(factory, clock, {TaskKind.MATCH: StubAgent(behaviour)},
                     {TaskKind.MATCH: lambda s, t, o: called.append(1) or []})
    tid = orch.enqueue(TaskKind.MATCH, "job", job_id)
    assert await orch.run_once() is True
    assert task(factory, tid).status == TaskStatus.FAILED
    assert not called
    with factory() as s:
        assert s.get(JobRow, job_id).status == "DISCOVERED"  # type: ignore[union-attr]
    assert "output_rejected" in actions(factory, job_id)


async def test_retryable_model_error_backs_off(factory) -> None:
    clock = FakeClock()
    job_id = seed_job(factory)
    agent = StubAgent(ModelError(ErrorCode.MODEL_TIMEOUT, "slow"))
    orch = make_orch(factory, clock, {TaskKind.MATCH: agent}, {TaskKind.MATCH: lambda *a: []})
    tid = orch.enqueue(TaskKind.MATCH, "job", job_id)
    assert await orch.run_once() is True
    t = task(factory, tid)
    assert t.status == TaskStatus.QUEUED and t.attempts == 1
    assert t.not_before == T0 + timedelta(seconds=2)
    assert await orch.run_once() is False  # not yet due
    clock.advance(2)
    assert await orch.run_once() is True
    t = task(factory, tid)
    assert t.attempts == 2 and t.not_before == clock.now + timedelta(seconds=8)
    clock.advance(8)
    assert await orch.run_once() is True
    t = task(factory, tid)
    assert t.attempts == 3 and t.status == TaskStatus.FAILED
    assert agent.calls == 3
    assert "task_failed" in actions(factory, job_id)


async def test_non_retryable_model_error_fails(factory) -> None:
    clock = FakeClock()
    job_id = seed_job(factory)
    agent = StubAgent(ModelError(ErrorCode.FORM_VALIDATION, "bad"))
    orch = make_orch(factory, clock, {TaskKind.MATCH: agent}, {TaskKind.MATCH: lambda *a: []})
    tid = orch.enqueue(TaskKind.MATCH, "job", job_id)
    await orch.run_once()
    assert task(factory, tid).status == TaskStatus.FAILED and agent.calls == 1


async def test_crash_contained(factory) -> None:
    clock = FakeClock()
    job_id = seed_job(factory)

    def boom(s: Session, t: TaskRow, o: BaseModel) -> list:
        s.add(OutreachDraftRow(outreach_id="x", company_key="c", campaign_id="c",
                               message_text="m", status="DRAFT_READY"))
        raise RuntimeError("password=hunter2")

    orch = make_orch(factory, clock, {TaskKind.MATCH: StubAgent(good_output)},
                     {TaskKind.MATCH: boom})
    tid = orch.enqueue(TaskKind.MATCH, "job", job_id)
    assert await orch.run_once() is True
    assert task(factory, tid).status == TaskStatus.FAILED
    with factory() as s:
        assert s.get(OutreachDraftRow, "x") is None  # rolled back
        ev = [e for e in list_events(s, tid) if e.action == "task_crashed"]
    assert ev and "hunter2" not in ev[0].redacted_details_json
    assert "RuntimeError" in ev[0].redacted_details_json


async def test_restart_requeues_expired_non_side_effect(factory) -> None:
    clock = FakeClock()
    orch = make_orch(factory, clock)
    tid = orch.enqueue(TaskKind.MATCH, "job", "job_x")
    with factory() as s:
        t = s.get(TaskRow, tid)
        t.status, t.lease_until = TaskStatus.RUNNING.value, T0 - timedelta(seconds=1)  # type: ignore[union-attr]
        s.commit()
    assert orch.recover_on_startup() == [tid]
    assert task(factory, tid).status == TaskStatus.QUEUED


async def test_restart_ignores_unexpired_lease(factory) -> None:
    clock = FakeClock()
    orch = make_orch(factory, clock)
    tid = orch.enqueue(TaskKind.MATCH, "job", "job_x")
    with factory() as s:
        t = s.get(TaskRow, tid)
        t.status, t.lease_until = TaskStatus.RUNNING.value, T0 + timedelta(seconds=60)  # type: ignore[union-attr]
        s.commit()
    assert orch.recover_on_startup() == []


async def test_restart_marks_inflight_submit_uncertain(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)
    orch = make_orch(factory, clock)
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    with factory() as s:
        from jobpilot.contracts.enums import ApplicationStatus
        from jobpilot.orchestrator.state_machine import transition
        transition(s, get_application(s, app_id), ApplicationStatus.SUBMISSION_IN_PROGRESS,
                   actor="system", reason="test")
        t = s.get(TaskRow, tid)
        t.status, t.lease_until = TaskStatus.RUNNING.value, T0 - timedelta(seconds=1)  # type: ignore[union-attr]
        s.commit()
    assert orch.recover_on_startup() == [tid]
    with factory() as s:
        assert get_application(s, app_id).status == "SUBMISSION_UNCERTAIN"
    assert task(factory, tid).status == TaskStatus.FAILED


async def test_restart_marks_inflight_send_delivery_unknown(factory) -> None:
    clock = FakeClock()
    with factory() as s:
        s.add(OutreachDraftRow(outreach_id="o1", company_key="acme", campaign_id="c1",
                               message_text="hi", status="SEND_IN_PROGRESS"))
        s.commit()
    orch = make_orch(factory, clock)
    tid = orch.enqueue(TaskKind.OUTREACH_SEND, "outreach", "o1")
    with factory() as s:
        t = s.get(TaskRow, tid)
        t.status, t.lease_until = TaskStatus.RUNNING.value, T0 - timedelta(seconds=1)  # type: ignore[union-attr]
        s.commit()
    assert orch.recover_on_startup() == [tid]
    with factory() as s:
        assert s.get(OutreachDraftRow, "o1").status == "DELIVERY_UNKNOWN"  # type: ignore[union-attr]


async def test_submit_happy_path_confirmed(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)
    adapter = SimulatedAdapter()
    orch = make_orch(factory, clock, adapter=adapter)
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    assert await orch.run_once() is True
    with factory() as s:
        app = get_application(s, app_id)
        assert app.status == "CONFIRMED"
        assert app.idempotency_key and app.confirmation_reference == (
            "SIM-" + app.idempotency_key.split(":", 1)[-1][:8])
        tos = [e.redacted_details_json for e in list_events(s, app_id)
               if e.action == "status_changed"]
    assert any('"SUBMITTED"' in t and '"to"' in t for t in tos)
    assert "submit_started" in actions(factory, app_id)
    assert adapter.submit_calls == 1 and adapter.real_submissions == 0
    assert task(factory, tid).status == TaskStatus.SUCCEEDED


async def test_submit_write_ahead_committed_before_adapter(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)
    seen: list[str] = []

    class Spy(SimulatedAdapter):
        async def submit(self, approved_application):  # type: ignore[no-untyped-def]
            with factory() as s:  # separate session sees only committed data
                seen.append(get_application(s, app_id).status)
            return await super().submit(approved_application)

    orch = make_orch(factory, clock, adapter=Spy())
    orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_once()
    assert seen == ["SUBMISSION_IN_PROGRESS"]


async def test_submit_timeout_not_retried(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)
    adapter = SimulatedAdapter(fail_mode="timeout")
    orch = make_orch(factory, clock, adapter=adapter)
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_until_idle()
    clock.advance(3600)
    await orch.run_until_idle()
    with factory() as s:
        assert get_application(s, app_id).status == "SUBMISSION_UNCERTAIN"
        evs = [e for e in list_events(s, app_id) if e.error_code == "SUBMIT_TIMEOUT"]
    assert evs
    assert adapter.submit_calls == 1
    assert task(factory, tid).status == TaskStatus.FAILED


async def test_submit_blocked_without_approval(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory, approve=False)
    adapter = SimulatedAdapter()
    orch = make_orch(factory, clock, adapter=adapter)
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_once()
    with factory() as s:
        assert get_application(s, app_id).status == "READY_FOR_REVIEW"
        ev = [e for e in list_events(s, app_id) if e.action == "gate_denied"]
    assert ev and "not_approved" in ev[0].redacted_details_json
    assert adapter.submit_calls == 0
    assert task(factory, tid).status == TaskStatus.FAILED


async def test_outreach_send_simulated(factory) -> None:
    clock = FakeClock()
    with factory() as s:
        s.add(ContactCandidateRow(contact_id="c1", company="Acme", name="Pat",
                                  public_source_url="https://acme.example/team",
                                  confidence=0.9))
        s.add(OutreachDraftRow(outreach_id="o1", company_key="acme", campaign_id="c1",
                               contact_id="c1", channel="email", message_text="hi",
                               status="NEEDS_REVIEW"))
        s.flush()
        svc = ApprovalService(s)
        svc.approve_outreach("o1", svc.outreach_hash("o1"))
        s.commit()
    orch = make_orch(factory, clock)
    tid = orch.enqueue(TaskKind.OUTREACH_SEND, "outreach", "o1")
    await orch.run_once()
    with factory() as s:
        d = s.get(OutreachDraftRow, "o1")
        assert d is not None and d.status == "SENT" and d.evidence_ref == "simulated"
    assert task(factory, tid).status == TaskStatus.SUCCEEDED


async def test_pause_stops_new_work(factory) -> None:
    clock = FakeClock()
    job_id = seed_job(factory)
    orch = make_orch(factory, clock, {TaskKind.MATCH: StubAgent(good_output)},
                     {TaskKind.MATCH: lambda *a: []})
    orch.enqueue(TaskKind.MATCH, "job", job_id)
    orch.pause()
    assert await orch.run_once() is False
    # pause survives restart (new orchestrator on same DB)
    orch2 = make_orch(factory, clock, {TaskKind.MATCH: StubAgent(good_output)},
                      {TaskKind.MATCH: lambda *a: []})
    assert await orch2.run_once() is False
    orch2.resume()
    assert await orch2.run_once() is True


async def test_stop_cancels_queued_returns_inflight(factory) -> None:
    clock = FakeClock()
    orch = make_orch(factory, clock)
    q = orch.enqueue(TaskKind.MATCH, "job", "j1")
    r = orch.enqueue(TaskKind.MATCH, "job", "j2")
    with factory() as s:
        t = s.get(TaskRow, r)
        t.status, t.lease_until = TaskStatus.RUNNING.value, T0 + timedelta(seconds=60)  # type: ignore[union-attr]
        s.commit()
    assert orch.stop() == [r]
    assert task(factory, q).status == TaskStatus.CANCELLED
    assert await orch.run_once() is False


async def test_run_until_idle_counts_steps(factory) -> None:
    clock = FakeClock()
    orch = make_orch(factory, clock, {TaskKind.MATCH: StubAgent(good_output)},
                     {TaskKind.MATCH: lambda *a: []})
    for _ in range(3):
        orch.enqueue(TaskKind.MATCH, "job", new_id("job"))
    assert await orch.run_until_idle() == 3


async def test_submit_adapter_runtime_error_marks_uncertain(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)

    class Broken(SimulatedAdapter):
        async def submit(self, approved_application):  # type: ignore[no-untyped-def]
            self.submit_calls += 1
            raise RuntimeError("token=abc123")

    adapter = Broken()
    orch = make_orch(factory, clock, adapter=adapter)
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_until_idle()
    clock.advance(3600)
    await orch.run_until_idle()
    with factory() as s:
        assert get_application(s, app_id).status == "SUBMISSION_UNCERTAIN"
        ev = [e for e in list_events(s, app_id) if e.action == "submit_error"]
    assert ev and ev[0].error_code == "SUBMIT_NO_CONFIRMATION"
    assert "abc123" not in ev[0].redacted_details_json
    assert adapter.submit_calls == 1
    assert task(factory, tid).status == TaskStatus.FAILED


async def test_recovery_sweeps_stranded_in_progress(factory) -> None:
    from jobpilot.contracts.enums import ApplicationStatus
    from jobpilot.orchestrator.state_machine import transition

    clock = FakeClock()
    app_id = seed_approved_app(factory)
    with factory() as s:
        transition(s, get_application(s, app_id), ApplicationStatus.SUBMISSION_IN_PROGRESS,
                   actor="system", reason="test")
        s.add(OutreachDraftRow(outreach_id="o2", company_key="acme", campaign_id="c2",
                               message_text="hi", status="SEND_IN_PROGRESS"))
        s.commit()
    make_orch(factory, clock).recover_on_startup()
    with factory() as s:
        assert get_application(s, app_id).status == "SUBMISSION_UNCERTAIN"
        assert s.get(OutreachDraftRow, "o2").status == "DELIVERY_UNKNOWN"  # type: ignore[union-attr]


async def test_outreach_send_error_marks_delivery_unknown(factory, monkeypatch) -> None:
    clock = FakeClock()
    with factory() as s:
        s.add(OutreachDraftRow(outreach_id="o1", company_key="acme", campaign_id="c1",
                               channel="email", message_text="hi", status="NEEDS_REVIEW"))
        s.flush()
        svc = ApprovalService(s)
        svc.approve_outreach("o1", svc.outreach_hash("o1"))
        s.commit()
    orch = make_orch(factory, clock)

    def boom(s: Session, d: OutreachDraftRow) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(orch, "_simulated_send", boom)
    tid = orch.enqueue(TaskKind.OUTREACH_SEND, "outreach", "o1")
    await orch.run_once()
    with factory() as s:
        assert s.get(OutreachDraftRow, "o1").status == "DELIVERY_UNKNOWN"  # type: ignore[union-attr]
        ev = [e for e in list_events(s, "o1") if e.action == "send_error"]
    assert ev and ev[0].error_code == "OUTREACH_DELIVERY_UNKNOWN"
    assert task(factory, tid).status == TaskStatus.FAILED


async def test_dry_run_rejects_non_simulated_adapter(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)
    calls: list[str] = []

    class RealAdapter:
        name = "simulated"

        async def capabilities(self):  # type: ignore[no-untyped-def]
            calls.append("capabilities")
            return frozenset()

        async def submit(self, approved_application):  # type: ignore[no-untyped-def]
            calls.append("submit")

        async def find_existing_submission(self, key):  # type: ignore[no-untyped-def]
            calls.append("find")

    orch = Orchestrator(factory, cfg(), {}, {"simulated": RealAdapter()},  # type: ignore[dict-item]
                        clock=clock)
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_once()
    assert calls == []
    with factory() as s:
        assert get_application(s, app_id).status == "APPROVED"
        ev = [e for e in list_events(s, app_id) if e.action == "gate_denied"]
    assert ev and "dry_run" in ev[0].redacted_details_json
    assert task(factory, tid).status == TaskStatus.FAILED


async def test_second_submit_task_gate_denied(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)
    adapter = SimulatedAdapter()
    orch = make_orch(factory, clock, adapter=adapter)
    orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    t2 = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_until_idle()
    assert adapter.submit_calls == 1
    with factory() as s:
        ev = [e for e in list_events(s, app_id) if e.action == "gate_denied"]
    assert ev and "wrong_status" in ev[0].redacted_details_json
    assert task(factory, t2).status == TaskStatus.FAILED


async def test_adapter_timeout_marks_uncertain(factory) -> None:
    import asyncio
    clock = FakeClock()
    app_id = seed_approved_app(factory)

    class Slow(SimulatedAdapter):
        async def submit(self, approved_application):  # type: ignore[no-untyped-def]
            self.submit_calls += 1
            await asyncio.sleep(5)
            return await super().submit(approved_application)

    adapter = Slow()
    orch = make_orch(factory, clock, adapter=adapter, adapter_timeout_s=0.05)
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_until_idle()
    with factory() as s:
        assert get_application(s, app_id).status == "SUBMISSION_UNCERTAIN"
        assert [e for e in list_events(s, app_id) if e.error_code == "SUBMIT_TIMEOUT"]
    assert task(factory, tid).status == TaskStatus.FAILED


async def test_unexpected_submit_status_marks_uncertain(factory) -> None:
    from jobpilot.contracts.models import SubmitResult
    clock = FakeClock()
    app_id = seed_approved_app(factory)

    class Weird(SimulatedAdapter):
        async def submit(self, approved_application):  # type: ignore[no-untyped-def]
            self.submit_calls += 1
            return SubmitResult.model_construct(
                status="IN_PROGRESS", confirmation_reference="x", evidence_url=None)

    orch = make_orch(factory, clock, adapter=Weird())
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_until_idle()
    with factory() as s:
        assert get_application(s, app_id).status == "SUBMISSION_UNCERTAIN"
    assert task(factory, tid).status == TaskStatus.FAILED


async def test_outreach_send_live_mode_gate_denied(factory) -> None:
    clock = FakeClock()
    with factory() as s:
        s.add(ContactCandidateRow(contact_id="c1", company="Acme", name="Pat",
                                  public_source_url="https://acme.example/team",
                                  confidence=0.9))
        s.add(OutreachDraftRow(outreach_id="o1", company_key="acme", campaign_id="c1",
                               contact_id="c1", channel="email", message_text="hi",
                               status="NEEDS_REVIEW"))
        s.flush()
        svc = ApprovalService(s)
        svc.approve_outreach("o1", svc.outreach_hash("o1"))
        s.commit()
    orch = Orchestrator(factory, cfg(dry_run=False), {}, {"simulated": SimulatedAdapter()},
                        clock=clock)
    tid = orch.enqueue(TaskKind.OUTREACH_SEND, "outreach", "o1")
    await orch.run_once()
    with factory() as s:
        d = s.get(OutreachDraftRow, "o1")
        assert d is not None and d.status == "APPROVED" and d.evidence_ref is None
        assert "send_started" not in [e.action for e in list_events(s, "o1")]
    assert task(factory, tid).status == TaskStatus.FAILED


def _record_click(factory: sessionmaker[Session], app_id: str, key: str) -> None:
    from jobpilot.portals.live_submit import make_before_click
    with factory() as s:
        get_application(s, app_id).idempotency_key = key
        s.commit()
    make_before_click(factory)(app_id, key)


@pytest.mark.parametrize("clicked", [True, False])
async def test_restart_with_inflight_live_submit_uncertain(factory, clicked: bool) -> None:
    """Crash after (or before: unprovable) the click: UNCERTAIN, never re-run."""
    from jobpilot.contracts.enums import ApplicationStatus
    from jobpilot.orchestrator.state_machine import transition

    clock = FakeClock()
    app_id = seed_approved_app(factory)
    adapter = SimulatedAdapter()
    orch = make_orch(factory, clock, adapter=adapter)
    tid = orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    with factory() as s:
        transition(s, get_application(s, app_id), ApplicationStatus.SUBMISSION_IN_PROGRESS,
                   actor="system", reason="test")
        t = s.get(TaskRow, tid)
        t.status, t.lease_until = TaskStatus.RUNNING.value, T0 - timedelta(seconds=1)  # type: ignore[union-attr]
        s.commit()
    if clicked:
        _record_click(factory, app_id, "key-live-1")
    assert orch.recover_on_startup() == [tid]
    await orch.run_until_idle()
    with factory() as s:
        assert get_application(s, app_id).status == "SUBMISSION_UNCERTAIN"
        submits = s.execute(select(TaskRow).where(TaskRow.kind == "SUBMIT")).scalars().all()
    assert [t.task_id for t in submits] == [tid]
    assert task(factory, tid).status == TaskStatus.FAILED
    assert adapter.submit_calls == 0


async def test_orphan_sweep_skips_clicked(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)
    _record_click(factory, app_id, "key-live-2")
    orch = make_orch(factory, clock)
    orch.recover_on_startup()
    with factory() as s:
        assert s.execute(select(TaskRow).where(TaskRow.kind == "SUBMIT")).first() is None
        assert get_application(s, app_id).status == "APPROVED"


class _ClickThenFail(SimulatedAdapter):
    """Records the click like LiveSubmitter, then reports a FAILED page."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        super().__init__()
        self._factory = factory

    async def submit(self, approved_application: Any) -> Any:
        from jobpilot.contracts.enums import ApplicationStatus
        from jobpilot.contracts.models import SubmitResult
        from jobpilot.portals.live_submit import make_before_click
        self.submit_calls += 1
        make_before_click(self._factory)(approved_application.application_id,
                                         approved_application.idempotency_key)
        return SubmitResult(task_id=approved_application.idempotency_key, created_at=T0,
                            application_id=approved_application.application_id,
                            status=ApplicationStatus.SUBMISSION_FAILED,
                            retry_allowed=False, next_action="none")


async def test_failed_result_coerced_after_click(factory) -> None:
    clock = FakeClock()
    app_id = seed_approved_app(factory)
    adapter = _ClickThenFail(factory)
    orch = make_orch(factory, clock, adapter=adapter)
    orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_until_idle()
    with factory() as s:
        assert get_application(s, app_id).status == "SUBMISSION_UNCERTAIN"
        reasons = [e.redacted_details_json for e in list_events(s, app_id)
                   if e.action == "status_changed"]
    assert adapter.submit_calls == 1
    assert any("failed_coerced_after_click" in r for r in reasons)


async def test_lease_kind_filter(factory) -> None:
    clock = FakeClock()
    job_id = seed_job(factory)
    match_agent, tailor_agent = StubAgent(good_output), StubAgent(good_output)
    orch = make_orch(factory, clock, {TaskKind.MATCH: match_agent,
                                      TaskKind.TAILOR: tailor_agent})
    tailor = orch.enqueue(TaskKind.TAILOR, "job", job_id)
    match = orch.enqueue(TaskKind.MATCH, "job", job_id)
    assert await orch.run_until_idle(kinds={TaskKind.MATCH}) == 1
    assert task(factory, match).status == TaskStatus.SUCCEEDED
    assert task(factory, tailor).status == TaskStatus.QUEUED
    assert tailor_agent.calls == 0
    assert await orch.run_until_idle() == 1  # default None leases every kind
    assert task(factory, tailor).status == TaskStatus.SUCCEEDED
