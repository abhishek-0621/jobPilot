import pytest

from jobpilot.contracts.enums import PortalCapability
from jobpilot.data.repositories import get_application, list_events
from jobpilot.data.tables import ContactCandidateRow, OutreachDraftRow
from jobpilot.ids import new_id
from jobpilot.orchestrator.approval import ApprovalService, StaleApprovalError
from jobpilot.orchestrator.policies import PolicyConfig, may_submit
from tests.factories import edit_answer, make_app_row, seed_ready_apps


def policy_ok() -> PolicyConfig:
    return PolicyConfig(
        dry_run=True,
        portal_capabilities={"greenhouse": frozenset({PortalCapability.SUBMIT_APPLICATION})},
    )


def test_approve_happy_path(db_session):
    a1, a2 = seed_ready_apps(db_session, 2)
    svc = ApprovalService(db_session)
    b = svc.build_batch([a1, a2])
    assert b.version == 1 and b.content_hash.startswith("sha256:")
    approval = svc.approve(b.batch_id, b.version, b.content_hash, [a1])
    assert get_application(db_session, a1).status == "APPROVED"
    assert get_application(db_session, a2).status == "READY_FOR_REVIEW"
    assert b.status == "PARTIALLY_APPROVED"
    assert approval.kind == "APPLICATION"
    assert approval.content_hash == b.content_hash
    assert svc.current_item_hash(a1) == svc.application_hash(a1)
    assert svc.current_item_hash(a2) is None


def test_approve_all_marks_batch_approved_and_may_submit(db_session):
    (a1,) = seed_ready_apps(db_session, 1)
    app = get_application(db_session, a1)
    app.portal = "greenhouse"
    svc = ApprovalService(db_session)
    b = svc.build_batch([a1])
    approval = svc.approve(b.batch_id, b.version, b.content_hash, [a1])
    assert b.status == "APPROVED"
    r = may_submit(app, approval, svc.application_hash(a1), 0, False, policy_ok(),
                   simulated=True, approved_item_hash=svc.current_item_hash(a1))
    assert r.allowed, r.reasons


def test_approve_with_stale_hash_rejected(db_session):
    ids = seed_ready_apps(db_session, 1)
    b = ApprovalService(db_session).build_batch(ids)
    with pytest.raises(StaleApprovalError):
        ApprovalService(db_session).approve(b.batch_id, b.version, "sha256:old", ids)
    assert get_application(db_session, ids[0]).status == "READY_FOR_REVIEW"


def test_approve_rejects_changed_item(db_session):
    (a1,) = seed_ready_apps(db_session, 1)
    svc = ApprovalService(db_session)
    b = svc.build_batch([a1])
    edit_answer(db_session, a1, "notice_period", "60 days")
    with pytest.raises(StaleApprovalError):
        svc.approve(b.batch_id, b.version, b.content_hash, [a1])


def test_rebuild_bumps_version_and_old_version_stale(db_session):
    (a1,) = seed_ready_apps(db_session, 1)
    svc = ApprovalService(db_session)
    b1 = svc.build_batch([a1])
    old_version, old_hash = b1.version, b1.content_hash
    b2 = svc.rebuild_batch(b1.batch_id)
    assert b2.batch_id == b1.batch_id and b2.version == 2
    with pytest.raises(StaleApprovalError):
        svc.approve(b1.batch_id, old_version, old_hash, [a1])
    svc.approve(b2.batch_id, b2.version, b2.content_hash, [a1])


def test_approve_rejects_id_not_in_batch(db_session):
    a1, a2 = seed_ready_apps(db_session, 2)
    svc = ApprovalService(db_session)
    b = svc.build_batch([a1])
    with pytest.raises(ValueError):
        svc.approve(b.batch_id, b.version, b.content_hash, [a2])


def test_edit_after_approval_invalidates(db_session):
    (a1,) = seed_ready_apps(db_session, 1)
    svc = ApprovalService(db_session)
    b = svc.build_batch([a1])
    approval = svc.approve(b.batch_id, b.version, b.content_hash, [a1])
    before = svc.application_hash(a1)
    edit_answer(db_session, a1, "notice_period", "60 days")
    assert approval.revoked_at is not None
    assert svc.application_hash(a1) != before
    assert get_application(db_session, a1).status == "READY_FOR_REVIEW"
    assert svc.current_item_hash(a1) is None
    assert any(e.action == "approval_revoked" for e in list_events(db_session, approval.approval_id))
    assert not may_submit(get_application(db_session, a1), approval, svc.application_hash(a1),
                          0, False, policy_ok(), simulated=True).allowed


def test_cannot_batch_unready_app(db_session):
    (a1,) = seed_ready_apps(db_session, 1)
    other = make_app_row(db_session, status="NEEDS_USER_INPUT")
    with pytest.raises(ValueError):
        ApprovalService(db_session).build_batch([a1, other.application_id])


def _draft(db_session) -> OutreachDraftRow:
    contact = ContactCandidateRow(contact_id=new_id("con"), company="Acme", name="Pat",
                                  public_source_url="https://example.com/pat", confidence=0.9)
    draft = OutreachDraftRow(outreach_id=new_id("out"), company_key="acme", campaign_id="c1",
                             contact_id=contact.contact_id, channel="linkedin",
                             message_text="hi", status="NEEDS_REVIEW")
    db_session.add_all([contact, draft])
    db_session.flush()
    return draft


def test_approve_outreach_happy_path(db_session):
    draft = _draft(db_session)
    svc = ApprovalService(db_session)
    h = svc.outreach_hash(draft.outreach_id)
    approval = svc.approve_outreach(draft.outreach_id, h)
    assert approval.kind == "OUTREACH" and approval.content_hash == h
    assert approval.batch_id == draft.outreach_id and approval.batch_version == 1
    assert draft.status == "APPROVED"


def test_approve_outreach_stale(db_session):
    draft = _draft(db_session)
    svc = ApprovalService(db_session)
    h = svc.outreach_hash(draft.outreach_id)
    draft.message_text = "changed"
    with pytest.raises(StaleApprovalError):
        svc.approve_outreach(draft.outreach_id, h)
    assert draft.status == "NEEDS_REVIEW"


def test_second_approve_preserves_prior_selection(db_session):
    from jobpilot.data.tables import BatchItemRow

    a1, a2 = seed_ready_apps(db_session, 2)
    svc = ApprovalService(db_session)
    b = svc.build_batch([a1, a2])
    svc.approve(b.batch_id, b.version, b.content_hash, [a1])
    assert b.status == "PARTIALLY_APPROVED"
    svc.approve(b.batch_id, b.version, b.content_hash, [a2])
    for a in (a1, a2):
        assert db_session.get(BatchItemRow, (b.batch_id, b.version, a)).selected
        assert get_application(db_session, a).status == "APPROVED"
    assert b.status == "APPROVED"


def test_approve_validates_state_before_mutating(db_session):
    from sqlalchemy import func, select

    from jobpilot.data.tables import ApprovalRow

    def n_approvals() -> int:
        return db_session.execute(select(func.count()).select_from(ApprovalRow)).scalar()

    a1, a2 = seed_ready_apps(db_session, 2)
    svc = ApprovalService(db_session)
    b = svc.build_batch([a1, a2])
    svc.approve(b.batch_id, b.version, b.content_hash, [a1])
    before = n_approvals()
    # a1 already APPROVED -> re-approving it (with a2) must fail with nothing changed
    with pytest.raises(ValueError):
        svc.approve(b.batch_id, b.version, b.content_hash, [a1, a2])
    assert db_session.new == set() or not any(isinstance(o, ApprovalRow) for o in db_session.new)
    assert n_approvals() == before
    assert get_application(db_session, a2).status == "READY_FOR_REVIEW"
    # batch in a non-approvable state
    svc.approve(b.batch_id, b.version, b.content_hash, [a2])
    assert b.status == "APPROVED"
    with pytest.raises(ValueError):
        svc.approve(b.batch_id, b.version, b.content_hash, [a2])
    assert n_approvals() == before + 1


def test_outreach_hash_tracks_resume_content(db_session):
    from jobpilot.data.tables import DocumentArtifactRow
    art = db_session.query(DocumentArtifactRow).first()
    if art is None:
        a1 = seed_ready_apps(db_session, 1)[0]
        art = db_session.query(DocumentArtifactRow).first()
    assert art is not None
    draft = _draft(db_session)
    draft.resume_artifact_id = art.artifact_id
    db_session.flush()
    svc = ApprovalService(db_session)
    old = svc.outreach_hash(draft.outreach_id)
    art.content_hash = "sha256:changed"
    db_session.flush()
    assert svc.outreach_hash(draft.outreach_id) != old
    with pytest.raises(StaleApprovalError):
        svc.approve_outreach(draft.outreach_id, old)


def test_revocation_blocks_submit_even_with_old_item_hash(db_session):
    (a1,) = seed_ready_apps(db_session, 1)
    svc = ApprovalService(db_session)
    b = svc.build_batch([a1])
    approval = svc.approve(b.batch_id, b.version, b.content_hash, [a1])
    before = svc.application_hash(a1)
    edit_answer(db_session, a1, "notice_period", "60 days")
    gate = may_submit(get_application(db_session, a1), approval, svc.application_hash(a1),
                      0, False, policy_ok(), simulated=True, approved_item_hash=before)
    assert not gate.allowed and "not_approved" in gate.reasons
