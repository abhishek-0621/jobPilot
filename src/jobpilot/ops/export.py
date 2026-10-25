"""Data export: a per-application audit bundle (zip) and a full-data export.

Exports contain personal data: they live under storage/exports (dir 0700, files 0600).
They never include browser_state, backups, logs, run files or anything from the keyring;
events are exported from ``redacted_details_json`` only.
"""

import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.config import AppConfig
from jobpilot.data.db import Base, utcnow
from jobpilot.data.tables import (
    ApplicationRow,
    ApprovalRow,
    AuditEventRow,
    BatchItemRow,
    ContactCandidateRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    MatchAssessmentRow,
    OutreachDraftRow,
)
from jobpilot.observability.events import EVENT_ORDER, SUBMIT_CLICKED, last_event
from jobpilot.ops.backup import TREES, _copy_tree, _private_dir

SCHEMA = 1
PERSONAL_DATA_NOTE = ("Exports contain your personal data (profile, answers, documents). "
                      "Keep them private.")


class JobOut(BaseModel):
    id: str
    title: str
    company: str
    location: str | None
    source: str
    canonical_url: str
    application_url: str | None
    status: str


class MatchOut(BaseModel):
    profile_version: int
    scores: dict[str, float | bool]
    evidence_fact_ids: list[str]
    explanation: str


class ApplicationOut(BaseModel):
    status: str
    portal: str | None
    confirmation_reference: str | None
    timestamps: dict[str, str | None]


class AnswerOut(BaseModel):
    field_id: str
    label: str
    decision: str
    value: Any
    source_fact_id: str | None


class ArtifactOut(BaseModel):
    id: str
    type: str
    version: int
    content_hash: str
    path_in_zip: str | None


class ApprovalOut(BaseModel):
    batch_id: str
    version: int
    approved_at: str
    revoked_at: str | None
    kind: str


class EventOut(BaseModel):
    timestamp_utc: str
    entity_type: str
    entity_id: str
    actor: str
    action: str
    outcome: str
    redacted_details: Any
    error_code: str | None


class ContactOut(BaseModel):
    id: str
    kind: str | None
    name: str
    professional_title: str | None
    channel: str | None
    address: str | None
    public_source_url: str
    confidence: float
    verified_at: str | None


class OutreachOut(BaseModel):
    id: str
    campaign_id: str
    status: str
    skip_reason: str | None
    channel: str | None
    message_text: str
    contact: ContactOut | None
    sent_at: str | None
    sent_note: str | None


class ApplicationBundle(BaseModel):
    schema_version: int = SCHEMA
    exported_utc: str
    job: JobOut
    match: MatchOut | None
    application: ApplicationOut
    answers: list[AnswerOut]
    artifacts: list[ArtifactOut]
    approvals: list[ApprovalOut]
    outreach: list[OutreachOut] = []
    contacts: list[ContactOut] = []
    events: list[EventOut]


def exports_dir(cfg: AppConfig) -> Path:
    return _private_dir(cfg.storage_path / "exports")


def contained_artifact_path(storage: Path, rel: str, application_id: str | None = None
                            ) -> Path | None:
    """Resolve ``rel`` under storage/artifacts (and storage/artifacts/<app> if given);
    None when it escapes, is missing or is not a regular file. Same check as /artifacts."""
    base = (storage / "artifacts").resolve()
    if application_id is not None:
        base = base / application_id
    path = (storage / rel).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return None
    return path


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _match(s: Session, job_id: str) -> MatchOut | None:
    m = s.execute(select(MatchAssessmentRow).where(MatchAssessmentRow.job_id == job_id)
                  .order_by(MatchAssessmentRow.profile_version.desc())).scalars().first()
    if m is None:
        return None
    ev = _json(m.evidence_json or "{}", {})
    ev = ev if isinstance(ev, dict) else {}
    ids = ev.get("matched_evidence_fact_ids")
    return MatchOut(
        profile_version=m.profile_version,
        scores={"skill_fit": m.skill_fit, "experience_fit": m.experience_fit,
                "project_fit": m.project_fit, "role_seniority_fit": m.role_seniority_fit,
                "location_fit": m.location_fit, "total_score": m.total_score,
                "confidence": m.confidence, "hard_filter_passed": m.hard_filter_passed},
        evidence_fact_ids=[str(i) for i in ids] if isinstance(ids, list) else [],
        explanation=str(ev.get("explanation", "")))


def _approvals(s: Session, app: ApplicationRow) -> list[ApprovalOut]:
    batch_ids = set(s.execute(select(BatchItemRow.batch_id).where(
        BatchItemRow.application_id == app.application_id)).scalars())
    if app.batch_id:
        batch_ids.add(app.batch_id)
    batch_ids |= set(s.execute(select(OutreachDraftRow.outreach_id).where(
        OutreachDraftRow.application_id == app.application_id)).scalars())  # OUTREACH scope
    rows = s.execute(select(ApprovalRow).where(ApprovalRow.batch_id.in_(batch_ids))
                     .order_by(EVENT_ORDER)).scalars() if batch_ids else []
    return [ApprovalOut(batch_id=a.batch_id, version=a.batch_version,
                        approved_at=a.approved_at.isoformat(), revoked_at=_iso(a.revoked_at),
                        kind=a.kind) for a in rows]


def _contact(c: ContactCandidateRow) -> ContactOut:
    return ContactOut(id=c.contact_id, kind=c.kind, name=c.name,
                      professional_title=c.professional_title, channel=c.channel,
                      address=c.address, public_source_url=c.public_source_url,
                      confidence=c.confidence, verified_at=_iso(c.verified_at))


def _outreach(s: Session, application_id: str) -> tuple[list[OutreachOut], list[ContactOut]]:
    contacts = list(s.execute(select(ContactCandidateRow).where(
        ContactCandidateRow.application_id == application_id)
        .order_by(ContactCandidateRow.created_at, ContactCandidateRow.contact_id)).scalars())
    drafts = []
    for d in s.execute(select(OutreachDraftRow).where(
            OutreachDraftRow.application_id == application_id)
            .order_by(OutreachDraftRow.created_at)).scalars():
        c = s.get(ContactCandidateRow, d.contact_id) if d.contact_id else None
        drafts.append(OutreachOut(
            id=d.outreach_id, campaign_id=d.campaign_id, status=d.status,
            skip_reason=d.skip_reason, channel=d.channel, message_text=d.message_text,
            contact=_contact(c) if c is not None else None, sent_at=_iso(d.sent_at),
            sent_note=d.sent_note))
    return drafts, [_contact(c) for c in contacts]


def export_application(cfg: AppConfig, s: Session, application_id: str, *,
                       now: Callable[[], datetime] = utcnow,
                       out_dir: Path | None = None) -> Path:
    """Write <out_dir or storage/exports>/application-<id>-<ts>.zip and return its path."""
    app = s.get(ApplicationRow, application_id)
    if app is None:
        raise LookupError(f"application {application_id} not found")
    job = s.get(JobRow, app.job_id)
    if job is None:
        raise LookupError(f"job for application {application_id} not found")
    moment = now()
    docs: list[tuple[Path, str]] = []
    artifacts: list[ArtifactOut] = []
    for a in s.execute(select(DocumentArtifactRow).where(
            DocumentArtifactRow.application_id == application_id)
            .order_by(DocumentArtifactRow.type, DocumentArtifactRow.version)).scalars():
        src = contained_artifact_path(cfg.storage_path, a.path, application_id)
        in_zip: str | None = None
        if src is not None:
            in_zip = "documents/" + src.relative_to(
                (cfg.storage_path / "artifacts" / application_id).resolve()).as_posix()
            if all(z != in_zip for _p, z in docs):
                docs.append((src, in_zip))
        artifacts.append(ArtifactOut(id=a.artifact_id, type=a.type, version=a.version,
                                     content_hash=a.content_hash, path_in_zip=in_zip))
    click = last_event(s, "application", application_id, SUBMIT_CLICKED)
    outreach, contacts = _outreach(s, application_id)
    bundle = ApplicationBundle(
        exported_utc=moment.isoformat(),
        job=JobOut(id=job.job_id, title=job.title, company=job.company,
                   location=job.location, source=job.source,
                   canonical_url=job.canonical_url, application_url=job.application_url,
                   status=job.status),
        match=_match(s, job.job_id),
        application=ApplicationOut(
            status=app.status, portal=app.portal,
            confirmation_reference=app.confirmation_reference,
            timestamps={"submit_requested_at": _iso(app.submit_requested_at),
                        "submit_clicked_at": _iso(click.timestamp_utc) if click else None}),
        answers=[AnswerOut(field_id=x.field_id, label=x.label, decision=x.decision,
                           value=_json(x.proposed_value_json, None),
                           source_fact_id=x.source_fact_id)
                 for x in s.execute(select(FormFieldAnswerRow).where(
                     FormFieldAnswerRow.application_id == application_id)
                     .order_by(FormFieldAnswerRow.field_id)).scalars()],
        artifacts=artifacts,
        approvals=_approvals(s, app),
        outreach=outreach, contacts=contacts,
        events=[EventOut(timestamp_utc=e.timestamp_utc.isoformat(),
                         entity_type=e.entity_type, entity_id=e.entity_id, actor=e.actor,
                         action=e.action, outcome=e.outcome,
                         redacted_details=_json(e.redacted_details_json, {}),
                         error_code=e.error_code)
                for e in s.execute(select(AuditEventRow).where(
                    AuditEventRow.entity_id == application_id)
                    .order_by(EVENT_ORDER)).scalars()])
    out_dir = exports_dir(cfg) if out_dir is None else out_dir
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    final = out_dir / f"application-{application_id}-{stamp}.zip"
    n = 1
    while final.exists():
        n += 1
        final = out_dir / f"application-{application_id}-{stamp}-{n}.zip"
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".zip", dir=out_dir)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            payload = bundle.model_dump(mode="json")
            payload["schema"] = payload.pop("schema_version")
            z.writestr("application.json", json.dumps(payload, indent=2, sort_keys=True))
            for src, in_zip in docs:
                z.write(src, in_zip)
        tmp.chmod(0o600)
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return final


def _plain(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def export_all(cfg: AppConfig, s: Session, *, now: Callable[[], datetime] = utcnow) -> Path:
    """Write storage/exports/export-all-<ts>/ with one JSON file per table plus profiles/
    and artifacts/. Returns the directory."""
    out_dir = exports_dir(cfg)
    stamp = now().strftime("%Y%m%d-%H%M%S")
    final = out_dir / f"export-all-{stamp}"
    n = 1
    while final.exists():
        n += 1
        final = out_dir / f"export-all-{stamp}-{n}"
    work = Path(tempfile.mkdtemp(prefix=".tmp-", dir=out_dir))
    try:
        work.chmod(0o700)
        for table in Base.metadata.sorted_tables:
            rows = [{k: _plain(v) for k, v in r._mapping.items()}
                    for r in s.execute(table.select())]
            f = work / f"{table.name}.json"
            f.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
            f.chmod(0o600)
        for tree in TREES:
            if (cfg.storage_path / tree).is_dir():
                _copy_tree(cfg.storage_path / tree, work / tree)
        os.replace(work, final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return final
