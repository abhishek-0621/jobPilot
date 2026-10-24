import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import PortalCapability
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.mutations import (
    set_answer,
    set_application_url,
    set_artifact,
    set_library_answer,
)
from jobpilot.data.repositories import get_application, list_events
from jobpilot.data.tables import (
    AnswerLibraryRow,
    ApprovalRow,
    CandidateFactRow,
    DocumentArtifactRow,
    TaskRow,
)
from jobpilot.ids import new_id
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator, TaskKind
from jobpilot.orchestrator.policies import PolicyConfig, may_submit
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.profile.importer import import_profile
from jobpilot.profile.schema import load_profile_file
from tests.factories import seed_ready_apps
from tests.unit.test_api import make_client

SAME_ORIGIN = {"Origin": "http://127.0.0.1:8765"}
PROFILE_FIXTURE = Path("tests/fixtures/profile_example.yaml")


def _approve(s: Session, ids: list[str]) -> ApprovalRow:
    svc = ApprovalService(s)
    b = svc.build_batch(ids)
    return svc.approve(b.batch_id, b.version, b.content_hash or "", ids)


def _gate(s: Session, app_id: str) -> list[str]:
    svc = ApprovalService(s)
    app = get_application(s, app_id)
    policy = PolicyConfig(dry_run=True, portal_capabilities={
        "simulated": frozenset({PortalCapability.SUBMIT_APPLICATION})})
    return may_submit(app, svc.active_approval_for(app_id), svc.application_hash(app_id),
                      0, False, policy, simulated=True,
                      approved_item_hash=svc.current_item_hash(app_id)).reasons


def test_answer_edit_revokes_approval(db_session: Session) -> None:
    (a1,) = seed_ready_apps(db_session, 1)
    approval = _approve(db_session, [a1])
    set_answer(db_session, a1, "notice_period", "60 days", actor="user")
    assert approval.revoked_at is not None
    assert get_application(db_session, a1).status == "READY_FOR_REVIEW"
    ev = [e for e in list_events(db_session, a1) if e.action == "answer_changed"]
    assert ev and "60 days" not in ev[0].redacted_details_json


def test_artifact_swap_revokes_approval(db_session: Session) -> None:
    (a1,) = seed_ready_apps(db_session, 1)
    approval = _approve(db_session, [a1])
    art = DocumentArtifactRow(artifact_id=new_id("art"), application_id=a1, type="resume",
                              path="/tmp/x.pdf", content_hash="sha256:other")
    db_session.add(art)
    db_session.flush()
    set_artifact(db_session, a1, "resume", art.artifact_id, actor="user")
    assert get_application(db_session, a1).resume_artifact_id == art.artifact_id
    assert approval.revoked_at is not None
    assert get_application(db_session, a1).status == "READY_FOR_REVIEW"


def test_application_url_http_only(db_session: Session) -> None:
    (a1,) = seed_ready_apps(db_session, 1)
    approval = _approve(db_session, [a1])
    with pytest.raises(ValueError):
        set_application_url(db_session, a1, "javascript:alert(1)", actor="user")
    assert approval.revoked_at is None
    set_application_url(db_session, a1, "https://example.com/apply/2", actor="user")
    assert approval.revoked_at is not None


def test_invalidate_one_keeps_sibling_approved(db_session: Session) -> None:
    a1, a2 = seed_ready_apps(db_session, 2)
    for a in (a1, a2):
        get_application(db_session, a).portal = "simulated"
    approval = _approve(db_session, [a1, a2])
    set_answer(db_session, a1, "notice_period", "60 days", actor="user")
    assert approval.revoked_at is None
    assert a1 in approval.revoked_item_ids_json
    assert get_application(db_session, a1).status == "READY_FOR_REVIEW"
    assert "not_approved" in _gate(db_session, a1)
    assert get_application(db_session, a2).status == "APPROVED"
    assert _gate(db_session, a2) == []
    assert any(e.action == "approval_item_revoked"
               for e in list_events(db_session, approval.approval_id))


def test_malformed_revoked_json_is_not_approved(db_session: Session) -> None:
    (a1,) = seed_ready_apps(db_session, 1)
    get_application(db_session, a1).portal = "simulated"
    approval = _approve(db_session, [a1])
    approval.revoked_item_ids_json = "{nope"
    db_session.flush()
    assert "not_approved" in _gate(db_session, a1)


def test_all_items_revoked_sets_revoked_at(db_session: Session) -> None:
    a1, a2 = seed_ready_apps(db_session, 2)
    approval = _approve(db_session, [a1, a2])
    set_answer(db_session, a1, "notice_period", "60 days", actor="user")
    assert approval.revoked_at is None
    set_answer(db_session, a2, "notice_period", "60 days", actor="user")
    assert approval.revoked_at is not None


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "m.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


def test_orphan_sweep_skips_item_revoked(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        a1, a2 = seed_ready_apps(s, 2)
        approval = _approve(s, [a1, a2])
        # Simulate a revoked item whose app is still APPROVED (e.g. crash mid-invalidate).
        approval.revoked_item_ids_json = f'["{a1}"]'
        s.commit()
    orch = Orchestrator(factory, AppConfig(model_routes={}), {},
                        {"simulated": SimulatedAdapter()})
    orch.recover_on_startup()
    with factory() as s:
        queued = set(s.execute(select(TaskRow.entity_id).where(
            TaskRow.kind == TaskKind.SUBMIT.value)).scalars())
    assert a2 in queued
    assert a1 not in queued


def test_set_library_answer_upsert_and_clear(db_session: Session) -> None:
    import_profile(db_session, load_profile_file(PROFILE_FIXTURE))
    set_library_answer(db_session, "notice_period", "15 days")
    set_library_answer(db_session, "notice_period", "45 days")
    rows = db_session.execute(select(AnswerLibraryRow).where(
        AnswerLibraryRow.question_key == "notice_period")).scalars().all()
    assert len(rows) == 1 and rows[0].value_json == '"45 days"'
    set_library_answer(db_session, "notice_period", None)
    assert db_session.execute(select(AnswerLibraryRow).where(
        AnswerLibraryRow.question_key == "notice_period")).first() is None
    ev = [e for e in list_events(db_session) if e.action == "answer_library_changed"]
    assert ev and all("45 days" not in e.redacted_details_json for e in ev)


def test_profile_page_renders_and_verifies(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client = make_client(factory, tmp_path)
    assert "jobpilot profile import" in client.get("/").text
    with factory() as s:
        import_profile(s, load_profile_file(PROFILE_FIXTURE))
        s.commit()
        fact = s.execute(select(CandidateFactRow)).scalars().first()
        assert fact is not None
        fact_id, category = fact.fact_id, fact.category
    page = client.get("/ui/profile")
    assert page.status_code == 200
    assert "Verify your profile before matching" in page.text
    assert "You answer these yourself on each form." in page.text
    assert 'name="notice_period"' in page.text
    assert client.post(f"/ui/profile/facts/{fact_id}/verify",
                       headers={"Origin": "https://evil.example"}).status_code == 403
    r = client.post(f"/ui/profile/facts/{fact_id}/verify", headers=SAME_ORIGIN,
                    data={"verified": "true"})
    assert r.status_code == 200
    with factory() as s:
        assert s.get(CandidateFactRow, fact_id).verified_by_user  # type: ignore[union-attr]
    assert "Verify your profile before matching" not in client.get("/ui/profile").text
    r = client.post(f"/ui/profile/sections/{category}/verify", headers=SAME_ORIGIN)
    assert r.status_code in (200, 303)
    with factory() as s:
        assert all(f.verified_by_user for f in s.execute(select(CandidateFactRow).where(
            CandidateFactRow.category == category)).scalars())
    r = client.post("/ui/profile/answers", headers=SAME_ORIGIN,
                    data={"notice_period": "20 days", "clear_current_ctc_inr_lpa": "on"})
    assert r.status_code in (200, 303)
    with factory() as s:
        keys = {a.question_key: a.value_json for a in s.execute(
            select(AnswerLibraryRow)).scalars()}
    assert keys.get("notice_period") == '"20 days"'
    assert "current_ctc_inr_lpa" not in keys


def test_mutations_are_only_writers() -> None:
    patterns = [re.compile(p) for p in (r"\.proposed_value_json\s*=(?!=)",
                                        r"\.resume_artifact_id\s*=(?!=)",
                                        r"\.cover_letter_artifact_id\s*=(?!=)")]
    root = Path("src/jobpilot")
    offenders = [
        str(p) for p in root.rglob("*.py")
        if p != root / "data" / "mutations.py"
        and any(rx.search(p.read_text(encoding="utf-8")) for rx in patterns)
    ]
    assert offenders == []


def test_set_artifact_rejects_foreign_or_wrong_kind(db_session: Session) -> None:
    a1, a2 = seed_ready_apps(db_session, 2)
    approval = _approve(db_session, [a1])
    foreign = get_application(db_session, a2).resume_artifact_id
    assert foreign is not None
    before = get_application(db_session, a1).resume_artifact_id
    with pytest.raises(ValueError):
        set_artifact(db_session, a1, "resume", foreign, actor="user")
    own = get_application(db_session, a1).resume_artifact_id
    assert own is not None
    with pytest.raises(ValueError):
        set_artifact(db_session, a1, "cover_letter", own, actor="user")
    assert get_application(db_session, a1).resume_artifact_id == before
    assert get_application(db_session, a1).cover_letter_artifact_id is None
    assert approval.revoked_at is None
    assert get_application(db_session, a1).status == "APPROVED"


def test_orphan_sweep_returns_item_revoked_to_review(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        a1, a2 = seed_ready_apps(s, 2)
        approval = _approve(s, [a1, a2])
        approval.revoked_item_ids_json = f'["{a1}"]'
        s.commit()
    orch = Orchestrator(factory, AppConfig(model_routes={}), {},
                        {"simulated": SimulatedAdapter()})
    orch.recover_on_startup()
    with factory() as s:
        assert get_application(s, a1).status == "READY_FOR_REVIEW"
        assert get_application(s, a2).status == "APPROVED"
