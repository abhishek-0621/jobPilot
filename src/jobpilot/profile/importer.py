"""Versioned profile import. Events carry counts only, never values."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import ApplicationStatus
from jobpilot.data.tables import (
    AnswerLibraryRow,
    ApplicationRow,
    CandidateFactRow,
    CandidateProfileRow,
    FormFieldAnswerRow,
)
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.profile.schema import ProfileFile
from jobpilot.util.hashing import canonical_json

PROFILE_ID = "default"
_ALIASES_PATH = Path(__file__).resolve().parents[3] / "config" / "field_aliases.yaml"
_DEFAULT_DECISION = "FILL_AND_REVIEW"


@dataclass(frozen=True)
class _Fact:
    fact_id: str
    category: str
    field: str
    value: str
    source: str | None


def _flatten(pf: ProfileFile) -> list[_Fact]:
    facts: list[_Fact] = []
    for e in pf.education:
        value = f"{e.degree} {e.field or ''}".strip() + f", {e.institution}"
        if e.end is not None:
            value += f" ({e.end})"
        facts.append(_Fact(e.fact_id, "education", e.degree, value, e.source))
    for r in pf.experience:
        facts += [_Fact(f.fact_id, "work_experience", r.title, f.value, f.source)
                  for f in r.facts]
    for p in pf.projects:
        facts += [_Fact(f.fact_id, "project", p.name, f.value, f.source) for f in p.facts]
    facts += [_Fact(f.fact_id, "achievement", "achievement", f.value, f.source)
              for f in pf.achievements]
    for group, items in pf.skills.items():
        facts.append(_Fact(f"fact_skill_{group}", "skill", group, ", ".join(items), None))
    return facts


def _decision_defaults() -> dict[str, str]:
    if not _ALIASES_PATH.exists():
        return {}
    data = yaml.safe_load(_ALIASES_PATH.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for spec in (data.get("fields") or {}).values():
        profile = spec.get("profile")
        if isinstance(profile, str) and profile.startswith("answer_library."):
            out[profile.split(".", 1)[1]] = spec.get("decision", _DEFAULT_DECISION)
    return out


def _latest_version(s: Session) -> int | None:
    return s.execute(select(func.max(CandidateProfileRow.version))).scalar()


def import_profile(s: Session, pf: ProfileFile, *, actor: str = "user") -> int:
    """Store ``pf`` as the next profile version; return the new version number.

    Fact rows are keyed by fact_id alone, so they are updated in place and re-stamped
    with the new version; facts that disappeared are removed.
    """
    previous = _latest_version(s)
    version = (previous or 0) + 1
    old = {f.fact_id: f for f in s.execute(select(CandidateFactRow)).scalars()}
    new = _flatten(pf)
    new_ids = {f.fact_id for f in new}

    changed = {fid for fid, row in old.items()
               if fid not in new_ids
               or next(f.value for f in new if f.fact_id == fid) != row.value}
    carried = 0
    for f in new:
        row = old.get(f.fact_id)
        unchanged = row is not None and row.value == f.value
        verified = bool(row and row.verified_by_user and unchanged)
        carried += verified
        if row is None:
            row = CandidateFactRow(fact_id=f.fact_id)
            s.add(row)
        row.profile_version = version
        row.category = f.category
        row.field = f.field
        row.value = f.value
        row.source_artifact = f.source
        row.verified_by_user = verified
        row.allowed_for_resume = f.category != "identity"
    for fid in set(old) - new_ids:
        s.delete(old[fid])

    prefs = {
        "default_resume": pf.default_resume,
        "current_employment": {
            "employer": pf.current_employment.employer,
            "title": pf.current_employment.title,
            "since": pf.current_employment.since,
            "company_start": pf.current_employment.company_start,
        },
        "languages": pf.languages,
    }
    s.add(CandidateProfileRow(profile_id=PROFILE_ID, version=version,
                              preferences_json=canonical_json(prefs)))
    defaults = _decision_defaults()
    answers = {k: v for k, v in pf.answer_library.items() if v is not None}
    for key, value in answers.items():
        s.add(AnswerLibraryRow(
            answer_id=new_id("ans"), profile_version=version, question_key=key,
            value_json=json.dumps(value), decision_default=defaults.get(key, _DEFAULT_DECISION),
        ))
    s.flush()

    affected = _affected_applications(s, changed)
    svc = ApprovalService(s)
    for app_id in affected:
        svc.invalidate_application(app_id, "profile_fact_changed")

    counts: dict[str, int] = {}
    for f in new:
        counts[f.category] = counts.get(f.category, 0) + 1
    record_event(s, entity_type="profile", entity_id=PROFILE_ID, actor=actor,
                 action="profile_imported", outcome="ok", details={
                     "version": version, "fact_counts": counts, "verified_carried": carried,
                     "answers": len(answers), "applications_invalidated": len(affected),
                     "open_questions": len(pf.open_questions)})
    return version


def _affected_applications(s: Session, changed: set[str]) -> list[str]:
    if not changed:
        return []
    rows = s.execute(
        select(ApplicationRow.application_id)
        .join(FormFieldAnswerRow, FormFieldAnswerRow.application_id == ApplicationRow.application_id)
        .where(
            ApplicationRow.status.in_([ApplicationStatus.APPROVED.value,
                                       ApplicationStatus.READY_FOR_REVIEW.value]),
            FormFieldAnswerRow.source_fact_id.in_(changed),
        ).distinct()
    ).scalars()
    return sorted(rows)


def active_profile(s: Session) -> tuple[int, list[CandidateFactRow]]:
    version = _latest_version(s)
    if version is None:
        raise LookupError("no profile imported; run `jobpilot profile import`")
    facts = list(s.execute(
        select(CandidateFactRow).where(CandidateFactRow.profile_version == version)
        .order_by(CandidateFactRow.fact_id)
    ).scalars())
    return version, facts


def verify_fact(s: Session, fact_id: str, verified: bool = True, *, actor: str = "user") -> None:
    version, _ = active_profile(s)
    row = s.get(CandidateFactRow, fact_id)
    if row is None or row.profile_version != version:
        raise LookupError(f"unknown fact {fact_id} in active profile")
    row.verified_by_user = verified
    record_event(s, entity_type="candidate_fact", entity_id=fact_id, actor=actor,
                 action="fact_verified", outcome="ok",
                 details={"fact_id": fact_id, "verified": verified})
    s.flush()


def verified_facts(s: Session) -> list[CandidateFactRow]:
    return [f for f in active_profile(s)[1] if f.verified_by_user]


def candidate_years(s: Session, now: datetime) -> float:
    version, _ = active_profile(s)
    row = s.get(CandidateProfileRow, (PROFILE_ID, version))
    start = json.loads(row.preferences_json)["current_employment"].get("company_start") \
        if row else None
    if not start:
        raise ValueError("profile has no current_employment.company_start")
    year, month = (int(p) for p in start.split("-"))
    return round(((now.year - year) * 12 + now.month - month) / 12, 1)
