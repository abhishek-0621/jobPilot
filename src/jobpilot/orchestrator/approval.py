"""Content-hash bound batch approvals: users approve exactly what they saw."""

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import (
    ApplicationStatus,
    ApprovalKind,
    BatchStatus,
    OutreachStatus,
)
from jobpilot.data.db import utcnow
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalBatchRow,
    ApprovalRow,
    BatchItemRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    OutreachDraftRow,
)
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.policies import revoked_items
from jobpilot.orchestrator.state_machine import transition
from jobpilot.util.hashing import canonical_json, content_hash

PLANNED_ACTIONS = ["upload_resume", "upload_cover_letter", "submit"]


class StaleApprovalError(Exception):
    pass


def forget_live_intent(app: ApplicationRow) -> None:
    """Any revocation drops the recorded live intent: a later live submit needs a new
    typed SUBMIT."""
    app.submit_live_intent = None


class ApprovalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- hashing -------------------------------------------------------
    def _artifact_hash(self, artifact_id: str | None) -> str | None:
        if artifact_id is None:
            return None
        art = self.session.get(DocumentArtifactRow, artifact_id)
        return art.content_hash if art is not None else None

    def application_hash(self, application_id: str) -> str:
        app = get_application(self.session, application_id)
        job = self.session.get(JobRow, app.job_id)
        answers = self.session.execute(
            select(FormFieldAnswerRow).where(
                FormFieldAnswerRow.application_id == application_id
            )
        ).scalars()
        return self._hash(app, job, list(answers))

    def application_hashes(self, application_ids: list[str]) -> dict[str, str]:
        """``application_hash`` for many applications with a fixed number of queries
        (dashboard pages). Missing applications are left out."""
        s = self.session
        apps = list(s.execute(select(ApplicationRow).where(
            ApplicationRow.application_id.in_(application_ids))).scalars())
        jobs = {j.job_id: j for j in s.execute(select(JobRow).where(
            JobRow.job_id.in_([a.job_id for a in apps]))).scalars()}
        art_ids = [i for a in apps for i in (a.resume_artifact_id, a.cover_letter_artifact_id)
                   if i]
        # Keep a strong reference: the session identity map is weak, and _artifact_hash's
        # session.get must find these rows without a query.
        arts = list(s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.artifact_id.in_(art_ids))).scalars())
        answers: dict[str, list[FormFieldAnswerRow]] = {}
        for a in s.execute(select(FormFieldAnswerRow).where(
                FormFieldAnswerRow.application_id.in_(application_ids))).scalars():
            answers.setdefault(a.application_id, []).append(a)
        out = {a.application_id: self._hash(a, jobs.get(a.job_id),
                                            answers.get(a.application_id, []))
               for a in apps}
        del arts
        return out

    def _hash(self, app: ApplicationRow, job: JobRow | None,
              answers: list[FormFieldAnswerRow]) -> str:
        answer_list: list[Any] = sorted(
            [a.field_id, json.loads(a.proposed_value_json), a.decision] for a in answers
        )
        return content_hash({
            "application_id": app.application_id,
            "application_url": job.application_url if job is not None else None,
            "resume_artifact_hash": self._artifact_hash(app.resume_artifact_id),
            "cover_letter_artifact_hash": self._artifact_hash(app.cover_letter_artifact_id),
            "answers": answer_list,
            "planned_actions": PLANNED_ACTIONS,
        })

    @staticmethod
    def _batch_hash(items: list[BatchItemRow]) -> str:
        return content_hash(sorted([i.application_id, i.item_hash] for i in items))

    def _items(self, batch_id: str, version: int) -> list[BatchItemRow]:
        return list(self.session.execute(
            select(BatchItemRow).where(
                BatchItemRow.batch_id == batch_id, BatchItemRow.version == version
            )
        ).scalars())

    def _latest_version(self, batch_id: str) -> int | None:
        return self.session.execute(
            select(func.max(ApprovalBatchRow.version)).where(
                ApprovalBatchRow.batch_id == batch_id
            )
        ).scalar()

    # ---- batches -------------------------------------------------------
    def _freeze(self, batch_id: str, version: int, application_ids: list[str]) -> ApprovalBatchRow:
        ids = sorted(set(application_ids))
        if not ids:
            raise ValueError("batch must contain at least one application")
        for app_id in ids:
            status = get_application(self.session, app_id).status
            if status != ApplicationStatus.READY_FOR_REVIEW:
                raise ValueError(f"application {app_id} is {status}, not READY_FOR_REVIEW")
        batch = ApprovalBatchRow(batch_id=batch_id, version=version,
                                 status=BatchStatus.BUILDING.value)
        self.session.add(batch)
        items = [
            BatchItemRow(batch_id=batch_id, version=version, application_id=a,
                         item_hash=self.application_hash(a), selected=False)
            for a in ids
        ]
        self.session.add_all(items)
        batch.content_hash = self._batch_hash(items)
        self.session.flush()
        transition(self.session, batch, BatchStatus.READY_FOR_REVIEW, actor="system",
                   reason="batch_frozen", details={"version": version, "items": len(items)})
        self.session.flush()
        return batch

    def build_batch(self, application_ids: list[str]) -> ApprovalBatchRow:
        return self._freeze(new_id("batch"), 1, application_ids)

    def rebuild_batch(self, batch_id: str, only_ready: bool = False) -> ApprovalBatchRow:
        """Freeze the next version. only_ready drops applications no longer READY_FOR_REVIEW."""
        latest = self._latest_version(batch_id)
        if latest is None:
            raise ValueError(f"unknown batch {batch_id}")
        ids = [i.application_id for i in self._items(batch_id, latest)]
        if only_ready:
            ids = [a for a in ids if get_application(self.session, a).status
                   == ApplicationStatus.READY_FOR_REVIEW]
        return self._freeze(batch_id, latest + 1, ids)

    def approve(
        self,
        batch_id: str,
        version: int,
        presented_hash: str,
        application_ids: list[str],
        actor: str = "user",
    ) -> ApprovalRow:
        batch = self.session.get(ApprovalBatchRow, (batch_id, version))
        if batch is None:
            raise ValueError(f"unknown batch {batch_id} v{version}")
        if version != self._latest_version(batch_id):
            raise StaleApprovalError("batch version is not the latest")
        items = {i.application_id: i for i in self._items(batch_id, version)}
        if not presented_hash or presented_hash != batch.content_hash \
                or presented_hash != self._batch_hash(list(items.values())):
            raise StaleApprovalError("presented hash does not match batch")
        selected = sorted(set(application_ids))
        if not selected:
            raise ValueError("no applications selected")
        missing = [a for a in selected if a not in items]
        if missing:
            raise ValueError(f"applications not in batch: {missing}")
        if batch.status not in (BatchStatus.READY_FOR_REVIEW, BatchStatus.PARTIALLY_APPROVED):
            raise ValueError(f"batch {batch_id} v{version} is {batch.status}")
        apps = {a: get_application(self.session, a) for a in selected}
        for a, app in apps.items():
            if app.status != ApplicationStatus.READY_FOR_REVIEW:
                raise ValueError(f"application {a} is {app.status}, not READY_FOR_REVIEW")
        for a in selected:
            if self.application_hash(a) != items[a].item_hash:
                raise StaleApprovalError(f"application {a} changed since batch was built")
        # All checks passed; mutate from here on. Never clear earlier selections.
        for a in selected:
            items[a].selected = True
        approval = ApprovalRow(
            approval_id=new_id("apr"), batch_id=batch_id, batch_version=version,
            kind=ApprovalKind.APPLICATION.value, content_hash=batch.content_hash,
            approved_item_ids_json=canonical_json(selected),
        )
        self.session.add(approval)
        for a in selected:
            transition(self.session, apps[a], ApplicationStatus.APPROVED, actor=actor,
                       reason="batch_approved", details={"approval_id": approval.approval_id})
        target = (BatchStatus.APPROVED if all(i.selected for i in items.values())
                  else BatchStatus.PARTIALLY_APPROVED)
        if batch.status != target:
            transition(self.session, batch, target, actor=actor, reason="approved",
                       details={"approval_id": approval.approval_id})
        record_event(self.session, entity_type="approval", entity_id=approval.approval_id,
                     actor=actor, action="approval_granted", outcome="ok",
                     details={"batch_id": batch_id, "version": version, "items": selected})
        self.session.flush()
        return approval

    # ---- invalidation / lookup ------------------------------------------
    def _approvals_containing(self, application_id: str) -> list[ApprovalRow]:
        rows = self.session.execute(
            select(ApprovalRow).where(
                ApprovalRow.kind == ApprovalKind.APPLICATION.value,
                ApprovalRow.revoked_at.is_(None),
            ).order_by(ApprovalRow.approved_at.desc(), ApprovalRow.approval_id.desc())
        ).scalars()
        out: list[ApprovalRow] = []
        for r in rows:
            revoked = revoked_items(r)
            if revoked is None or application_id in revoked:
                continue
            if application_id in _approved_ids(r):
                out.append(r)
        return out

    def invalidate_application(self, application_id: str, reason: str) -> None:
        for approval in self._approvals_containing(application_id):
            revoked = sorted({*(revoked_items(approval) or []), application_id})
            approval.revoked_item_ids_json = canonical_json(revoked)
            record_event(self.session, entity_type="approval", entity_id=approval.approval_id,
                         actor="system", action="approval_item_revoked", outcome="ok",
                         details={"application_id": application_id, "reason": reason})
            if set(_approved_ids(approval)) <= set(revoked):
                approval.revoked_at = utcnow()
                record_event(self.session, entity_type="approval",
                             entity_id=approval.approval_id, actor="system",
                             action="approval_revoked", outcome="ok",
                             details={"application_id": application_id, "reason": reason})
        app = get_application(self.session, application_id)
        forget_live_intent(app)
        if app.status == ApplicationStatus.APPROVED:
            transition(self.session, app, ApplicationStatus.READY_FOR_REVIEW,
                       actor="system", reason=reason)
        self.session.flush()

    def active_approval_for(self, application_id: str) -> ApprovalRow | None:
        approvals = self._approvals_containing(application_id)
        return approvals[0] if approvals else None

    def active_outreach_approval_for(self, outreach_id: str) -> ApprovalRow | None:
        rows = self.session.execute(
            select(ApprovalRow).where(
                ApprovalRow.kind == ApprovalKind.OUTREACH.value,
                ApprovalRow.revoked_at.is_(None),
            ).order_by(ApprovalRow.approved_at.desc(), ApprovalRow.approval_id.desc())
        ).scalars()
        for r in rows:
            if outreach_id in _approved_ids(r):
                return r
        return None

    def invalidate_outreach(self, outreach_id: str, reason: str) -> None:
        """Revoke every active outreach approval of the draft; an APPROVED draft goes back
        to NEEDS_REVIEW. Application approvals are never touched (separate scope)."""
        while (approval := self.active_outreach_approval_for(outreach_id)) is not None:
            approval.revoked_at = utcnow()
            record_event(self.session, entity_type="approval",
                         entity_id=approval.approval_id, actor="system",
                         action="approval_revoked", outcome="ok",
                         details={"outreach_id": outreach_id, "reason": reason})
            self.session.flush()
        draft = self.session.get(OutreachDraftRow, outreach_id)
        if draft is not None and draft.status == OutreachStatus.APPROVED:
            transition(self.session, draft, OutreachStatus.NEEDS_REVIEW, actor="system",
                       reason=reason)
        self.session.flush()

    def current_item_hash(self, application_id: str) -> str | None:
        approvals = self._approvals_containing(application_id)
        if not approvals:
            return None
        item = self.session.get(
            BatchItemRow, (approvals[0].batch_id, approvals[0].batch_version, application_id)
        )
        return item.item_hash if item is not None else None

    # ---- outreach -------------------------------------------------------
    def outreach_hash(self, outreach_id: str) -> str:
        d = self.session.get(OutreachDraftRow, outreach_id)
        if d is None:
            raise ValueError(f"unknown outreach draft {outreach_id}")
        return content_hash({
            "outreach_id": d.outreach_id, "contact_id": d.contact_id, "channel": d.channel,
            "message_text": d.message_text,
            "resume_artifact_hash": self._artifact_hash(d.resume_artifact_id),
        })

    def approve_outreach(
        self, outreach_id: str, presented_hash: str, actor: str = "user"
    ) -> ApprovalRow:
        current = self.outreach_hash(outreach_id)
        if not presented_hash or presented_hash != current:
            raise StaleApprovalError("outreach draft changed since it was presented")
        draft = self.session.get(OutreachDraftRow, outreach_id)
        if draft is None:
            raise ValueError(f"unknown outreach draft {outreach_id}")
        if draft.contact_id is None or draft.channel is None:
            raise ValueError("choose a contact before approving")
        if draft.status != OutreachStatus.NEEDS_REVIEW:
            raise ValueError(f"outreach draft is {draft.status}, not NEEDS_REVIEW")
        approval = ApprovalRow(
            approval_id=new_id("apr"), batch_id=outreach_id, batch_version=1,
            kind=ApprovalKind.OUTREACH.value, content_hash=current,
            approved_item_ids_json=canonical_json([outreach_id]),
        )
        self.session.add(approval)
        draft.content_hash = current
        transition(self.session, draft, OutreachStatus.APPROVED, actor=actor,
                   reason="outreach_approved", details={"approval_id": approval.approval_id})
        record_event(self.session, entity_type="approval", entity_id=approval.approval_id,
                     actor=actor, action="approval_granted", outcome="ok",
                     details={"outreach_id": outreach_id})
        self.session.flush()
        return approval


def _approved_ids(approval: ApprovalRow) -> list[str]:
    """Approved item ids; malformed JSON means nothing is approved."""
    try:
        ids = json.loads(approval.approved_item_ids_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str)]
