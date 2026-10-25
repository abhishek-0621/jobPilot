"""Irreversible deletion: one finished application's data, or everything.

Audit history stays append-only: ``audit_event`` rows are never edited or removed; a
``data_deleted`` event with ids and counts only is added instead. The job row is kept.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ApplicationStatus
from jobpilot.data.mutations import clear_application_content
from jobpilot.data.tables import ApplicationRow, DocumentArtifactRow, FormFieldAnswerRow
from jobpilot.observability.events import record_event
from jobpilot.ops.backup import restore_marker, server_running
from jobpilot.ops.lock import ops_lock
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.state_machine import TERMINAL_APPLICATION, transition

DELETE_ALL_WORD = "DELETE ALL"
DELETE_ALL_TREES = ("profiles", "artifacts", "browser_state", "exports", "logs")
DATA_DELETED = "data_deleted"


class PurgeError(RuntimeError):
    """Base for refused deletions."""


class ConfirmationMismatch(PurgeError):
    """The typed confirmation did not match (HTTP 422)."""


class DeleteRefused(PurgeError):
    """The application or system is not in a state that allows deletion (HTTP 409)."""


def _app_artifact_dir(cfg: AppConfig, application_id: str) -> Path | None:
    """storage/artifacts/<id>, only when it resolves strictly inside storage/artifacts."""
    base = (cfg.storage_path / "artifacts").resolve()
    target = (base / application_id).resolve()
    if target == base or not target.is_relative_to(base):
        return None
    return target


def remove_artifacts(cfg: AppConfig, s: Session, application_id: str) -> tuple[int, int]:
    """Delete the application's artifact rows and its artifact directory.
    Returns (artifact rows removed, files removed). Caller commits."""
    rows = s.execute(select(func.count()).select_from(DocumentArtifactRow).where(
        DocumentArtifactRow.application_id == application_id)).scalar_one()
    s.execute(delete(DocumentArtifactRow).where(
        DocumentArtifactRow.application_id == application_id))
    files = 0
    target = _app_artifact_dir(cfg, application_id)
    if target is not None and target.is_dir():
        files = sum(1 for p in target.rglob("*") if p.is_file())
        shutil.rmtree(target)
    return rows, files


def delete_application(cfg: AppConfig, s: Session, application_id: str, *,
                       confirm: str) -> dict[str, int]:
    """Delete answers, documents and user content of one finished application, then
    mark it DELETED. ``confirm`` must equal the application id. Commits."""
    if confirm != application_id:
        raise ConfirmationMismatch("type the application id exactly to confirm deletion")
    with ops_lock(cfg):
        app = s.get(ApplicationRow, application_id)
        if app is None:
            raise LookupError(f"application {application_id} not found")
        status = ApplicationStatus(app.status)
        if status is ApplicationStatus.DELETED:
            raise DeleteRefused("this application's data is already deleted")
        if status not in TERMINAL_APPLICATION:
            hint = (" Resolve the uncertain submission first."
                    if status is ApplicationStatus.SUBMISSION_UNCERTAIN else "")
            raise DeleteRefused(f"cannot delete an application in {status.value}; only "
                                f"finished applications can be deleted.{hint}")
        answers = s.execute(select(func.count()).select_from(FormFieldAnswerRow).where(
            FormFieldAnswerRow.application_id == application_id)).scalar_one()
        s.execute(delete(FormFieldAnswerRow).where(
            FormFieldAnswerRow.application_id == application_id))
        artifacts = s.execute(select(func.count()).select_from(DocumentArtifactRow).where(
            DocumentArtifactRow.application_id == application_id)).scalar_one()
        s.execute(delete(DocumentArtifactRow).where(
            DocumentArtifactRow.application_id == application_id))
        ApprovalService(s).invalidate_application(application_id, "data_deleted")
        clear_application_content(s, application_id)
        transition(s, app, ApplicationStatus.DELETED, actor="user", reason="data_deleted")
        record_event(s, entity_type="application", entity_id=application_id, actor="user",
                     action=DATA_DELETED, outcome="ok",
                     details={"app_id": application_id, "answers_n": answers,
                              "artifacts_n": artifacts})
        s.commit()
        target = _app_artifact_dir(cfg, application_id)
        if target is not None and target.is_dir():
            shutil.rmtree(target)
    return {"answers_n": answers, "artifacts_n": artifacts}


def delete_all(cfg: AppConfig, *, confirm: str, include_backups: bool = False,
               server_running: Callable[[AppConfig], bool] = server_running) -> list[Path]:
    """Remove the database and every data tree under storage (CLI only). Backups stay
    unless ``include_backups``; the run/lock directory always stays. Returns what was
    removed."""
    if confirm != DELETE_ALL_WORD:
        raise ConfirmationMismatch(f'type "{DELETE_ALL_WORD}" exactly to confirm')
    if server_running(cfg):
        raise DeleteRefused("jobpilot start is running; stop it before deleting all data")
    if restore_marker(cfg).exists():
        raise DeleteRefused(f"a restore did not finish ({restore_marker(cfg)} exists); "
                            "sort that out before deleting all data")
    removed: list[Path] = []
    with ops_lock(cfg):
        for suffix in ("", "-wal", "-shm"):
            p = cfg.db_path.with_name(cfg.db_path.name + suffix)
            if p.exists():
                p.unlink()
                removed.append(p)
        trees = DELETE_ALL_TREES + (("backups",) if include_backups else ())
        for tree in trees:
            p = cfg.storage_path / tree
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
                removed.append(p)
            elif p.exists() or p.is_symlink():
                p.unlink()
                removed.append(p)
    return removed
