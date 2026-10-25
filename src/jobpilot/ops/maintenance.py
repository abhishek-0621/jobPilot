"""Retention and database upkeep (`jobpilot maintenance`, also after scheduled discovery).

Only applications in a terminal state are ever pruned, and pruning never changes their
status. Audit history is untouched; a ``maintenance_run`` event records counts only.
"""

import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import Engine, delete, inspect, select, text
from sqlalchemy.orm import Session

from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, utcnow
from jobpilot.data.mutations import detach_contacts
from jobpilot.data.tables import (
    ApplicationRow,
    ContactCandidateRow,
    DocumentArtifactRow,
    OutreachDraftRow,
)
from jobpilot.observability.events import last_event, record_event
from jobpilot.ops.backup import pre_restore_dirs, restore_marker
from jobpilot.ops.lock import ops_lock
from jobpilot.ops.purge import DeleteRefused, _app_artifact_dir, remove_artifacts
from jobpilot.orchestrator.state_machine import TERMINAL_APPLICATION

log = logging.getLogger(__name__)

MAINTENANCE_RUN = "maintenance_run"
ARTIFACTS_PRUNED = "artifacts_pruned"


class MaintenanceError(RuntimeError):
    pass


def _terminal_ids(s: Session) -> list[str]:
    return list(s.execute(select(ApplicationRow.application_id).where(
        ApplicationRow.status.in_([t.value for t in TERMINAL_APPLICATION]))).scalars())


def _prune_artifacts(cfg: AppConfig, s: Session, cutoff: datetime) -> int:
    pruned = 0
    for app_id in _terminal_ids(s):
        changed = last_event(s, "application", app_id, "status_changed")
        if changed is None or changed.timestamp_utc >= cutoff:
            continue
        has_rows = s.execute(select(DocumentArtifactRow.artifact_id).where(
            DocumentArtifactRow.application_id == app_id).limit(1)).first() is not None
        try:
            target = _app_artifact_dir(cfg, app_id)
            if not has_rows and target is None:
                continue
            rows, files = remove_artifacts(cfg, s, app_id)
        except DeleteRefused as e:
            log.warning("skipping artifacts of %s: %s", app_id, e)
            continue
        record_event(s, entity_type="application", entity_id=app_id, actor="maintenance",
                     action=ARTIFACTS_PRUNED, outcome="ok",
                     details={"app_id": app_id, "artifacts_n": rows, "files_n": files})
        s.commit()
        pruned += 1
    return pruned


def _prune_exports(cfg: AppConfig, cutoff: datetime) -> int:
    root = cfg.storage_path / "exports"
    if not root.is_dir():
        return 0
    n = 0
    for p in root.iterdir():
        if datetime.fromtimestamp(p.lstat().st_mtime, cutoff.tzinfo) >= cutoff:
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        n += 1
    return n


def _prune_pre_restore(cfg: AppConfig, cutoff: datetime) -> int:
    """Remove old storage/.pre-restore-* copies; never while a restore is unfinished."""
    if restore_marker(cfg).exists():
        return 0
    n = 0
    for p in pre_restore_dirs(cfg):
        if datetime.fromtimestamp(p.lstat().st_mtime, cutoff.tzinfo) >= cutoff:
            continue
        shutil.rmtree(p)
        n += 1
    return n


# Contacts a draft still needs (the user has not finished with it) are never pruned.
_UNDER_REVIEW = ("DRAFT_READY", "NEEDS_REVIEW", "APPROVED")


def _prune_contacts(engine: Engine, s: Session, cutoff: datetime) -> int:
    """Remove old contacts researched for finished applications; drafts that pointed at
    them are unbound first (their approval is revoked). Drafts and events stay."""
    insp = inspect(engine)
    if not insp.has_table("contact_candidate"):
        return 0
    cols = {c["name"] for c in insp.get_columns("contact_candidate")}
    if not {"created_at", "application_id"} <= cols:
        return 0
    terminal = [t.value for t in TERMINAL_APPLICATION]
    ids = list(s.execute(select(ContactCandidateRow.contact_id).where(
        ContactCandidateRow.created_at < cutoff,
        ContactCandidateRow.application_id.in_(select(ApplicationRow.application_id).where(
            ApplicationRow.status.in_(terminal))),
        ContactCandidateRow.contact_id.not_in(select(OutreachDraftRow.contact_id).where(
            OutreachDraftRow.contact_id.is_not(None),
            OutreachDraftRow.status.in_(_UNDER_REVIEW))))).scalars())
    if not ids:
        return 0
    detach_contacts(s, ids, reason="retention")
    s.execute(delete(ContactCandidateRow).where(ContactCandidateRow.contact_id.in_(ids)))
    s.commit()
    return len(ids)


def run_maintenance(cfg: AppConfig, *, now: Callable[[], datetime] = utcnow) -> dict[str, int]:
    """Prune old data of finished applications and old exports, optimize and check the DB.
    Raises MaintenanceError when the integrity check fails."""
    moment = now()
    days = cfg.retention_days
    with ops_lock(cfg):
        engine = make_engine(cfg.db_path)
        try:
            with make_session_factory(engine)() as s:
                counts = {
                    "artifacts_pruned": _prune_artifacts(
                        cfg, s, moment - timedelta(days=days.artifacts)),
                    "contacts_pruned": _prune_contacts(
                        engine, s, moment - timedelta(days=days.contacts)),
                    "exports_pruned": _prune_exports(
                        cfg, moment - timedelta(days=days.exports)),
                    "pre_restore_pruned": _prune_pre_restore(
                        cfg, moment - timedelta(days=days.artifacts)),
                }
                s.execute(text("PRAGMA optimize"))
                result = str(s.execute(text("PRAGMA integrity_check")).scalar())
                ok = result == "ok"
                record_event(s, entity_type="system", entity_id="maintenance",
                             actor="maintenance", action=MAINTENANCE_RUN,
                             outcome="ok" if ok else "failed",
                             details={**counts, "integrity_ok": ok})
                s.commit()
        finally:
            engine.dispose()
    if not ok:
        log.error("database integrity check failed: %s", result)
        raise MaintenanceError(f"database integrity check failed: {result}")
    return counts
