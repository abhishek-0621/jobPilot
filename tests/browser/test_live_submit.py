"""Live submission on local fixture portals: verify, click once, read the confirmation.
Every test asserts the exact number of POSTs the fixture server received."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import AppConfig, ModelRoute
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import (
    ApplicationRow,
    AuditEventRow,
    CandidateProfileRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    TaskRow,
)
from jobpilot.documents import store
from jobpilot.documents.render import chromium_available
from jobpilot.forms.browser import portal_page
from jobpilot.observability.events import EVENT_ORDER
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator, TaskKind
from jobpilot.portals.ashby import AshbyAdapter
from jobpilot.portals.greenhouse import GreenhouseAdapter
from jobpilot.portals.lever import LeverAdapter
from jobpilot.portals.live_submit import LiveSubmitter, SubmitContext, make_before_click
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.profile.importer import PROFILE_ID
from jobpilot.runtime import make_submit_loader, make_submitter_factory
from jobpilot.util.hashing import canonical_json
from tests.factories import make_app_row

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium not installed"),
]

Portal = Literal["greenhouse", "lever", "ashby"]
_ADAPTERS = {"greenhouse": GreenhouseAdapter, "lever": LeverAdapter, "ashby": AshbyAdapter}
_IDENTITY_IDS = {"ashby": ("_systemfield_name", "_systemfield_email", "_systemfield_resume")}


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    upgrade_to_head(tmp_path / "live.db")
    engine = make_engine(tmp_path / "live.db")
    yield make_session_factory(engine)
    engine.dispose()


def live_cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "storage", db_path=tmp_path / "live.db",
                     dry_run=False, live_portals=["greenhouse", "lever", "ashby"],
                     browser_headless=True, model_routes={"a": ModelRoute(model_tag="m1")})


async def _snapshot_hash(cfg: AppConfig, portal: Portal, url: str) -> str:
    cls = _ADAPTERS[portal]
    async with portal_page(cfg, portal=portal, allowed_hosts={"127.0.0.1"}) as session:
        adapter = cls(session.page, upload_root=cfg.storage_path)
        await adapter.open_job(url)
        return adapter.snapshot_hash(await adapter.inspect_form())


async def seed_approved(factory: sessionmaker[Session], cfg: AppConfig, url: str,
                        portal: Portal = "greenhouse", *,
                        extra_answers: tuple[str, ...] = ()) -> str:
    """An APPROVED application whose stored answers fit the *_submit.html fixtures."""
    snap = await _snapshot_hash(cfg, portal, url)
    with factory() as s:
        s.add(CandidateProfileRow(profile_id=PROFILE_ID, version=1))
        app = make_app_row(s, status="READY_FOR_REVIEW")
        job = s.get(JobRow, app.job_id)
        assert job is not None
        job.application_url, job.source, app.portal = url, portal, portal
        app.submit_live_intent = portal  # stands in for the typed SUBMIT request
        art_dir = cfg.storage_path / "artifacts" / app.application_id
        art_dir.mkdir(parents=True)
        resume = art_dir / "resume_v1.pdf"
        resume.write_bytes(b"%PDF-1.4 synthetic test resume")
        app.resume_artifact_id = store.write_artifact(
            s, app.application_id, "resume", resume, [], root=cfg.storage_path,
            profile_version=1).artifact_id
        name_id, email_id, resume_id = _IDENTITY_IDS.get(
            portal, ("first_name", "email", "resume"))
        for fid, ftype, value, source in [
                (name_id, "text", "Testy", "fact_identity_first"),
                (email_id, "email", "candidate@example.com", "fact_identity_email"),
                (resume_id, "file", str(resume), "artifact:resume"),
                *[(f, "text", "x", "fact_x") for f in extra_answers]]:
            s.add(FormFieldAnswerRow(
                application_id=app.application_id, field_id=fid, label=fid,
                field_type=ftype, proposed_value_json=canonical_json(value),
                decision="AUTO_FILL", source_fact_id=source, confidence=0.9,
                rationale="test", required=True))
        app.form_snapshot_hash = snap
        s.flush()
        svc = ApprovalService(s)
        batch = svc.build_batch([app.application_id])
        svc.approve(batch.batch_id, 1, batch.content_hash or "", [app.application_id])
        s.commit()
        return app.application_id


async def run_submit(factory: sessionmaker[Session], cfg: AppConfig, app_id: str,
                     submitter_factory: object | None = None) -> Orchestrator:
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()},
                        submitter_factory=submitter_factory  # type: ignore[arg-type]
                        or make_submitter_factory(cfg, factory))
    orch.enqueue(TaskKind.SUBMIT, "application", app_id)
    await orch.run_until_idle()
    return orch


def _state(factory: sessionmaker[Session], app_id: str) -> tuple[ApplicationRow, TaskRow,
                                                                 list[AuditEventRow]]:
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        task = s.execute(select(TaskRow).where(TaskRow.kind == "SUBMIT")).scalar_one()
        events = list(s.execute(select(AuditEventRow).order_by(
            EVENT_ORDER)).scalars())
        assert app is not None
        s.expunge_all()
        return app, task, events


def _clicks(events: list[AuditEventRow]) -> int:
    return sum(e.action == "submit_clicked" for e in events)


def _assert_no_click(factory: sessionmaker[Session], app_id: str, code: str,
                     status: str = "READY_FOR_REVIEW") -> ApplicationRow:
    app, task, events = _state(factory, app_id)
    assert app.status == status
    assert task.status == "FAILED"
    assert _clicks(events) == 0
    failed = [json.loads(e.redacted_details_json) for e in events
              if e.action == "presubmit_failed"]
    assert [f["code"] for f in failed] == [code]
    assert any(e.action == "approval_item_revoked" for e in events)
    with factory() as s:
        assert ApprovalService(s).active_approval_for(app_id) is None
        assert s.execute(select(TaskRow).where(TaskRow.status == "QUEUED",
                                               TaskRow.kind == "SUBMIT")).first() is None
    return app


@pytest.mark.parametrize("portal", ["greenhouse", "lever", "ashby"])
async def test_live_submit_confirmed(factory: sessionmaker[Session], tmp_path: Path,
                                     form_server: str, form_posts: list[str],
                                     portal: Portal) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/{portal}_submit.html", portal)
    await run_submit(factory, cfg, app_id)
    assert form_posts == ["/submit_ok"]
    app, task, events = _state(factory, app_id)
    assert app.status == "CONFIRMED" and task.status == "SUCCEEDED"
    assert app.confirmation_reference == "APP-12345"
    assert _clicks(events) == 1
    with factory() as s:
        shot = s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.type == "submit_confirmation")).scalar_one()
    assert shot.path.endswith("submit_v1.png")
    assert (cfg.storage_path / shot.path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


async def test_presubmit_mismatch_never_clicks(factory: sessionmaker[Session], tmp_path: Path,
                                               form_server: str,
                                               form_posts: list[str]) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/greenhouse_submit.html")
    real = make_submit_loader(factory, cfg.storage_path)
    calls = 0

    def editing_loader(application_id: str) -> SubmitContext | None:
        nonlocal calls
        ctx = real(application_id)
        calls += 1
        if calls == 1:  # the answer changes behind the approval's back mid-submit
            with factory() as s:
                row = s.get(FormFieldAnswerRow, (application_id, "first_name"))
                assert row is not None
                row.proposed_value_json = canonical_json("Edited")
                s.commit()
        return ctx

    def submitter(portal: str) -> LiveSubmitter:
        return LiveSubmitter(cfg, portal, editing_loader, make_before_click(factory), factory)

    await run_submit(factory, cfg, app_id, submitter)
    assert form_posts == []
    _assert_no_click(factory, app_id, "HASH_MISMATCH")


async def test_layout_change_never_clicks(factory: sessionmaker[Session], tmp_path: Path,
                                          form_server: str, form_posts: list[str]) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/greenhouse_submit.html")
    with factory() as s:  # the form read at prepare time no longer matches the page
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        app.form_snapshot_hash = "sha256:" + "0" * 64
        s.commit()
    await run_submit(factory, cfg, app_id)
    assert form_posts == []
    app = _assert_no_click(factory, app_id, "FORM_LAYOUT_CHANGED", "NEEDS_USER_INPUT")
    assert app.form_snapshot_hash is None


async def test_value_mismatch_back_to_user(factory: sessionmaker[Session], tmp_path: Path,
                                           form_server: str, form_posts: list[str]) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/greenhouse_submit.html",
                                 extra_answers=("gone_field",))  # not on the page
    await run_submit(factory, cfg, app_id)
    assert form_posts == []
    app = _assert_no_click(factory, app_id, "VALUE_MISMATCH", "NEEDS_USER_INPUT")
    assert app.form_snapshot_hash is None


async def test_stale_documents_never_clicks(factory: sessionmaker[Session], tmp_path: Path,
                                            form_server: str, form_posts: list[str]) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/greenhouse_submit.html")
    with factory() as s:  # profile re-imported after approval
        s.add(CandidateProfileRow(profile_id=PROFILE_ID, version=2))
        s.commit()
    await run_submit(factory, cfg, app_id)
    assert form_posts == []
    _assert_no_click(factory, app_id, "STALE_DOCUMENTS", "DOCUMENTS_PENDING")
    _app, _task, events = _state(factory, app_id)
    tos = [json.loads(e.redacted_details_json).get("to") for e in events
           if e.action == "status_changed" and e.entity_id == app_id]
    assert tos[-2:] == ["READY_FOR_REVIEW", "DOCUMENTS_PENDING"]
    assert any(e.action == "documents_stale" for e in events)
    with factory() as s:
        # TAILOR was queued (this test's orchestrator has no agents to run it)
        assert s.execute(select(TaskRow).where(TaskRow.kind == "TAILOR")).first()


async def test_ambiguous_submit_control(factory: sessionmaker[Session], tmp_path: Path,
                                        form_server: str, form_posts: list[str]) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/two_submits.html")
    await run_submit(factory, cfg, app_id)
    assert form_posts == []
    app, task, events = _state(factory, app_id)
    assert app.status == "NEEDS_USER_INPUT" and task.status == "FAILED"
    assert _clicks(events) == 0
    reasons = [json.loads(e.redacted_details_json).get("reason") for e in events
               if e.action == "status_changed" and e.entity_id == app_id]
    assert reasons[-1] == "manual_handoff:SUBMIT_CONTROL_AMBIGUOUS"


async def test_blocker_at_submit_handoff(factory: sessionmaker[Session], tmp_path: Path,
                                         form_server: str, form_posts: list[str]) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/greenhouse_submit_captcha.html")
    await run_submit(factory, cfg, app_id)
    assert form_posts == []
    app, task, events = _state(factory, app_id)
    assert app.status == "NEEDS_USER_INPUT" and task.status == "FAILED"
    assert app.form_snapshot_hash is None
    assert _clicks(events) == 0
    reasons = [json.loads(e.redacted_details_json).get("reason") for e in events
               if e.action == "status_changed" and e.entity_id == app_id]
    assert reasons[-1] == "manual_handoff:BLOCKED:captcha"


async def test_validation_error_page_uncertain_with_evidence(
        factory: sessionmaker[Session], tmp_path: Path, form_server: str,
        form_posts: list[str]) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/greenhouse_submit_error.html")
    await run_submit(factory, cfg, app_id, _quick(cfg, factory))
    assert form_posts == ["/submit_error"]
    app, _task, events = _state(factory, app_id)
    assert app.status == "SUBMISSION_UNCERTAIN"  # never auto-failed: a retry could duplicate
    finished = [json.loads(e.redacted_details_json) for e in events
                if e.action == "submit_finished"]
    assert finished and finished[-1]["error_codes"] == ["PAGE_ERRORS:2"]
    assert app.confirmation_reference is None
    assert _clicks(events) == 1


async def test_no_confirmation_is_uncertain(factory: sessionmaker[Session], tmp_path: Path,
                                            form_server: str, form_posts: list[str]) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg,
                                 f"{form_server}/greenhouse_submit_noconfirm.html")
    loader = make_submit_loader(factory, cfg.storage_path)

    def submitter(portal: str) -> LiveSubmitter:
        return LiveSubmitter(cfg, portal, loader, make_before_click(factory), factory,
                             confirm_timeout_s=1)

    await run_submit(factory, cfg, app_id, submitter)
    assert form_posts == ["/submit_noconfirm"]
    app, _task, events = _state(factory, app_id)
    assert app.status == "SUBMISSION_UNCERTAIN" and _clicks(events) == 1


def _quick(cfg: AppConfig, factory: sessionmaker[Session]) -> object:
    loader = make_submit_loader(factory, cfg.storage_path)

    def submitter(portal: str) -> LiveSubmitter:
        return LiveSubmitter(cfg, portal, loader, make_before_click(factory), factory,
                             confirm_timeout_s=1)
    return submitter


@pytest.mark.parametrize("page", [
    "jobs/submitted-roles/1?utm=thankyou&x=success",  # words only in path part / query
    "greenhouse_submit_static.html",  # confirmation phrases in the page before the click
])
async def test_preexisting_evidence_is_not_confirmation(
        factory: sessionmaker[Session], tmp_path: Path, form_server: str,
        form_posts: list[str], page: str) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/{page}")
    await run_submit(factory, cfg, app_id, _quick(cfg, factory))
    assert form_posts == ["/submit_noop"]
    app, _task, events = _state(factory, app_id)
    assert app.status == "SUBMISSION_UNCERTAIN" and _clicks(events) == 1
    assert app.confirmation_reference is None


@pytest.mark.parametrize(("page", "post", "status"), [
    ("greenhouse_submit_errbox.html", "/submit_keep", "SUBMISSION_UNCERTAIN"),
    ("greenhouse_submit_errbox_ok.html", "/submit_thanks", "CONFIRMED"),
])
async def test_preexisting_error_box_is_not_failure(
        factory: sessionmaker[Session], tmp_path: Path, form_server: str,
        form_posts: list[str], page: str, post: str, status: str) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/{page}")
    await run_submit(factory, cfg, app_id, _quick(cfg, factory))
    assert form_posts == [post]
    app, _task, _events = _state(factory, app_id)
    assert app.status == status


@pytest.mark.parametrize(("page", "post"), [
    # static "Thank you for your interest" h1; the form renders late after the no-op POST
    ("greenhouse_submit_interest.html", "/submit_noop"),
    # redirect to a form-less listing whose h1 says "submitted"
    ("greenhouse_submit_listing.html", "/submit_listing"),
    # a pre-existing live error region shows "Saving..." then clears; the page stays
    ("greenhouse_submit_live.html", "/submit_noop"),
    # the no-op POST reloads the form, which then shows a static error text
    ("greenhouse_submit_reload_error.html", "/submit_noop"),
])
async def test_weak_evidence_is_uncertain(
        factory: sessionmaker[Session], tmp_path: Path, form_server: str,
        form_posts: list[str], page: str, post: str) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/{page}")
    loader = make_submit_loader(factory, cfg.storage_path)

    def submitter(portal: str) -> LiveSubmitter:
        return LiveSubmitter(cfg, portal, loader, make_before_click(factory), factory,
                             confirm_timeout_s=2.5)

    await run_submit(factory, cfg, app_id, submitter)
    assert form_posts == [post]
    app, _task, events = _state(factory, app_id)
    assert app.status == "SUBMISSION_UNCERTAIN" and _clicks(events) == 1


def _force_approved(factory: sessionmaker[Session], app_id: str) -> None:
    """Simulate a bug that puts a clicked application back in APPROVED."""
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        app.status = "APPROVED"
        s.commit()


@pytest.mark.parametrize(("confirm_s", "timeout_s"), [
    (1.0, 120.0),  # detect_confirmation gives up: SUBMIT_NO_CONFIRMATION
    (30.0, 8.0),  # the engine's wait_for fires first: SUBMIT_TIMEOUT
])
async def test_timeout_uncertain_no_second_click(
        factory: sessionmaker[Session], tmp_path: Path, form_server: str,
        form_posts: list[str], confirm_s: float, timeout_s: float) -> None:
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg,
                                 f"{form_server}/greenhouse_submit_noconfirm.html")
    loader = make_submit_loader(factory, cfg.storage_path)
    made: list[LiveSubmitter] = []

    def submitter(portal: str) -> LiveSubmitter:
        made.append(LiveSubmitter(cfg, portal, loader, make_before_click(factory), factory,
                                  confirm_timeout_s=confirm_s, timeout_s=timeout_s))
        return made[-1]

    await run_submit(factory, cfg, app_id, submitter)
    assert form_posts == ["/submit_noconfirm"]
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "SUBMISSION_UNCERTAIN"
        key = app.idempotency_key
    assert key is not None
    existing = await made[-1].find_existing_submission(key)
    assert existing is not None and existing.status == "SUBMISSION_UNCERTAIN"

    _force_approved(factory, app_id)
    calls: list[str] = []
    orig = LiveSubmitter.submit

    async def spy(self: LiveSubmitter, a: object) -> object:
        calls.append("submit")
        return await orig(self, a)  # type: ignore[arg-type]

    LiveSubmitter.submit = spy  # type: ignore[method-assign,assignment]
    try:
        await run_submit(factory, cfg, app_id, submitter)
    finally:
        LiveSubmitter.submit = orig  # type: ignore[method-assign]
    assert calls == [] and form_posts == ["/submit_noconfirm"]
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "SUBMISSION_UNCERTAIN"
        clicks = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "submit_clicked")).scalars().all()
    assert len(clicks) == 1


async def test_crash_after_click_is_uncertain(
        factory: sessionmaker[Session], tmp_path: Path, form_server: str,
        form_posts: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(self: object, *args: object) -> None:
        raise RuntimeError("injected fault after click")

    monkeypatch.setattr(GreenhouseAdapter, "detect_confirmation", boom)
    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg, f"{form_server}/greenhouse_submit.html")
    await run_submit(factory, cfg, app_id, _quick(cfg, factory))
    assert form_posts == ["/submit_ok"]
    app, task, events = _state(factory, app_id)
    assert app.status == "SUBMISSION_UNCERTAIN" and task.status == "FAILED"
    assert _clicks(events) == 1
    assert any(e.action == "submit_error" for e in events)


def _prepare_again_and_reapprove(factory: sessionmaker[Session], app_id: str,
                                 cfg: AppConfig) -> None:
    """User "Prepare again", then stand in for PREPARE_FORM and re-approve."""
    from jobpilot.api.actions import prepare_again

    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    with factory() as s:
        snap = s.get(ApplicationRow, app_id).form_snapshot_hash  # type: ignore[union-attr]
        prepare_again(s, orch, app_id)
    with factory() as s:
        for t in s.execute(select(TaskRow).where(TaskRow.kind == "PREPARE_FORM")).scalars():
            s.delete(t)  # the form was already read in this test; nothing to prepare
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "FORM_IN_PROGRESS"
        app.status, app.form_snapshot_hash = "READY_FOR_REVIEW", snap
        svc = ApprovalService(s)
        batch = svc.build_batch([app_id])
        svc.approve(batch.batch_id, batch.version, batch.content_hash or "", [app_id])
        app.submit_live_intent = app.portal  # the user re-requests with SUBMIT
        s.commit()


@pytest.mark.parametrize("mode", ["uncertain_resolved", "failed_resolved", "unresolved"])
async def test_prepare_again_submits_once_more_only_after_user_resolution(
        factory: sessionmaker[Session], tmp_path: Path, form_server: str,
        form_posts: list[str], mode: str) -> None:
    user_resolved = mode != "unresolved"
    from jobpilot.api.actions import resolve_uncertain
    from jobpilot.contracts.enums import ApplicationStatus
    from jobpilot.orchestrator.state_machine import transition

    cfg = live_cfg(tmp_path)
    app_id = await seed_approved(factory, cfg,
                                 f"{form_server}/greenhouse_submit_noconfirm.html")
    await run_submit(factory, cfg, app_id, _quick(cfg, factory))
    assert form_posts == ["/submit_noconfirm"]
    with factory() as s:
        if mode == "uncertain_resolved":
            resolve_uncertain(s, app_id, submitted=False)
        else:  # failed (by verification), with or without the user's "not submitted"
            app = s.get(ApplicationRow, app_id)
            assert app is not None and app.status == "SUBMISSION_UNCERTAIN"
            transition(s, app, ApplicationStatus.SUBMISSION_FAILED, actor="verification",
                       reason="t")
            s.commit()
            if mode == "failed_resolved":  # the Failed section's own button
                resolve_uncertain(s, app_id, submitted=False)
                assert s.get(ApplicationRow, app_id).status == "SUBMISSION_FAILED"  # type: ignore[union-attr]
    _prepare_again_and_reapprove(factory, app_id, cfg)
    await run_submit(factory, cfg, app_id, _quick(cfg, factory))
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "SUBMISSION_UNCERTAIN"
        clicks = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "submit_clicked")).scalars().all()
        denied = [json.loads(e.redacted_details_json) for e in s.execute(
            select(AuditEventRow).where(AuditEventRow.action == "gate_denied")).scalars()]
    if user_resolved:
        assert form_posts == ["/submit_noconfirm", "/submit_noconfirm"]
        assert len(clicks) == 2 and denied == []
        keys = {json.loads(c.redacted_details_json)["idempotency_key"] for c in clicks}
        assert len(keys) == 2  # a new attempt gets a new idempotency key
    else:
        assert form_posts == ["/submit_noconfirm"]
        assert len(clicks) == 1
        assert [d["reasons"] for d in denied] == [["prior_click"]]


async def test_prepare_again_clears_live_intent(
        factory: sessionmaker[Session], tmp_path: Path) -> None:
    from jobpilot.api.actions import prepare_again

    cfg = live_cfg(tmp_path)
    with factory() as s:
        app = make_app_row(s, status="SUBMISSION_FAILED")
        app.submit_live_intent = "greenhouse"
        app_id = app.application_id
        s.commit()
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    with factory() as s:
        prepare_again(s, orch, app_id)
    with factory() as s:
        row = s.get(ApplicationRow, app_id)
        assert row is not None and row.submit_live_intent is None
