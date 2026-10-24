"""Pure policy gates for application submission and outreach sending."""

import json

from pydantic import BaseModel, ConfigDict

from jobpilot.contracts.enums import (
    ApplicationStatus,
    ApprovalKind,
    OutreachStatus,
    PortalCapability,
)
from jobpilot.data.tables import ApplicationRow, ApprovalRow, OutreachDraftRow


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dry_run: bool
    portal_capabilities: dict[str, frozenset[PortalCapability]]


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reasons: list[str]


def _result(reasons: list[str]) -> GateResult:
    return GateResult(allowed=not reasons, reasons=reasons)


def _approval_valid(
    approval: ApprovalRow | None, kind: ApprovalKind, item_id: str
) -> bool:
    if approval is None or approval.revoked_at is not None:
        return False
    if approval.kind != kind:
        return False
    try:
        ids = json.loads(approval.approved_item_ids_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return False
    return item_id in ids


def _hashes_match(approved: str | None, current: str | None) -> bool:
    return bool(approved) and bool(current) and approved == current


def _has_capability(
    policy: PolicyConfig, key: str | None, capability: PortalCapability
) -> bool:
    return key is not None and capability in policy.portal_capabilities.get(
        key, frozenset()
    )


def may_submit(
    app: ApplicationRow,
    approval: ApprovalRow | None,
    current_hash: str,
    unresolved_required: int,
    duplicate_detected: bool,
    policy: PolicyConfig,
    *,
    simulated: bool,
    approved_item_hash: str | None = None,
) -> GateResult:
    reasons: list[str] = []
    if policy.dry_run and not simulated:
        reasons.append("dry_run")
    if not _hashes_match(approved_item_hash, current_hash):
        reasons.append("hash_mismatch")
    if not _approval_valid(approval, ApprovalKind.APPLICATION, app.application_id):
        reasons.append("not_approved")
    if not _has_capability(policy, app.portal, PortalCapability.SUBMIT_APPLICATION):
        reasons.append("capability_missing")
    if unresolved_required > 0:
        reasons.append("unresolved_answers")
    if duplicate_detected:
        reasons.append("duplicate")
    if app.status != ApplicationStatus.APPROVED:
        reasons.append("wrong_status")
    return _result(reasons)


def may_send_outreach(
    draft: OutreachDraftRow,
    approval: ApprovalRow | None,
    current_hash: str,
    already_sent_for_company: bool,
    policy: PolicyConfig,
    *,
    simulated: bool,
) -> GateResult:
    reasons: list[str] = []
    if policy.dry_run and not simulated:
        reasons.append("dry_run")
    if approval is None or not _hashes_match(approval.content_hash, current_hash):
        reasons.append("hash_mismatch")
    if not _approval_valid(approval, ApprovalKind.OUTREACH, draft.outreach_id):
        reasons.append("not_approved")
    if not _has_capability(policy, draft.channel, PortalCapability.SEND_OUTREACH):
        reasons.append("capability_missing")
    if already_sent_for_company:
        reasons.append("duplicate")
    if draft.status != OutreachStatus.APPROVED:
        reasons.append("wrong_status")
    return _result(reasons)
