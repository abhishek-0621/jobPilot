"""DISCOVER: fetch public boards, prefilter, persist listings, enqueue EXTRACT."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import JobStatus
from jobpilot.contracts.models import AgentOutput, JobListing
from jobpilot.data.repositories import upsert_job
from jobpilot.data.tables import JobRow, TaskRow
from jobpilot.discovery.normalize import html_to_text
from jobpilot.discovery.prefilter import prefilter
from jobpilot.discovery.sources.base import RawPosting, SourceAdapter, SourceError
from jobpilot.ids import new_id
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.engine import (
    AgentContext,
    FollowUp,
    OutputRejected,
    TaskInput,
    TaskKind,
)
from jobpilot.profile.preferences import CompanyBoard, SearchPreferences

_ACTOR = "job_finder"


class DiscoverOutput(AgentOutput):
    run_id: str
    listings: list[JobListing]
    source_status: dict[str, str]  # "<source>:<token>" -> "ok" | error code
    fetched: int = 0  # raw postings returned by sources, before prefilter


def _to_listing(p: RawPosting, task_id: str, now: datetime) -> JobListing:
    return JobListing(
        task_id=task_id, created_at=now, job_id=new_id("job"), source=p.source,
        source_job_id=p.source_job_id, canonical_url=p.canonical_url,
        application_url=p.application_url, company=p.company, title=p.title,
        location=p.location, description_text=html_to_text(p.description_html),
        posted_at=p.posted_at, first_seen_at=now, last_seen_at=now,
        extraction_confidence=0.0,
    )


class DiscoverAgent:
    name = "job_finder"
    prompt_version = "none"
    output_model = DiscoverOutput
    tools = ("sources", "prefs", "companies", "clock")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> DiscoverOutput:
        sources: dict[str, SourceAdapter] = ctx.tools["sources"]
        prefs: SearchPreferences = ctx.tools["prefs"]
        companies: list[CompanyBoard] = ctx.tools["companies"]
        clock: Callable[[], datetime] = ctx.tools["clock"]
        status: dict[str, str] = {}
        listings: list[JobListing] = []
        fetched = 0
        for board in companies:
            if len(listings) >= prefs.max_jobs_per_run:
                break
            key = f"{board.source}:{board.token}"
            adapter = sources.get(board.source)
            if adapter is None:
                status[key] = "unsupported_source"
                continue
            try:
                postings = await adapter.list_postings(board)
            except SourceError as e:
                status[key] = e.code.value
                continue
            status[key] = "ok"
            fetched += len(postings)
            for p in postings:
                if len(listings) >= prefs.max_jobs_per_run:
                    break
                keep, _reason = prefilter(p, prefs)
                if keep:
                    listings.append(_to_listing(p, ctx.task_id, clock()))
        return DiscoverOutput(task_id=ctx.task_id, created_at=clock(), run_id=inp.entity_id,
                              listings=listings, source_status=status, fetched=fetched)


def discover_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
    if not isinstance(out, DiscoverOutput) or out.run_id != task.entity_id:
        raise OutputRejected("discover output does not match task run")
    follow: list[FollowUp] = []
    new = duplicates = reseen = 0
    for listing in out.listings:
        known = listing.source_job_id is not None and s.execute(
            select(JobRow.job_id).where(JobRow.source == listing.source,
                                        JobRow.source_job_id == listing.source_job_id)
        ).first() is not None
        row, created = upsert_job(s, listing)
        if created:
            new += 1
            assert row.status == JobStatus.DISCOVERED.value
            follow.append((TaskKind.EXTRACT, "job", row.job_id))
        elif known:
            reseen += 1
        else:
            duplicates += 1
    record_event(
        s, entity_type="run", entity_id=out.run_id, actor=_ACTOR, action="discovery_run",
        outcome="ok",
        details={"counts": {"fetched": out.fetched, "kept": len(out.listings), "new": new,
                            "duplicates": duplicates, "reseen": reseen},
                 "source_status": out.source_status},
    )
    return follow
