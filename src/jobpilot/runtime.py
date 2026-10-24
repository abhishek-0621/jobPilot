"""Wire the DISCOVER..COVER_LETTER agents, handlers and their tools for a running app."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents.cover_letter import CoverLetterAgent, make_cover_letter_handler
from jobpilot.agents.extract import ExtractAgent, extract_handler
from jobpilot.agents.match import MatchAgent, make_match_handler
from jobpilot.agents.tailor import TailorAgent, make_tailor_handler
from jobpilot.config import AppConfig
from jobpilot.data.db import utcnow
from jobpilot.data.tables import ApplicationRow, DocumentArtifactRow, JobRow, MatchAssessmentRow
from jobpilot.discovery.finder import DiscoverAgent, discover_handler
from jobpilot.discovery.sources.greenhouse import GreenhouseSource
from jobpilot.discovery.sources.lever import LeverSource
from jobpilot.documents.validate import (
    BANNED_PATH,
    TECH_TERMS_PATH,
    load_banned,
    load_tech_terms,
)
from jobpilot.orchestrator.engine import Agent, Handler, OutputRejected, Provided, TaskKind
from jobpilot.portals.base import PortalAdapter
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.profile.importer import active_profile, candidate_years, verified_facts
from jobpilot.profile.preferences import load_companies, load_search_preferences
from jobpilot.profile.schema import ProfileFile, load_profile_file


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
            facts = [{"fact_id": f.fact_id, "category": f.category, "field": f.field,
                      "value": f.value}
                     for f in verified_facts(s)
                     if f.verified_by_user and f.profile_version == version]
            try:
                years = candidate_years(s, clock())
            except ValueError as e:
                raise OutputRejected("bad_profile_dates") from e
            return version, facts, years
    return profile_loader


def make_application_loader(
    session_factory: sessionmaker[Session],
) -> Callable[[str], dict | None]:
    """Application + job (requirements, summary) + match evidence as {fact_id: value},
    restricted to verified facts of the active profile; plus the current resume's
    source fact ids and the other companies present in the jobs table."""
    def application_loader(application_id: str) -> dict | None:
        with session_factory() as s:
            app = s.get(ApplicationRow, application_id)
            job = None if app is None else s.get(JobRow, app.job_id)
            if app is None or job is None:
                return None
            try:
                data = json.loads(job.extracted_json or "{}")
            except ValueError:
                data = {}
            req = data.get("requirements") if isinstance(data, dict) else None
            req = req if isinstance(req, dict) else {}
            facts = {f.fact_id: f.value for f in verified_facts(s)}
            m = s.query(MatchAssessmentRow).filter(MatchAssessmentRow.job_id == job.job_id) \
                .order_by(MatchAssessmentRow.profile_version.desc()).first()
            try:
                ev = json.loads(m.evidence_json) if m is not None else {}
            except ValueError:
                ev = {}
            ids = ev.get("matched_evidence_fact_ids", []) if isinstance(ev, dict) else []
            resume = None if app.resume_artifact_id is None \
                else s.get(DocumentArtifactRow, app.resume_artifact_id)
            try:
                resume_ids = json.loads(resume.source_fact_ids_json) if resume else []
            except ValueError:
                resume_ids = []
            others = sorted({c for (c,) in s.query(JobRow.company).filter(
                JobRow.company != job.company).distinct() if c})
            return {"application_id": app.application_id, "job_id": job.job_id,
                    "status": app.status, "title": job.title, "company": job.company,
                    "requirements": req, "job_summary": req.get("summary") or "",
                    "match_evidence": {i: facts[i] for i in ids if i in facts},
                    "resume_fact_ids": [i for i in resume_ids if i in facts],
                    "other_companies": others}
    return application_loader


def make_profile_file(cfg: AppConfig) -> Callable[[], ProfileFile]:
    """Identity and role structure from the profile YAML (contents never logged)."""
    path = cfg.storage_path / "profiles" / "profile.yaml"

    def profile_file() -> ProfileFile:
        if not path.is_file():
            raise OutputRejected("no_profile_file")
        try:
            return load_profile_file(path)
        except ValueError as e:
            raise OutputRejected("bad_profile_file") from e
    return profile_file


def make_artifact_dir(cfg: AppConfig) -> Callable[[str], Path]:
    def artifact_dir(application_id: str) -> Path:
        d = cfg.storage_path / "artifacts" / application_id
        d.mkdir(parents=True, exist_ok=True)
        return d
    return artifact_dir


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
        "application_loader": make_application_loader(session_factory),
        "profile_file": make_profile_file(cfg),
        "artifact_dir": make_artifact_dir(cfg),
        "tech_terms": lambda: load_tech_terms(TECH_TERMS_PATH),
        "cover_letter_banned": lambda: load_banned(BANNED_PATH),
    }
    agents: dict[TaskKind, Agent[Any, Any]] = {
        TaskKind.DISCOVER: DiscoverAgent(),
        TaskKind.EXTRACT: ExtractAgent(),
        TaskKind.MATCH: MatchAgent(),
        TaskKind.TAILOR: TailorAgent(),
        TaskKind.COVER_LETTER: CoverLetterAgent(),
    }
    handlers: dict[TaskKind, Handler] = {
        TaskKind.DISCOVER: discover_handler,
        TaskKind.EXTRACT: extract_handler,
        TaskKind.MATCH: make_match_handler(prefs),
        TaskKind.TAILOR: make_tailor_handler(cfg.storage_path),
        TaskKind.COVER_LETTER: make_cover_letter_handler(cfg.storage_path),
    }
    return Runtime(agents=agents, handlers=handlers,
                   adapters={"simulated": SimulatedAdapter()}, tools=tools,
                   client=http, owns_client=owns)
