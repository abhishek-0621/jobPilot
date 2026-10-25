"""Plan 8 Task 3: OUTREACH_RESEARCH task, flow wiring, refusal of sends, deletion/retention."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents.outreach import OutreachAgent, make_outreach_handler, make_outreach_loader
from jobpilot.api.actions import (
    ActionConflict,
    mark_applied,
    request_outreach,
    resolve_uncertain,
)
from jobpilot.config import AppConfig, OutreachConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import (
    ApplicationRow,
    AuditEventRow,
    CandidateFactRow,
    ContactCandidateRow,
    JobRow,
    OutreachDraftRow,
    TaskRow,
)
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.ops import purge
from jobpilot.ops.export import export_application
from jobpilot.ops.maintenance import run_maintenance
from jobpilot.orchestrator.engine import Orchestrator, TaskKind, TaskStatus, add_task
from jobpilot.orchestrator.policies import OutreachSendDisabled
from jobpilot.outreach.contacts import FoundContact
from jobpilot.portals.simulated import SimulatedAdapter

FACTS = [{"fact_id": "fact_exp_001", "category": "experience", "field": "bullet",
          "value": "Built a retrieval pipeline serving 10k queries per day"}]


class _Identity:
    full_name = "Alex Example"


class _Profile:
    identity = _Identity()


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    storage = tmp_path / "storage"
    storage.mkdir()
    return AppConfig(storage_path=storage, db_path=storage / "jobpilot.db", model_routes={})


@pytest.fixture
def factory(cfg: AppConfig) -> Iterator[sessionmaker[Session]]:
    upgrade_to_head(cfg.db_path)
    engine = make_engine(cfg.db_path)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(CandidateFactRow(fact_id="fact_exp_001", profile_version=1,
                               category="experience", field="bullet",
                               value=FACTS[0]["value"], verified_by_user=True))
        s.commit()
    yield sf
    engine.dispose()


def _orch(factory: sessionmaker[Session], cfg: AppConfig,
          careers: list[FoundContact] | None = None) -> Orchestrator:
    async def careers_inbox(url: str | None, domain: str | None) -> list[FoundContact]:
        return list(careers or [])
    tools: dict[str, Any] = {
        "outreach_context": make_outreach_loader(factory, cfg),
        "careers_inbox": careers_inbox,
        "profile_loader": lambda: (1, FACTS, 5.0),
        "profile_file": lambda: _Profile(),
        "tech_terms": lambda: set(),
        "cover_letter_banned": lambda: [],
    }
    return Orchestrator(factory, cfg, {TaskKind.OUTREACH_RESEARCH: OutreachAgent()},
                        {"simulated": SimulatedAdapter()},
                        handlers={TaskKind.OUTREACH_RESEARCH: make_outreach_handler(cfg)},
                        tools=tools)


def seed_app(s: Session, *, company: str = "Acme Robotics", status: str = "SUBMITTED_BY_USER",
             description: str = "Write to careers@acmerobotics.example with questions.",
             url: str = "https://careers.acmerobotics.example/jobs/1") -> str:
    job_id, app_id = new_id("job"), new_id("app")
    s.add(JobRow(job_id=job_id, source="manual", source_job_id=job_id, canonical_url=url,
                 company=company, title="Backend Engineer", location="Remote",
                 work_mode="remote", description_text=description, extracted_json="{}",
                 status="MATCHED", dedupe_key=job_id, first_seen_at=datetime.now(UTC),
                 last_seen_at=datetime.now(UTC)))
    s.flush()
    s.add(ApplicationRow(application_id=app_id, job_id=job_id, status=status))
    s.flush()
    if status == "SUBMITTED_BY_USER":
        record_event(s, entity_type="application", entity_id=app_id, actor="user",
                     action="user_marked_applied", outcome="ok",
                     details={"application_id": app_id, "applied_on": "2026-10-20"})
    s.commit()
    return app_id


def _drafts(s: Session) -> list[OutreachDraftRow]:
    return list(s.execute(select(OutreachDraftRow)).scalars())


async def test_research_creates_draft_for_review(factory, cfg) -> None:
    with factory() as s:
        app_id = seed_app(s)
    orch = _orch(factory, cfg)
    with factory() as s:
        request_outreach(s, orch, app_id, cfg)
    await orch.run_until_idle()
    with factory() as s:
        (d,) = _drafts(s)
        assert d.status == "NEEDS_REVIEW" and d.application_id == app_id
        assert d.company_key == "acmerobotics" and d.campaign_id == "default"
        assert d.channel == "email" and "20 October 2026" in d.message_text
        c = s.get(ContactCandidateRow, d.contact_id)
        assert c is not None and c.address == "careers@acmerobotics.example"
        assert c.application_id == app_id and c.created_at is not None
        assert c.verified_at is None  # only user-added contacts are verified
        steps = [e.redacted_details_json for e in s.execute(select(AuditEventRow).where(
            AuditEventRow.entity_id == d.outreach_id,
            AuditEventRow.action == "status_changed")).scalars()]
        assert [x.split('"to":"')[1].split('"')[0] for x in steps] == [
            "CONTACT_RESEARCH", "DRAFT_READY", "NEEDS_REVIEW"]
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "outreach_drafted")).scalars().one()
        assert "careers@" not in ev.redacted_details_json  # ids/counts only
        assert "retrieval" not in ev.redacted_details_json


async def test_one_draft_per_company_campaign(factory, cfg) -> None:
    with factory() as s:
        a1 = seed_app(s)
        a2 = seed_app(s, company="ACME Robotics")
    orch = _orch(factory, cfg)
    with factory() as s:
        request_outreach(s, orch, a1, cfg)
        # both queued before either ran: the handler is the backstop
        add_task(s, TaskKind.OUTREACH_RESEARCH, "application", a2)
        s.commit()
    await orch.run_until_idle()
    with factory() as s:
        assert len(_drafts(s)) == 1
        dup = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "outreach_duplicate_company")).scalars().one()
        assert dup.entity_id == a2
        with pytest.raises(ActionConflict):
            request_outreach(s, orch, a2, cfg)
        # the DB constraint holds too
        s.add(OutreachDraftRow(outreach_id="o_x", company_key="acmerobotics",
                               campaign_id="default", message_text="x", status="NEEDS_REVIEW"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
    # another campaign may draft again
    cfg2 = cfg.model_copy(update={"outreach": OutreachConfig(campaign="spring")})
    orch2 = _orch(factory, cfg2)
    with factory() as s:
        request_outreach(s, orch2, a2, cfg2)
    await orch2.run_until_idle()
    with factory() as s:
        assert sorted(d.campaign_id for d in _drafts(s)) == ["default", "spring"]


async def test_no_contact_skips_with_reason(factory, cfg) -> None:
    with factory() as s:
        app_id = seed_app(s, description="Contact: Sam Lee, Recruiter. No email given. "
                          "Or write to jobs@other-company.example")
    orch = _orch(factory, cfg)
    with factory() as s:
        request_outreach(s, orch, app_id, cfg)
    await orch.run_until_idle()
    with factory() as s:
        (d,) = _drafts(s)
        assert d.status == "SKIPPED" and d.skip_reason == "no_public_contact"
        assert d.contact_id is None and d.message_text  # kept for a user-added contact
        assert s.execute(select(func.count()).select_from(ContactCandidateRow)).scalar_one() \
            == 0


async def test_careers_inbox_used_and_ranked(factory, cfg) -> None:
    with factory() as s:
        app_id = seed_app(s, description="No contact details.")
    inbox = FoundContact(kind="careers_inbox", name="Careers inbox", professional_title=None,
                         channel="email", address="recruiting@acmerobotics.example",
                         public_source_url="https://acmerobotics.example/careers",
                         confidence=0.7)
    orch = _orch(factory, cfg, careers=[inbox])
    with factory() as s:
        request_outreach(s, orch, app_id, cfg)
    await orch.run_until_idle()
    with factory() as s:
        (d,) = _drafts(s)
        c = s.get(ContactCandidateRow, d.contact_id)
        assert c is not None and c.kind == "careers_inbox" and c.confidence == 0.7


async def test_research_only_after_applied(factory, cfg) -> None:
    orch = _orch(factory, cfg)
    with factory() as s:
        ready = seed_app(s, status="MANUAL_APPLY_READY")
        with pytest.raises(ActionConflict):
            request_outreach(s, orch, ready, cfg)
        with pytest.raises(LookupError):
            request_outreach(s, orch, "app_missing", cfg)
        # mark_applied queues research when auto_draft is on
        mark_applied(s, ready, None, date(2026, 10, 21), cfg=cfg)
        assert _research_tasks(s, ready) == 1
        off = cfg.model_copy(update={"outreach": OutreachConfig(auto_draft=False)})
        other = seed_app(s, company="Initech", status="MANUAL_APPLY_READY")
        mark_applied(s, other, None, None, cfg=off)
        assert _research_tasks(s, other) == 0
        # a simulated (dry-run) confirmation never counts as applied
        sim = seed_app(s, company="Hooli", status="CONFIRMED")
        record_event(s, entity_type="application", entity_id=sim, actor="orchestrator",
                     action="submit_started", outcome="ok", details={"simulated": True})
        s.commit()
        with pytest.raises(ActionConflict):
            request_outreach(s, orch, sim, cfg)
        # a live submission resolved as submitted by the user does
        live = seed_app(s, company="Umbrella", status="SUBMISSION_UNCERTAIN")
        record_event(s, entity_type="application", entity_id=live, actor="orchestrator",
                     action="submit_started", outcome="ok", details={"simulated": False})
        s.commit()
        resolve_uncertain(s, live, True, cfg=cfg)
        assert _research_tasks(s, live) == 1
    # the agent re-checks: a non-applied application is rejected, no draft written
    with factory() as s:
        add_task(s, TaskKind.OUTREACH_RESEARCH, "application", sim)
        s.commit()
    await orch.run_until_idle()
    with factory() as s:
        assert {d.application_id for d in _drafts(s)} == {ready, live}


async def test_engine_confirmed_live_submit_queues_research(factory, cfg) -> None:
    from jobpilot.orchestrator.engine import Orchestrator as O

    with factory() as s:
        app_id = seed_app(s, status="SUBMISSION_IN_PROGRESS")
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        from jobpilot.contracts.models import SubmitResult
        r = SubmitResult(task_id="t", created_at=datetime.now(UTC), application_id=app_id,
                         status="CONFIRMED", submitted_at=datetime.now(UTC),
                         confirmation_reference="C-1", retry_allowed=False, next_action="none")
        record_event(s, entity_type="application", entity_id=app_id, actor="orchestrator",
                     action="submit_started", outcome="ok", details={"simulated": False})
        orch = O(factory, cfg, {}, {"simulated": SimulatedAdapter()})
        orch._apply_submit_result(s, app, r, simulated=False)
        s.commit()
        assert app.status == "CONFIRMED" and _research_tasks(s, app_id) == 1
        sim_id = seed_app(s, company="Hooli", status="SUBMISSION_IN_PROGRESS")
        sim = s.get(ApplicationRow, sim_id)
        assert sim is not None
        orch._apply_submit_result(s, sim, r.model_copy(update={"application_id": sim_id}),
                                  simulated=True)
        s.commit()
        assert sim.status == "CONFIRMED" and _research_tasks(s, sim_id) == 0


def _research_tasks(s: Session, app_id: str) -> int:
    return s.execute(select(func.count()).select_from(TaskRow).where(
        TaskRow.kind == "OUTREACH_RESEARCH", TaskRow.entity_id == app_id)).scalar_one()


async def test_outreach_send_refused(factory, cfg) -> None:
    with factory() as s:
        s.add(OutreachDraftRow(outreach_id="o1", company_key="acme", campaign_id="c1",
                               channel="email", message_text="hi", status="APPROVED"))
        s.commit()
    orch = _orch(factory, cfg)
    with pytest.raises(OutreachSendDisabled):
        orch.enqueue(TaskKind.OUTREACH_SEND, "outreach", "o1")
    with factory() as s:
        with pytest.raises(OutreachSendDisabled):
            add_task(s, TaskKind.OUTREACH_SEND, "outreach", "o1")
        s.rollback()
        # a row queued by an older version is refused when run, nothing changes
        s.add(TaskRow(task_id="task_old", kind="OUTREACH_SEND", entity_type="outreach",
                      entity_id="o1", status="QUEUED", attempts=0))
        s.commit()
    await orch.run_until_idle()
    with factory() as s:
        d = s.get(OutreachDraftRow, "o1")
        assert d is not None and d.status == "APPROVED" and d.sent_at is None
        t = s.get(TaskRow, "task_old")
        assert t is not None and t.status == TaskStatus.FAILED.value
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "gate_denied")).scalars().one()
        assert "outreach_send_disabled" in ev.redacted_details_json


async def _drafted(factory, cfg) -> tuple[str, str]:
    with factory() as s:
        app_id = seed_app(s)
    orch = _orch(factory, cfg)
    with factory() as s:
        request_outreach(s, orch, app_id, cfg)
    await orch.run_until_idle()
    with factory() as s:
        (d,) = _drafts(s)
        return app_id, d.outreach_id


async def test_delete_app_removes_contacts_and_drafts(factory, cfg) -> None:
    app_id, outreach_id = await _drafted(factory, cfg)
    with factory() as s:
        purge.delete_application(cfg, s, app_id, confirm=app_id)
    with factory() as s:
        assert _drafts(s) == []
        assert s.execute(select(func.count()).select_from(ContactCandidateRow)).scalar_one() \
            == 0
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "data_deleted")).scalars().one()
        assert '"contacts_n":1' in ev.redacted_details_json
        assert '"drafts_n":1' in ev.redacted_details_json
        # events stay
        assert s.execute(select(func.count()).select_from(AuditEventRow).where(
            AuditEventRow.entity_id == outreach_id)).scalar_one() >= 3


async def test_contact_retention_prunes(factory, cfg) -> None:
    app_id, outreach_id = await _drafted(factory, cfg)
    with factory() as s:
        fresh_app = seed_app(s, company="Initech")
        s.add(ContactCandidateRow(contact_id="con_fresh", company="Initech", name="x",
                                  public_source_url="https://initech.example/careers",
                                  confidence=0.7, application_id=fresh_app))
        old = datetime.now(UTC) - timedelta(days=cfg.retention_days.contacts + 1)
        for c in s.execute(select(ContactCandidateRow).where(
                ContactCandidateRow.application_id == app_id)).scalars():
            c.created_at = old
        s.commit()
    counts = run_maintenance(cfg)
    assert counts["contacts_pruned"] == 1
    with factory() as s:
        assert s.get(ContactCandidateRow, "con_fresh") is not None
        d = s.get(OutreachDraftRow, outreach_id)
        assert d is not None and d.contact_id is None  # unbound, the draft row stays


async def test_export_includes_outreach(factory, cfg) -> None:
    import json
    import zipfile

    app_id, outreach_id = await _drafted(factory, cfg)
    with factory() as s:
        path = export_application(cfg, s, app_id)
    with zipfile.ZipFile(path) as z:
        data = json.loads(z.read("application.json"))
    (o,) = data["outreach"]
    assert o["id"] == outreach_id and o["status"] == "NEEDS_REVIEW" and o["message_text"]
    assert o["contact"]["address"] == "careers@acmerobotics.example"
    assert data["contacts"][0]["kind"] == "posting_inbox"
