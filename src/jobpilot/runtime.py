"""Wire DISCOVER/EXTRACT/MATCH agents, handlers and their tools for a running app."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents.extract import ExtractAgent, extract_handler
from jobpilot.agents.match import MatchAgent, make_match_handler
from jobpilot.config import AppConfig
from jobpilot.data.db import utcnow
from jobpilot.data.tables import JobRow
from jobpilot.discovery.finder import DiscoverAgent, discover_handler
from jobpilot.discovery.sources.greenhouse import GreenhouseSource
from jobpilot.discovery.sources.lever import LeverSource
from jobpilot.orchestrator.engine import Agent, Handler, Provided, TaskKind
from jobpilot.portals.base import PortalAdapter
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.profile.importer import active_profile, candidate_years, verified_facts
from jobpilot.profile.preferences import load_companies, load_search_preferences


@dataclass
class Runtime:
    agents: dict[TaskKind, Agent[Any, Any]]
    handlers: dict[TaskKind, Handler]
    adapters: dict[str, PortalAdapter]
    tools: dict[str, Any]
    client: httpx.AsyncClient
    owns_client: bool = field(default=False)

    async def aclose(self) -> None:
        if self.owns_client:
            await self.client.aclose()


def make_job_loader(session_factory: sessionmaker[Session]) -> Callable[[str], dict | None]:
    def job_loader(job_id: str) -> dict | None:
        with session_factory() as s:
            j = s.get(JobRow, job_id)
            if j is None:
                return None
            try:
                data = json.loads(j.extracted_json or "{}")
            except ValueError:
                data = {}
            return {"job_id": j.job_id, "source": j.source, "title": j.title,
                    "company": j.company, "location": j.location, "work_mode": j.work_mode,
                    "status": j.status, "description_text": j.description_text,
                    "requirements": data.get("requirements") if isinstance(data, dict)
                    else None}
    return job_loader


def make_profile_loader(
    session_factory: sessionmaker[Session], clock: Callable[[], datetime],
) -> Callable[[], tuple[int, list[dict], float]]:
    """Only verified_by_user facts of the ACTIVE profile version ever leave this loader."""
    def profile_loader() -> tuple[int, list[dict], float]:
        with session_factory() as s:
            version, _ = active_profile(s)
            facts = [{"fact_id": f.fact_id, "category": f.category, "value": f.value}
                     for f in verified_facts(s)
                     if f.verified_by_user and f.profile_version == version]
            return version, facts, candidate_years(s, clock())
    return profile_loader


def build_runtime(
    cfg: AppConfig,
    session_factory: sessionmaker[Session],
    *,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> Runtime:
    owns = client is None
    http = client if client is not None else httpx.AsyncClient()
    prefs = Provided(lambda: load_search_preferences(cfg))
    tools: dict[str, Any] = {
        "sources": {"greenhouse": GreenhouseSource(http), "lever": LeverSource(http)},
        "prefs": prefs,
        "companies": Provided(lambda: load_companies(cfg)),
        "clock": clock,
        "job_loader": make_job_loader(session_factory),
        "profile_loader": make_profile_loader(session_factory, clock),
    }
    agents: dict[TaskKind, Agent[Any, Any]] = {
        TaskKind.DISCOVER: DiscoverAgent(),
        TaskKind.EXTRACT: ExtractAgent(),
        TaskKind.MATCH: MatchAgent(),
    }
    handlers: dict[TaskKind, Handler] = {
        TaskKind.DISCOVER: discover_handler,
        TaskKind.EXTRACT: extract_handler,
        TaskKind.MATCH: make_match_handler(prefs),
    }
    return Runtime(agents=agents, handlers=handlers,
                   adapters={"simulated": SimulatedAdapter()}, tools=tools,
                   client=http, owns_client=owns)
