"""DISCOVER: fetch public boards, prefilter, persist listings, enqueue EXTRACT."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import ValidationError
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
    skipped: int = 0  # postings that failed conversion to a JobListing


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
    timeout_s = 1800  # dozens of boards at >=1 s per host plus detail calls

    async def run(self, inp: TaskInput, ctx: AgentContext) -> DiscoverOutput:
        sources: dict[str, SourceAdapter] = ctx.tools["sources"]
        prefs: SearchPreferences = ctx.tools["prefs"]
        companies: list[CompanyBoard] = ctx.tools["companies"]
        clock: Callable[[], datetime] = ctx.tools["clock"]
        status: dict[str, str] = {}
        listings: list[JobListing] = []
        fetched = skipped = 0

        async def fetch_board(board: CompanyBoard) -> tuple[str, list[RawPosting] | str]:
            key = f"{board.source}:{board.token}"
            adapter = sources.get(board.source)
            if adapter is None:
                return key, "unsupported_source"
            try:
                async with gate:
                    if getattr(adapter, "supports_max_postings", False):
                        return key, await adapter.list_postings(  # type: ignore[call-arg]
                            board, max_postings=prefs.max_jobs_per_run)
                    return key, await adapter.list_postings(board)
            except SourceError as e:
                return key, e.code.value

        async def fetch_remote(name: str) -> tuple[str, list[RawPosting] | str]:
            key = f"board:{name}"
            remote = sources.get(name)
            if remote is None or getattr(remote, "kind", None) != "remote_board":
                return key, "unsupported_source"
            try:
                async with gate:
                    return key, await remote.list_board(  # type: ignore[attr-defined]
                        max_postings=prefs.max_jobs_per_run)
            except SourceError as e:
                return key, e.code.value

        # Boards on different hosts are fetched concurrently (each adapter still rate-limits
        # per host). Results keep board order so the run stays deterministic.
        gate = asyncio.Semaphore(BOARD_CONCURRENCY)
        results = await asyncio.gather(*[fetch_board(b) for b in companies],
                                       *[fetch_remote(n) for n in prefs.remote_boards])
        queues: list[list[RawPosting]] = []
        for key, res in results:
            if isinstance(res, str):
                status[key] = res
                continue
            status[key] = "ok"
            fetched += len(res)
            eligible = [p for p in res if prefilter(p, prefs)[0]]
            if eligible:
                queues.append(eligible)
        # Round-robin across boards so a large employer cannot use up the whole
        # max_jobs_per_run before smaller companies get a turn.
        depth = 0
        while len(listings) < prefs.max_jobs_per_run and any(depth < len(q) for q in queues):
            for q in queues:
                if depth >= len(q) or len(listings) >= prefs.max_jobs_per_run:
                    continue
                try:
                    listings.append(_to_listing(q[depth], ctx.task_id, clock()))
                except (ValidationError, ValueError):
                    skipped += 1
            depth += 1
        return DiscoverOutput(task_id=ctx.task_id, created_at=clock(), run_id=inp.entity_id,
                              listings=listings, source_status=status, fetched=fetched,
                              skipped=skipped)


BOARD_CONCURRENCY = 8
REMATCH_LIMIT = 200


def _unmatched_jobs(s: Session, exclude: set[str]) -> list[str]:
    """Extracted jobs that were never scored (e.g. MATCH refused before facts were verified)
    and have no MATCH task waiting. Each discovery run retries them, oldest first."""
    pending = select(TaskRow.entity_id).where(
        TaskRow.kind == TaskKind.MATCH.value, TaskRow.status.in_(("QUEUED", "RUNNING")))
    rows = s.execute(
        select(JobRow.job_id).where(JobRow.status == JobStatus.NORMALIZED.value,
                                    JobRow.job_id.not_in(pending))
        .order_by(JobRow.job_id).limit(REMATCH_LIMIT)).scalars()
    return [j for j in rows if j not in exclude]


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
            if row.status != JobStatus.DISCOVERED.value:
                raise OutputRejected("new job is not in DISCOVERED state")
            follow.append((TaskKind.EXTRACT, "job", row.job_id))
        elif known:
            reseen += 1
        else:
            duplicates += 1
    rematch = _unmatched_jobs(s, exclude={j for _, _, j in follow})
    follow.extend((TaskKind.MATCH, "job", job_id) for job_id in rematch)
    record_event(
        s, entity_type="run", entity_id=out.run_id, actor=_ACTOR, action="discovery_run",
        outcome="ok",
        details={"counts": {"fetched": out.fetched, "kept": len(out.listings), "new": new,
                            "duplicates": duplicates, "reseen": reseen,
                            "skipped": out.skipped, "rematch": len(rematch)},
                 "source_status": out.source_status},
    )
    return follow
