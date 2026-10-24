"""PREPARE_FORM / VALIDATE agents + handlers and the answers page, with a fake adapter."""

import json
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents.form_prep import ADAPTERS, allowed_hosts, form_url
from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.contracts.enums import FieldDecision
from jobpilot.contracts.models import FieldAnswer
from jobpilot.data import mutations
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import create_application, upsert_job
from jobpilot.data.tables import (
    ApplicationRow,
    AuditEventRow,
    CandidateFactRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    TaskRow,
)
from jobpilot.documents import store
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator, TaskKind
from jobpilot.portals.base import FillReport, FormField, FormSnapshot, ValidationReport
from jobpilot.portals.greenhouse import GreenhouseAdapter
from jobpilot.portals.lever import LeverAdapter
from jobpilot.portals.playwright_form import snapshot_hash
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.profile.importer import active_profile, import_profile, verify_fact
from jobpilot.profile.schema import load_profile_file
from jobpilot.runtime import build_runtime
from jobpilot.util.hashing import canonical_json
from tests.factories import make_listing

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "tests" / "fixtures" / "profile_example.yaml"
ALIASES = ROOT / "config" / "field_aliases.yaml"
AUTH = "Are you legally authorized to work in this country?"
PRIVACY = "I agree to the privacy policy"
SECRET = "Yes-secret-value"


def snapshot(extra: list[FormField] | None = None) -> FormSnapshot:
    return FormSnapshot(url="http://127.0.0.1/form", fields=[
        FormField(field_id="first_name", label="First Name", field_type="text", required=True),
        FormField(field_id="email", label="Email", field_type="email", required=True),
        FormField(field_id="resume", label="Resume/CV", field_type="file", required=True),
        FormField(field_id="q_auth", label=AUTH, field_type="radio", required=True,
                  options=["Yes", "No"]),
        FormField(field_id="q_privacy", label=PRIVACY, field_type="checkbox",
                  required=False, options=[PRIVACY]),
        *(extra or []),
    ])


@dataclass
class FakePortal:
    """Stands in for a Playwright adapter: fills whatever it is allowed, never submits."""

    snap: FormSnapshot
    blockers_found: list[str] = field(default_factory=list)
    page_errors: dict[str, str] = field(default_factory=dict)
    filled_answers: list[FieldAnswer] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    submit_clicks: int = 0

    async def open_job(self, url: str) -> None:
        self.opened.append(url)

    async def blockers(self) -> list[str]:
        return list(self.blockers_found)

    async def inspect_form(self) -> FormSnapshot:
        return self.snap

    def snapshot_hash(self, snap: FormSnapshot) -> str:
        return snapshot_hash(snap)

    async def fill_fields(self, answers: list[FieldAnswer]) -> FillReport:
        todo = [a for a in answers if a.proposed_value not in (None, "", [])
                and (a.decision in (FieldDecision.AUTO_FILL, FieldDecision.FILL_AND_REVIEW)
                     or a.source_fact_id == "user")]
        self.filled_answers = todo
        return FillReport(filled=[a.field_id for a in todo], failed={})

    async def validate_form(self, expected: dict[str, Any] | None = None) -> ValidationReport:
        return ValidationReport(ok=not self.page_errors, errors=dict(self.page_errors))

    async def screenshot(self, path: Path) -> None:
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    async def submit(self, *_: Any) -> None:
        self.submit_clicks += 1
        raise AssertionError("submit must never be called")


@dataclass
class Env:
    factory: sessionmaker[Session]
    cfg: AppConfig
    orch: Orchestrator
    portal: FakePortal
    app_id: str
    tools: dict[str, Any]

    def app(self, s: Session) -> ApplicationRow:
        row = s.get(ApplicationRow, self.app_id)
        assert row is not None
        return row

    def rows(self, s: Session) -> dict[str, FormFieldAnswerRow]:
        return {r.field_id: r for r in s.execute(select(FormFieldAnswerRow).where(
            FormFieldAnswerRow.application_id == self.app_id)).scalars()}

    def tasks(self, s: Session, kind: TaskKind) -> list[TaskRow]:
        return list(s.execute(select(TaskRow).where(
            TaskRow.kind == kind.value, TaskRow.entity_id == self.app_id)).scalars())

    def client(self) -> TestClient:
        return TestClient(create_app(self.cfg, self.factory, self.orch, start_worker=False),
                          base_url="http://127.0.0.1")


def make_cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "storage", model_routes={},
                     browser_headless=True, field_aliases_path=ALIASES)


def seed_application(factory: sessionmaker[Session], cfg: AppConfig, url: str,
                     *, artifact_version: int | None = None) -> str:
    """Verified synthetic profile + a FORM_IN_PROGRESS application with bound documents."""
    (cfg.storage_path / "profiles").mkdir(parents=True, exist_ok=True)
    shutil.copy(PROFILE, cfg.storage_path / "profiles" / "profile.yaml")
    with factory() as s:
        import_profile(s, load_profile_file(PROFILE))
        for f in s.execute(select(CandidateFactRow)).scalars().all():
            verify_fact(s, f.fact_id)
        version, _ = active_profile(s)
        job, _ = upsert_job(s, make_listing(application_url=url, source="greenhouse"))
        app = create_application(s, job)
        app.status = "FORM_IN_PROGRESS"
        d = cfg.storage_path / "artifacts" / app.application_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "resume_v1.pdf").write_bytes(b"%PDF-1.4 synthetic resume")
        (d / "cover_letter_v1.pdf").write_bytes(b"%PDF-1.4 synthetic letter")
        (d / "cover_letter_v1.txt").write_text("Dear Hiring Team,\n\nHello.", "utf-8")
        pv = version if artifact_version is None else artifact_version
        for type_, name in (("resume", "resume_v1.pdf"), ("cover_letter", "cover_letter_v1.pdf"),
                            ("cover_letter_text", "cover_letter_v1.txt")):
            row = store.write_artifact(s, app.application_id, type_, d / name, [],
                                       root=cfg.storage_path, profile_version=pv)
            if type_ in ("resume", "cover_letter"):
                mutations.set_artifact(s, app.application_id, type_, row.artifact_id,
                                       actor="test")
        s.commit()
        return app.application_id


def make_env(tmp_path: Path, portal: FakePortal, **seed: Any) -> Env:
    db = tmp_path / "form.db"
    upgrade_to_head(db)
    factory = make_session_factory(make_engine(db))
    cfg = make_cfg(tmp_path)
    app_id = seed_application(factory, cfg, "http://127.0.0.1:9/apply", **seed)
    rt = build_runtime(cfg, factory)

    @asynccontextmanager
    async def opener(source: str, url: str, application_id: str) -> AsyncIterator[Any]:
        assert source == "greenhouse" and application_id == app_id
        yield portal

    rt.tools["form_opener"] = opener
    orch = Orchestrator(factory, cfg, rt.agents, {"simulated": SimulatedAdapter()},
                        handlers=rt.handlers, tools=rt.tools)
    return Env(factory, cfg, orch, portal, app_id, rt.tools)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield make_env(tmp_path, FakePortal(snapshot()))


async def run(env: Env, kind: TaskKind) -> None:
    env.orch.enqueue(kind, "application", env.app_id)
    await env.orch.run_once()


def answer_user(env: Env, data: dict[str, str]) -> int:
    return env.client().post(f"/ui/applications/{env.app_id}/answers", data=data,
                             follow_redirects=False).status_code


# ---- PREPARE_FORM ---------------------------------------------------------------------


async def test_prepare_maps_and_needs_user_input(env: Env) -> None:
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        app = env.app(s)
        assert app.status == "NEEDS_USER_INPUT"
        assert app.form_snapshot_hash == snapshot_hash(snapshot())
        rows = env.rows(s)
        assert {k: r.decision for k, r in rows.items()} == {
            "first_name": "AUTO_FILL", "email": "AUTO_FILL", "resume": "AUTO_FILL",
            "q_auth": "USER_ONLY", "q_privacy": "USER_ONLY"}
        assert json.loads(rows["first_name"].proposed_value_json) == "Asha"
        assert rows["q_auth"].required and not rows["q_privacy"].required
        assert json.loads(rows["q_auth"].options_json) == ["Yes", "No"]
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.entity_id == env.app_id,
            AuditEventRow.action == "status_changed")).scalars().all()[-1]
        assert json.loads(ev.redacted_details_json)["reason"] == "needs_user_input:q_auth"
        assert env.tasks(s, TaskKind.VALIDATE) == []
    assert env.portal.opened == ["http://127.0.0.1:9/apply"]


async def test_prepare_all_resolved_follows_up_validate(tmp_path: Path) -> None:
    snap = FormSnapshot(url="x", fields=snapshot().fields[:3])
    env = make_env(tmp_path, FakePortal(snap))
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        assert env.app(s).status == "FORM_IN_PROGRESS"
        assert [t.status for t in env.tasks(s, TaskKind.VALIDATE)] == ["QUEUED"]


async def test_prepare_blockers_manual_handoff(tmp_path: Path) -> None:
    env = make_env(tmp_path, FakePortal(snapshot(), blockers_found=["captcha"]))
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        assert env.app(s).status == "NEEDS_USER_INPUT"
        assert env.rows(s) == {}
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "status_changed",
            AuditEventRow.entity_id == env.app_id)).scalars().all()[-1]
        assert json.loads(ev.redacted_details_json)["reason"] == "manual_handoff:captcha"


async def test_prepare_empty_form_is_handoff(tmp_path: Path) -> None:
    env = make_env(tmp_path, FakePortal(FormSnapshot(url="x", fields=[])))
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        assert env.app(s).status == "NEEDS_USER_INPUT"
        assert env.tasks(s, TaskKind.VALIDATE) == []


async def test_reprepare_preserves_user_answers_and_drops_gone_fields(env: Env) -> None:
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        mutations.set_answer(s, env.app_id, "q_auth", "Yes", actor="user",
                             source_fact_id="user", decision=FieldDecision.USER_ONLY)
        s.commit()
    env.portal.snap = FormSnapshot(url="x", fields=snapshot().fields[1:])  # first_name gone
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        rows = env.rows(s)
        assert "first_name" not in rows
        assert (json.loads(rows["q_auth"].proposed_value_json), rows["q_auth"].source_fact_id,
                rows["q_auth"].decision) == ("Yes", "user", "USER_ONLY")
        assert env.app(s).status == "FORM_IN_PROGRESS"
        assert len(env.tasks(s, TaskKind.VALIDATE)) == 1


def test_upsert_answer_initial_refuses_existing_row(db_session: Session) -> None:
    from tests.factories import make_app_row

    app = make_app_row(db_session)
    a = FieldAnswer(field_id="f", label="F", field_type="text", proposed_value="v",
                    decision=FieldDecision.AUTO_FILL, confidence=1.0, rationale="r")
    mutations.upsert_answer_initial(db_session, app.application_id, a, required=True,
                                    options=[], actor="t")
    with pytest.raises(ValueError):
        mutations.upsert_answer_initial(db_session, app.application_id, a, required=True,
                                        options=[], actor="t")


def test_form_url_and_hosts() -> None:
    assert form_url({"application_url": "https://x.test/a"}) == "https://x.test/a"
    assert form_url({"source": "lever", "canonical_url": "https://jobs.lever.co/a/1"}) == \
        "https://jobs.lever.co/a/1/apply"
    assert form_url({"source": "greenhouse",
                     "canonical_url": "https://boards.greenhouse.io/a/jobs/1"}) == \
        "https://boards.greenhouse.io/a/jobs/1"
    assert ADAPTERS == {"greenhouse": GreenhouseAdapter, "lever": LeverAdapter}
    hosts = allowed_hosts(LeverAdapter, "https://jobs.lever.co/a/1/apply")
    assert hosts == {"jobs.lever.co", "api.lever.co"}
    assert "127.0.0.1" not in allowed_hosts(GreenhouseAdapter, "https://boards.greenhouse.io/x")
    assert {"127.0.0.1", "localhost"} <= allowed_hosts(GreenhouseAdapter,
                                                       "http://127.0.0.1:8000/f")


# ---- answers page + VALIDATE ----------------------------------------------------------


async def test_user_answers_then_validate_ready(env: Env) -> None:
    await run(env, TaskKind.PREPARE_FORM)
    page = env.client().get(f"/ui/applications/{env.app_id}/answers").text
    assert "You are answering this yourself" in page and PRIVACY in page
    assert answer_user(env, {"v:q_auth": "Maybe"}) == 409  # not an option
    assert answer_user(env, {"v:q_auth": "Yes", "seen:q_privacy": "1",
                             "v:q_privacy": "on"}) == 303
    with env.factory() as s:
        rows = env.rows(s)
        assert (rows["q_auth"].source_fact_id, rows["q_auth"].decision) == ("user", "USER_ONLY")
        assert json.loads(rows["q_privacy"].proposed_value_json) is True
        assert env.app(s).status == "FORM_IN_PROGRESS"
        assert [t.status for t in env.tasks(s, TaskKind.VALIDATE)] == ["QUEUED"]
        changed = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "answer_changed")).scalars().all()
        assert changed and all("Yes" not in e.redacted_details_json for e in changed)
    await env.orch.run_once()
    with env.factory() as s:
        assert env.app(s).status == "READY_FOR_REVIEW"
    filled = {a.field_id: a.proposed_value for a in env.portal.filled_answers}
    assert filled["q_auth"] == "Yes" and filled["q_privacy"] is True
    assert Path(str(filled["resume"])).name == "resume_v1.pdf"
    assert env.portal.submit_clicks == 0


async def test_screenshot_artifact_stored(env: Env) -> None:
    await run(env, TaskKind.PREPARE_FORM)
    answer_user(env, {"v:q_auth": "No"})
    await env.orch.run_once()
    with env.factory() as s:
        shots = s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.type == "form_screenshot")).scalars().all()
        assert len(shots) == 1 and shots[0].path.endswith("form_v1.png")
        assert (env.cfg.storage_path / shots[0].path).read_bytes().startswith(b"\x89PNG")
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "status_changed",
            AuditEventRow.entity_id == env.app_id)).scalars().all()[-1]
        assert json.loads(ev.redacted_details_json)["screenshot_artifact_id"] == \
            shots[0].artifact_id


async def test_content_hash_recomputed_on_ready(env: Env) -> None:
    await run(env, TaskKind.PREPARE_FORM)
    answer_user(env, {"v:q_auth": "Yes"})
    with env.factory() as s:
        env.app(s).content_hash = "stale"
        s.commit()
    await env.orch.run_once()
    with env.factory() as s:
        app = env.app(s)
        assert app.status == "READY_FOR_REVIEW"
        assert app.content_hash == ApprovalService(s).application_hash(env.app_id)
        events = [json.loads(e.redacted_details_json) for e in s.execute(
            select(AuditEventRow).where(AuditEventRow.action == "status_changed",
                                        AuditEventRow.entity_id == env.app_id)).scalars()]
        assert [e["to"] for e in events][-2:] == ["VALIDATING", "READY_FOR_REVIEW"]


def test_application_hash_includes_decisions(env: Env) -> None:
    with env.factory() as s:
        a = FieldAnswer(field_id="f", label="F", field_type="text", proposed_value="v",
                        decision=FieldDecision.FILL_AND_REVIEW, confidence=1.0, rationale="r")
        mutations.upsert_answer_initial(s, env.app_id, a, required=False, options=[],
                                        actor="t")
        before = ApprovalService(s).application_hash(env.app_id)
        mutations.set_answer(s, env.app_id, "f", "v", actor="t",
                             decision=FieldDecision.USER_ONLY)
        assert ApprovalService(s).application_hash(env.app_id) != before


async def test_validate_stale_artifact_requeues_tailor(tmp_path: Path) -> None:
    env = make_env(tmp_path, FakePortal(FormSnapshot(url="x", fields=snapshot().fields[:3])),
                   artifact_version=0)
    with env.factory() as s:
        env.app(s).form_snapshot_hash = "anything"
        s.commit()
    await run(env, TaskKind.VALIDATE)
    with env.factory() as s:
        assert env.app(s).status == "DOCUMENTS_PENDING"
        assert [t.status for t in env.tasks(s, TaskKind.TAILOR)] == ["QUEUED"]
        assert s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.type == "form_screenshot")).first() is None
    assert env.portal.opened == []  # never opened the form with stale documents


async def test_validate_layout_changed_needs_user_input(env: Env) -> None:
    await run(env, TaskKind.PREPARE_FORM)
    answer_user(env, {"v:q_auth": "Yes"})
    env.portal.snap = snapshot([FormField(field_id="new", label="New", field_type="text",
                                          required=True)])
    await env.orch.run_once()
    with env.factory() as s:
        assert env.app(s).status == "NEEDS_USER_INPUT"
        assert env.portal.filled_answers == []
        assert env.tasks(s, TaskKind.PREPARE_FORM)[0].status == "SUCCEEDED"
        assert len(env.tasks(s, TaskKind.PREPARE_FORM)) == 1


async def test_validate_failure_records_codes_not_values(env: Env) -> None:
    await run(env, TaskKind.PREPARE_FORM)
    answer_user(env, {"v:q_auth": "Yes"})
    env.portal.page_errors = {"email": f"expected {SECRET!r}, found 'x'",
                              "first_name": f"Server says {SECRET}"}
    await env.orch.run_once()
    with env.factory() as s:
        assert env.app(s).status == "VALIDATION_FAILED"
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "status_changed",
            AuditEventRow.entity_id == env.app_id)).scalars().all()[-1]
        assert SECRET not in ev.redacted_details_json
        assert json.loads(ev.redacted_details_json)["errors"] == {
            "email": "VALUE_MISMATCH", "first_name": "FIELD_ERROR"}
        assert json.loads(env.rows(s)["email"].validation_errors_json) == ["VALUE_MISMATCH"]
    # the user can answer again from VALIDATION_FAILED, which re-queues VALIDATE
    assert answer_user(env, {"v:q_auth": "No"}) == 303
    with env.factory() as s:
        assert env.app(s).status == "FORM_IN_PROGRESS"


async def test_cover_letter_text_and_paths_in_loader(env: Env) -> None:
    app = env.tools["application_loader"](env.app_id)
    assert app["cover_letter_text"].startswith("Dear Hiring Team")
    assert Path(app["resume_path"]).is_relative_to(
        (env.cfg.storage_path / "artifacts" / env.app_id).resolve())
    ctx = env.tools["mapping_context"](env.app_id)
    assert ctx.identity.first_name == "Asha" and ctx.library["notice_period"][0] == "30 days"


def test_answers_page_read_only_outside_answerable(env: Env) -> None:
    with env.factory() as s:
        a = FieldAnswer(field_id="q", label="Q", field_type="text", proposed_value=None,
                        decision=FieldDecision.ASK_USER, confidence=0.0, rationale="r")
        mutations.upsert_answer_initial(s, env.app_id, a, required=True, options=[],
                                        actor="t")
        s.commit()
    assert answer_user(env, {"v:q": "x"}) == 409  # FORM_IN_PROGRESS
    assert canonical_json(None) in {r.proposed_value_json for r in
                                    env.rows(env.factory()).values()}
