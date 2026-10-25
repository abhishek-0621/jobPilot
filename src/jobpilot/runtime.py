"""Wire the DISCOVER..VALIDATE agents, handlers and their tools for a running app."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents.cover_letter import CoverLetterAgent, make_cover_letter_handler
from jobpilot.agents.essay import LIBRARY_PREFIX as ESSAY_LIBRARY_PREFIX
from jobpilot.agents.essay import EssayContext
from jobpilot.agents.extract import ExtractAgent, extract_handler
from jobpilot.agents.form_prep import (
    PrepareFormAgent,
    ValidateAgent,
    make_form_opener,
    make_validate_handler,
    prepare_form_handler,
)
from jobpilot.agents.manual_package import ManualPackageAgent, manual_package_handler
from jobpilot.agents.match import MatchAgent, make_match_handler
from jobpilot.agents.outreach import OutreachAgent, make_outreach_handler, make_outreach_loader
from jobpilot.agents.tailor import TailorAgent, make_tailor_handler
from jobpilot.config import AppConfig
from jobpilot.contracts.enums import FieldDecision
from jobpilot.contracts.models import FieldAnswer
from jobpilot.data.db import utcnow
from jobpilot.data.tables import (
    AnswerLibraryRow,
    ApplicationRow,
    CandidateProfileRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
    JobRow,
    MatchAssessmentRow,
)
from jobpilot.discovery.finder import DiscoverAgent, discover_handler
from jobpilot.discovery.sources.ashby import AshbySource
from jobpilot.discovery.sources.base import RateLimitedFetcher, SourceAdapter
from jobpilot.discovery.sources.greenhouse import GreenhouseSource
from jobpilot.discovery.sources.lever import LeverSource
from jobpilot.discovery.sources.smartrecruiters import SmartRecruitersSource
from jobpilot.discovery.sources.workday import WorkdaySource
from jobpilot.documents.model import split_skills
from jobpilot.documents.validate import (
    BANNED_PATH,
    TECH_TERMS_PATH,
    has_phrase,
    load_banned,
    load_tech_terms,
)
from jobpilot.forms.model import BoundArtifacts, MappingContext, load_aliases
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.documents_flow import active_profile_version
from jobpilot.orchestrator.engine import Agent, Handler, OutputRejected, Provided, TaskKind
from jobpilot.outreach.contacts import FoundContact, fetch_careers_inbox
from jobpilot.portals.base import PortalAdapter
from jobpilot.portals.live_submit import LiveSubmitter, SubmitContext, make_before_click
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.profile.importer import PROFILE_ID, active_profile, candidate_years, verified_facts
from jobpilot.profile.preferences import load_companies, load_search_preferences
from jobpilot.profile.schema import ProfileFile, load_profile_file
from jobpilot.profile.stories import StoredBank, stored_bank


@dataclass
class Runtime:
    agents: dict[TaskKind, Agent[Any, Any]]
    handlers: dict[TaskKind, Handler]
    adapters: dict[str, PortalAdapter]
    tools: dict[str, Any]
    client: httpx.AsyncClient
    owns_client: bool = field(default=False)
    submitter_factory: Callable[[str], PortalAdapter] | None = None

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


def _own_employers(s: Session) -> set[str]:
    """Casefolded employer names from the active profile's roles and current employment."""
    try:
        version, _ = active_profile(s)
    except LookupError:
        return set()
    row = s.get(CandidateProfileRow, (PROFILE_ID, version))
    prefs = json.loads(row.preferences_json) if row else {}
    names = list(prefs.get("employers") or [])
    names.append((prefs.get("current_employment") or {}).get("employer") or "")
    return {n.strip().casefold() for n in names if n and n.strip()}


def _bound_file(storage_path: Path, s: Session, application_id: str,
                artifact_id: str | None) -> tuple[Path | None, int | None]:
    """Path (only if it resolves inside storage/artifacts/<app>) and profile version of
    a bound artifact."""
    row = None if artifact_id is None else s.get(DocumentArtifactRow, artifact_id)
    if row is None or row.application_id != application_id:
        return None, None
    path = (storage_path / row.path).resolve()
    root = (storage_path / "artifacts" / application_id).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None, row.profile_version
    return path, row.profile_version


def bound_artifacts(storage_path: Path, s: Session, app: ApplicationRow) -> dict[str, Any]:
    """The application's bound resume / cover letter: paths, profile versions and the
    cover letter's text (its sibling ``.txt`` artifact)."""
    resume, resume_v = _bound_file(storage_path, s, app.application_id,
                                   app.resume_artifact_id)
    cover, cover_v = _bound_file(storage_path, s, app.application_id,
                                 app.cover_letter_artifact_id)
    text = None
    if cover is not None:
        txt = cover.with_suffix(".txt")
        rel = txt.relative_to(storage_path.resolve()).as_posix()
        known = s.query(DocumentArtifactRow).filter(
            DocumentArtifactRow.application_id == app.application_id,
            DocumentArtifactRow.type == "cover_letter_text",
            DocumentArtifactRow.path == rel).first()
        if known is not None and txt.is_file():
            text = txt.read_text(encoding="utf-8")
    return {"resume_artifact_id": app.resume_artifact_id,
            "cover_letter_artifact_id": app.cover_letter_artifact_id,
            "resume_path": None if resume is None else str(resume),
            "cover_letter_path": None if cover is None else str(cover),
            "resume_profile_version": resume_v, "cover_letter_profile_version": cover_v,
            "cover_letter_text": text}


def make_application_loader(
    session_factory: sessionmaker[Session], storage_path: Path = Path("storage"),
) -> Callable[[str], dict | None]:
    """Application + job (requirements, summary, URLs) + match evidence as
    {fact_id: value}, restricted to verified facts of the active profile; plus the
    current resume's source fact ids, the other companies present in the jobs table and
    the bound artifacts (see ``bound_artifacts``)."""
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
            bound = bound_artifacts(storage_path, s, app)
            own = _own_employers(s)
            others = sorted({c for (c,) in s.query(JobRow.company).filter(
                JobRow.company != job.company).distinct()
                if c and c.casefold() not in own
                and not any(has_phrase(v, c) for v in facts.values())})
            return {"application_id": app.application_id, "job_id": job.job_id,
                    "status": app.status, "title": job.title, "company": job.company,
                    "requirements": req, "job_summary": req.get("summary") or "",
                    "match_evidence": {i: facts[i] for i in ids if i in facts},
                    "resume_fact_ids": [i for i in resume_ids if i in facts],
                    "other_companies": others,
                    "source": job.source, "canonical_url": job.canonical_url,
                    "application_url": job.application_url,
                    "form_snapshot_hash": app.form_snapshot_hash, **bound}
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


def make_mapping_context(cfg: AppConfig, session_factory: sessionmaker[Session],
                         clock: Callable[[], datetime]) -> Callable[[str], MappingContext]:
    """Profile identity/links/employment/education, the active answer library, the
    bound artifacts, field aliases and total years for one application."""
    profile_file = make_profile_file(cfg)

    def mapping_context(application_id: str) -> MappingContext:
        pf = profile_file()
        with session_factory() as s:
            app = s.get(ApplicationRow, application_id)
            if app is None:
                raise OutputRejected("application_not_found")
            try:
                version, _ = active_profile(s)
            except LookupError as e:
                raise OutputRejected("no_profile") from e
            library = {r.question_key: (json.loads(r.value_json),
                                        FieldDecision(r.decision_default))
                       for r in s.query(AnswerLibraryRow).filter(
                           AnswerLibraryRow.profile_version == version)}
            try:
                years: float | None = candidate_years(s, clock())
            except ValueError:
                years = None
            bound = bound_artifacts(cfg.storage_path, s, app)
        artifacts = BoundArtifacts(
            resume_path=bound["resume_path"], cover_letter_path=bound["cover_letter_path"],
            cover_letter_text=bound["cover_letter_text"])
        return MappingContext(
            identity=pf.identity, links=pf.links, current_employment=pf.current_employment,
            education=pf.education, languages=pf.languages, library=library,
            artifacts=artifacts, aliases=load_aliases(cfg.field_aliases_path),
            years_experience=years)
    return mapping_context


def make_essay_context(session_factory: sessionmaker[Session]) -> Callable[[str], EssayContext]:
    """Verified stories, the user's canned answers and standing rules, verified facts and
    skills, the job's title/company/requirements and the essay answers saved to the
    library, for one application. Unverified stories never leave this loader."""
    def essay_context(application_id: str) -> EssayContext:
        with session_factory() as s:
            app = s.get(ApplicationRow, application_id)
            job = None if app is None else s.get(JobRow, app.job_id)
            if app is None or job is None:
                raise OutputRejected("application_not_found")
            try:
                data = json.loads(job.extracted_json or "{}")
            except ValueError:
                data = {}
            req = data.get("requirements") if isinstance(data, dict) else None
            req = req if isinstance(req, dict) else {}
            try:
                version, _ = active_profile(s)
                facts = verified_facts(s)
            except LookupError:
                version, facts = None, []
            library = {} if version is None else {
                r.question_key: json.loads(r.value_json)
                for r in s.query(AnswerLibraryRow).filter(
                    AnswerLibraryRow.profile_version == version,
                    AnswerLibraryRow.question_key.like(ESSAY_LIBRARY_PREFIX + "%"))}
            bank = stored_bank(s)
            bank = StoredBank(stories=[st for st in bank.stories if st.verified],
                              standing_rules=bank.standing_rules, canned=bank.canned)
            return EssayContext(
                bank=bank,
                facts={f.fact_id: f.value for f in facts if f.category != "skill"},
                skills=[x for f in facts if f.category == "skill"
                        for x in split_skills(f.value)],
                job={"title": job.title, "company": job.company, "requirements": req,
                     "job_summary": req.get("summary") or ""},
                library=library)
    return essay_context


def make_stored_answers(
    session_factory: sessionmaker[Session],
) -> Callable[[str], list[FieldAnswer]]:
    def stored_answers(application_id: str) -> list[FieldAnswer]:
        with session_factory() as s:
            rows = s.query(FormFieldAnswerRow).filter(
                FormFieldAnswerRow.application_id == application_id).order_by(
                FormFieldAnswerRow.field_id).all()
            return [FieldAnswer(
                field_id=r.field_id, label=r.label, field_type=r.field_type,
                proposed_value=json.loads(r.proposed_value_json),
                decision=FieldDecision(r.decision), source_fact_id=r.source_fact_id,
                confidence=r.confidence, rationale=r.rationale) for r in rows]
    return stored_answers


def make_active_version(session_factory: sessionmaker[Session]) -> Callable[[], int | None]:
    def active_version() -> int | None:
        with session_factory() as s:
            return active_profile_version(s)
    return active_version


def make_artifact_dir(cfg: AppConfig) -> Callable[[str], Path]:
    def artifact_dir(application_id: str) -> Path:
        d = cfg.storage_path / "artifacts" / application_id
        d.mkdir(parents=True, exist_ok=True)
        return d
    return artifact_dir


def _intact(storage_path: Path, s: Session, artifact_id: str | None) -> bool:
    row = None if artifact_id is None else s.get(DocumentArtifactRow, artifact_id)
    if row is None:
        return artifact_id is None
    path = storage_path / row.path
    return path.is_file() and \
        "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == row.content_hash


def make_submit_loader(
    session_factory: sessionmaker[Session], storage_path: Path,
) -> Callable[[str], SubmitContext | None]:
    """Everything the live pre-submit check compares, read fresh from the DB."""
    load_app = make_application_loader(session_factory, storage_path)
    stored = make_stored_answers(session_factory)

    def submit_loader(application_id: str) -> SubmitContext | None:
        app = load_app(application_id)
        if app is None:
            return None
        with session_factory() as s:
            row = s.get(ApplicationRow, application_id)
            assert row is not None
            intact = all(_intact(storage_path, s, a) for a in
                         (row.resume_artifact_id, row.cover_letter_artifact_id))
            return SubmitContext(
                app=app, answers=stored(application_id),
                application_hash=ApprovalService(s).application_hash(application_id),
                active_version=active_profile_version(s), documents_intact=intact)
    return submit_loader


def make_submitter_factory(
    cfg: AppConfig, session_factory: sessionmaker[Session],
) -> Callable[[str], PortalAdapter]:
    """Only used by the engine when dry_run is off AND the portal is in live_portals."""
    loader = make_submit_loader(session_factory, cfg.storage_path)
    before_click = make_before_click(session_factory)

    def submitter_factory(portal: str) -> PortalAdapter:
        return LiveSubmitter(cfg, portal, loader, before_click, session_factory)
    return submitter_factory


def build_sources(client: httpx.AsyncClient) -> dict[str, SourceAdapter]:
    """The one registry of discovery source adapters (runtime and `sources check`)."""
    return {
        "greenhouse": GreenhouseSource(client),
        "lever": LeverSource(client),
        "ashby": AshbySource(client),
        "smartrecruiters": SmartRecruitersSource(client),
        "workday": WorkdaySource(client),
    }


def make_careers_inbox(
    client: httpx.AsyncClient,
) -> Callable[[str | None, str | None], Any]:
    """One rate-limited GET of a configured careers page (same User-Agent as sources)."""
    fetcher = RateLimitedFetcher(client)

    async def careers_inbox(url: str | None, domain: str | None) -> list[FoundContact]:
        return await fetch_careers_inbox(url, fetcher, domain)
    return careers_inbox


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
        "sources": build_sources(http),
        "prefs": prefs,
        "companies": Provided(lambda: load_companies(cfg)),
        "clock": clock,
        "job_loader": make_job_loader(session_factory),
        "profile_loader": make_profile_loader(session_factory, clock),
        "application_loader": make_application_loader(session_factory, cfg.storage_path),
        "mapping_context": make_mapping_context(cfg, session_factory, clock),
        "stored_answers": make_stored_answers(session_factory),
        "essay_context": make_essay_context(session_factory),
        "active_profile_version": make_active_version(session_factory),
        "form_opener": make_form_opener(cfg),
        "profile_file": make_profile_file(cfg),
        "artifact_dir": make_artifact_dir(cfg),
        "tech_terms": lambda: load_tech_terms(TECH_TERMS_PATH),
        "cover_letter_banned": lambda: load_banned(BANNED_PATH),
        "outreach_context": make_outreach_loader(session_factory, cfg),
        "careers_inbox": make_careers_inbox(http),
    }
    agents: dict[TaskKind, Agent[Any, Any]] = {
        TaskKind.DISCOVER: DiscoverAgent(),
        TaskKind.EXTRACT: ExtractAgent(),
        TaskKind.MATCH: MatchAgent(),
        TaskKind.TAILOR: TailorAgent(),
        TaskKind.COVER_LETTER: CoverLetterAgent(),
        TaskKind.PREPARE_FORM: PrepareFormAgent(),
        TaskKind.MANUAL_PACKAGE: ManualPackageAgent(),
        TaskKind.VALIDATE: ValidateAgent(),
        TaskKind.OUTREACH_RESEARCH: OutreachAgent(),
    }
    handlers: dict[TaskKind, Handler] = {
        TaskKind.DISCOVER: discover_handler,
        TaskKind.EXTRACT: extract_handler,
        TaskKind.MATCH: make_match_handler(prefs),
        TaskKind.TAILOR: make_tailor_handler(cfg.storage_path),
        TaskKind.COVER_LETTER: make_cover_letter_handler(cfg.storage_path),
        TaskKind.PREPARE_FORM: prepare_form_handler,
        TaskKind.MANUAL_PACKAGE: manual_package_handler,
        TaskKind.VALIDATE: make_validate_handler(cfg.storage_path),
        TaskKind.OUTREACH_RESEARCH: make_outreach_handler(cfg),
    }
    return Runtime(agents=agents, handlers=handlers,
                   adapters={"simulated": SimulatedAdapter()}, tools=tools,
                   client=http, owns_client=owns,
                   submitter_factory=make_submitter_factory(cfg, session_factory))
