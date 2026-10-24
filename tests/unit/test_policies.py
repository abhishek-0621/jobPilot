import json
from datetime import UTC, datetime
from typing import Any

from jobpilot.contracts.enums import (
    ApplicationStatus,
    OutreachStatus,
    PortalCapability,
)
from jobpilot.data.tables import ApplicationRow, ApprovalRow, OutreachDraftRow
from jobpilot.orchestrator.policies import (
    PolicyConfig,
    may_send_outreach,
    may_submit,
)

HASH = "sha256:abc"


def _policy(dry_run: bool = False) -> PolicyConfig:
    return PolicyConfig(
        dry_run=dry_run,
        portal_capabilities={
            "greenhouse": frozenset({PortalCapability.SUBMIT_APPLICATION}),
            "linkedin": frozenset({PortalCapability.SEND_OUTREACH}),
        },
    )


def ok_args(
    *,
    dry_run: bool = False,
    simulated: bool = False,
    current_hash: str = HASH,
    approved_item_hash: str | None = HASH,
    status: ApplicationStatus = ApplicationStatus.APPROVED,
    approval_kind: str = "APPLICATION",
    revoked: bool = False,
    item_ids: list[str] | None = None,
    portal: str | None = "greenhouse",
    unresolved_required: int = 0,
    duplicate_detected: bool = False,
    no_approval: bool = False,
) -> dict[str, Any]:
    app = ApplicationRow(application_id="app1", job_id="j1", portal=portal, status=status)
    approval = None if no_approval else ApprovalRow(
        approval_id="ap1",
        batch_id="b1",
        batch_version=1,
        kind=approval_kind,
        content_hash="sha256:batch",
        approved_item_ids_json=json.dumps(["app1"] if item_ids is None else item_ids),
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    return dict(
        app=app,
        approval=approval,
        current_hash=current_hash,
        approved_item_hash=approved_item_hash,
        unresolved_required=unresolved_required,
        duplicate_detected=duplicate_detected,
        policy=_policy(dry_run),
        simulated=simulated,
    )


def ok_outreach_args(
    *,
    dry_run: bool = False,
    simulated: bool = False,
    current_hash: str = HASH,
    approval_hash: str = HASH,
    approval_kind: str = "OUTREACH",
    revoked: bool = False,
    status: OutreachStatus = OutreachStatus.APPROVED,
    channel: str | None = "linkedin",
    already_sent_for_company: bool = False,
    no_approval: bool = False,
) -> dict[str, Any]:
    draft = OutreachDraftRow(
        outreach_id="o1",
        company_key="acme",
        campaign_id="c1",
        channel=channel,
        message_text="hi",
        status=status,
    )
    approval = None if no_approval else ApprovalRow(
        approval_id="ap2",
        batch_id="b2",
        batch_version=1,
        kind=approval_kind,
        content_hash=approval_hash,
        approved_item_ids_json=json.dumps(["o1"]),
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    return dict(
        draft=draft,
        approval=approval,
        current_hash=current_hash,
        already_sent_for_company=already_sent_for_company,
        policy=_policy(dry_run),
        simulated=simulated,
    )


def test_all_ok_allowed():
    r = may_submit(**ok_args())
    assert r.allowed and r.reasons == []


def test_dry_run_blocks_real_submit():
    r = may_submit(**ok_args(dry_run=True, simulated=False))
    assert not r.allowed and r.reasons == ["dry_run"]


def test_dry_run_allows_simulated():
    assert may_submit(**ok_args(dry_run=True, simulated=True)).allowed


def test_hash_mismatch_blocks():
    r = may_submit(**ok_args(current_hash="sha256:other"))
    assert "hash_mismatch" in r.reasons and not r.allowed


def test_missing_item_hash_is_mismatch():
    assert "hash_mismatch" in may_submit(**ok_args(approved_item_hash=None)).reasons


def test_no_approval_not_approved():
    assert "not_approved" in may_submit(**ok_args(no_approval=True)).reasons


def test_revoked_wrong_kind_or_unlisted_not_approved():
    assert "not_approved" in may_submit(**ok_args(revoked=True)).reasons
    assert "not_approved" in may_submit(**ok_args(approval_kind="OUTREACH")).reasons
    assert "not_approved" in may_submit(**ok_args(item_ids=["other"])).reasons


def test_capability_missing():
    assert "capability_missing" in may_submit(**ok_args(portal="unknown")).reasons
    assert "capability_missing" in may_submit(**ok_args(portal=None)).reasons


def test_unresolved_duplicate_wrong_status():
    r = may_submit(
        **ok_args(
            unresolved_required=2,
            duplicate_detected=True,
            status=ApplicationStatus.READY_FOR_REVIEW,
        )
    )
    assert set(r.reasons) == {"unresolved_answers", "duplicate", "wrong_status"}


def test_collects_all_reasons():
    r = may_submit(
        **ok_args(dry_run=True, current_hash="x", no_approval=True, portal="nope")
    )
    assert set(r.reasons) == {
        "dry_run", "hash_mismatch", "not_approved", "capability_missing"
    }


def test_outreach_ok():
    assert may_send_outreach(**ok_outreach_args()).allowed


def test_application_approval_cannot_authorize_outreach():
    r = may_send_outreach(**ok_outreach_args(approval_kind="APPLICATION"))
    assert not r.allowed and "not_approved" in r.reasons


def test_outreach_revoked_missing_not_approved():
    assert "not_approved" in may_send_outreach(**ok_outreach_args(revoked=True)).reasons
    assert "not_approved" in may_send_outreach(**ok_outreach_args(no_approval=True)).reasons


def test_second_outreach_same_company_blocked():
    r = may_send_outreach(**ok_outreach_args(already_sent_for_company=True))
    assert "duplicate" in r.reasons


def test_outreach_hash_status_capability_dry_run():
    assert "hash_mismatch" in may_send_outreach(**ok_outreach_args(current_hash="z")).reasons
    assert "wrong_status" in may_send_outreach(
        **ok_outreach_args(status=OutreachStatus.NEEDS_REVIEW)
    ).reasons
    assert "capability_missing" in may_send_outreach(
        **ok_outreach_args(channel="email")
    ).reasons
    assert "dry_run" in may_send_outreach(**ok_outreach_args(dry_run=True)).reasons
    assert may_send_outreach(**ok_outreach_args(dry_run=True, simulated=True)).allowed


def test_malformed_approved_ids_not_approved_without_raising():
    for bad in ["not json", "null", '{"a": 1}', '[1, 2]', '"app1"']:
        args = ok_args()
        args["approval"].approved_item_ids_json = bad
        assert "not_approved" in may_submit(**args).reasons
    args = ok_args()
    args["approval"].approved_item_ids_json = None
    assert "not_approved" in may_submit(**args).reasons


def test_empty_hashes_are_mismatch():
    assert "hash_mismatch" in may_submit(**ok_args(current_hash="", approved_item_hash="")).reasons
    assert "hash_mismatch" in may_send_outreach(
        **ok_outreach_args(current_hash="", approval_hash="")
    ).reasons


def test_policy_models_frozen_and_strict():
    import pydantic
    import pytest

    p = _policy()
    with pytest.raises(pydantic.ValidationError):
        p.dry_run = True
    with pytest.raises(pydantic.ValidationError):
        PolicyConfig(dry_run=False, portal_capabilities={}, extra=1)
    r = may_submit(**ok_args())
    with pytest.raises(pydantic.ValidationError):
        r.allowed = False
