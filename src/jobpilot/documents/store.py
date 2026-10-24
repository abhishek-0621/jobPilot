"""Versioned document artifacts: one row per rendered file, hashed and fact-linked."""

import hashlib
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.data.tables import DocumentArtifactRow
from jobpilot.ids import new_id
from jobpilot.util.hashing import canonical_json


def next_version(s: Session, application_id: str, type_: str) -> int:
    current = s.execute(select(func.max(DocumentArtifactRow.version)).where(
        DocumentArtifactRow.application_id == application_id,
        DocumentArtifactRow.type == type_)).scalar_one_or_none()
    return (current or 0) + 1


def write_artifact(s: Session, application_id: str, type_: str, path: Path,
                   source_fact_ids: list[str], *, root: Path,
                   profile_version: int | None = None) -> DocumentArtifactRow:
    """Record ``path`` (must live under ``root``; stored relative to it). Flushes only."""
    resolved, base = path.resolve(), root.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError("artifact path outside storage root")
    data = resolved.read_bytes()
    row = DocumentArtifactRow(
        artifact_id=new_id("art"), application_id=application_id, type=type_,
        path=resolved.relative_to(base).as_posix(),
        content_hash="sha256:" + hashlib.sha256(data).hexdigest(),
        version=next_version(s, application_id, type_),
        source_fact_ids_json=canonical_json(list(dict.fromkeys(source_fact_ids))),
        profile_version=profile_version)
    s.add(row)
    s.flush()
    return row
