"""When an application counts as applied for outreach, and the per-company draft lookup.

Only a real application counts: a manual application the user marked as applied, or a
CONFIRMED submission that was not simulated. A dry-run (simulated) confirmation never
leads to outreach, so no draft ever refers to an application that was not made.
"""

import json
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ApplicationStatus
from jobpilot.data.tables import ApplicationRow, JobRow, OutreachDraftRow, TaskRow
from jobpilot.observability.events import last_event
from jobpilot.outreach.contacts import company_slug

USER_MARKED_APPLIED = "user_marked_applied"
RESEARCH_KIND = "OUTREACH_RESEARCH"
_LIVE_TASK = ("QUEUED", "RUNNING")


def _details(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def applied_for_real(s: Session, app: ApplicationRow) -> bool:
    if app.status == ApplicationStatus.SUBMITTED_BY_USER.value:
        return True
    if app.status != ApplicationStatus.CONFIRMED.value:
        return False
    started = last_event(s, "application", app.application_id, "submit_started")
    return started is not None and _details(started.redacted_details_json).get(
        "simulated") is False


def recorded_applied_on(s: Session, app: ApplicationRow) -> str | None:
    """ISO date of the recorded submission event, or None when nothing was recorded."""
    if app.status == ApplicationStatus.SUBMITTED_BY_USER.value:
        ev = last_event(s, "application", app.application_id, USER_MARKED_APPLIED)
        if ev is None:
            return None
        day = _details(ev.redacted_details_json).get("applied_on")
        if isinstance(day, str):
            try:
                return date.fromisoformat(day).isoformat()
            except ValueError:
                return None
        return ev.timestamp_utc.date().isoformat()
    if app.status == ApplicationStatus.CONFIRMED.value:
        ev = last_event(s, "application", app.application_id, "submit_finished")
        return ev.timestamp_utc.date().isoformat() if ev is not None else None
    return None


_SUFFIXES = frozenset({"inc", "llc", "ltd", "limited", "pvt", "private", "gmbh", "corp",
                       "corporation", "co", "plc", "technologies", "technology", "labs",
                       "india", "ai", "software", "solutions", "group", "sa", "bv", "ag",
                       "llp", "company"})


def company_key(company: str) -> str:
    """Normalized company identity for dedupe: legal/common suffixes are dropped
    ("Acme, Inc." == "Acme"), keeping at least one token."""
    tokens = re.findall(r"[a-z0-9]+", company.casefold())
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return "".join(tokens) or company_slug(company) or company.casefold().strip()


def app_company_key(s: Session, app: ApplicationRow) -> str | None:
    job = s.get(JobRow, app.job_id)
    return company_key(job.company) if job is not None and job.company else None


def existing_draft(s: Session, key: str, campaign: str) -> OutreachDraftRow | None:
    return s.execute(select(OutreachDraftRow).where(
        OutreachDraftRow.company_key == key,
        OutreachDraftRow.campaign_id == campaign)).scalars().first()


def research_pending(s: Session, application_id: str) -> bool:
    return s.execute(select(TaskRow.task_id).where(
        TaskRow.kind == RESEARCH_KIND, TaskRow.entity_id == application_id,
        TaskRow.status.in_(_LIVE_TASK))).first() is not None


def research_blocker(s: Session, app: ApplicationRow, cfg: AppConfig) -> str | None:
    """Why OUTREACH_RESEARCH may not be queued for ``app`` now (None: it may)."""
    if not applied_for_real(s, app):
        return "not_applied"
    key = app_company_key(s, app)
    if key is None:
        return "no_company"
    if existing_draft(s, key, cfg.outreach.campaign) is not None:
        return "duplicate_company"
    if research_pending(s, app.application_id):
        return "research_pending"
    return None
