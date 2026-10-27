from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents.match import make_match_handler
from jobpilot.config import AppConfig, ModelRoute
from jobpilot.contracts.enums import JobStatus
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import JobRow, TaskRow
from jobpilot.discovery.finder import DiscoverAgent
from jobpilot.orchestrator.engine import Orchestrator, Provided, TaskKind, TaskStatus
from jobpilot.profile.importer import import_profile, verify_fact
from jobpilot.profile.preferences import SearchPreferences
from jobpilot.profile.schema import load_profile_file
from jobpilot.runtime import build_runtime
from tests.factories import valid_match

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "profile_example.yaml"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "rt.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "storage", db_path=tmp_path / "rt.db",
                     model_routes={"a": ModelRoute(model_tag="m1")})


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))


def test_build_runtime_registers_kinds(factory: sessionmaker[Session], tmp_path: Path) -> None:
    rt = build_runtime(_cfg(tmp_path), factory, client=_client())
    kinds = {TaskKind.DISCOVER, TaskKind.EXTRACT, TaskKind.MATCH, TaskKind.TAILOR,
             TaskKind.COVER_LETTER, TaskKind.PREPARE_FORM, TaskKind.VALIDATE,
                 TaskKind.MANUAL_PACKAGE, TaskKind.OUTREACH_RESEARCH, TaskKind.COMPANY_RESEARCH}
    assert set(rt.agents) == kinds == set(rt.handlers)
    assert set(rt.adapters) == {"simulated"}
    assert set(rt.tools["sources"]) == {"greenhouse", "lever", "ashby", "smartrecruiters",
                                           "workday", "himalayas", "remotive", "remoteok", "jobicy",
                                           "workingnomads", "weworkremotely"}
    for kind, agent in rt.agents.items():
        assert set(agent.tools) <= set(rt.tools), kind
    assert not rt.owns_client


def test_profile_loader_returns_only_verified_active(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as s:
        import_profile(s, load_profile_file(FIXTURE))
        verify_fact(s, "fact_exp_001")
        import_profile(s, load_profile_file(FIXTURE))  # v2 is now active
        s.commit()
    rt = build_runtime(_cfg(tmp_path), factory, client=_client(), clock=lambda: NOW)
    version, facts, years = rt.tools["profile_loader"]()
    assert version == 2
    assert [f["fact_id"] for f in facts] == ["fact_exp_001"]
    assert set(facts[0]) == {"fact_id", "category", "field", "value"}
    assert years == 3.0


def test_prefs_and_companies_reload_per_task(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    prof = cfg.storage_path / "profiles"
    prof.mkdir(parents=True)
    (prof / "search_preferences.yaml").write_text(yaml.safe_dump(
        {"search": {"min_match_score": 55}}))
    (prof / "companies.yaml").write_text(yaml.safe_dump(
        {"companies": [{"name": "Example AI", "source": "greenhouse", "token": "ex"}]}))
    rt = build_runtime(cfg, factory, client=_client())
    assert isinstance(rt.tools["prefs"], Provided)
    assert rt.tools["prefs"].factory().min_match_score == 55
    (prof / "search_preferences.yaml").write_text(yaml.safe_dump(
        {"search": {"min_match_score": 80}}))
    assert rt.tools["prefs"].factory().min_match_score == 80
    assert [c.token for c in rt.tools["companies"].factory()] == ["ex"]


async def test_engine_resolves_provided_tools(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    seen: list[object] = []

    class Probe(DiscoverAgent):
        async def run(self, inp, ctx):  # type: ignore[no-untyped-def]
            seen.append(ctx.tools["prefs"])
            return await super().run(inp, ctx)

    cfg = _cfg(tmp_path)
    rt = build_runtime(cfg, factory, client=_client())
    orch = Orchestrator(factory, cfg, {TaskKind.DISCOVER: Probe()}, rt.adapters,
                        handlers=rt.handlers, tools=rt.tools)
    orch.enqueue(TaskKind.DISCOVER, "run", "run_1")
    assert await orch.run_once()
    assert isinstance(seen[0], SearchPreferences)
    with factory() as s:
        assert s.query(TaskRow).one().status == TaskStatus.SUCCEEDED.value


def test_match_handler_accepts_prefs_provider(db_session: Session) -> None:
    from jobpilot.contracts.models import MatchAssessment

    db_session.add(JobRow(job_id="job_1", source="greenhouse", source_job_id="1",
                          canonical_url="https://example.com/1", company="Example AI",
                          title="GenAI Engineer", description_text="d",
                          status=JobStatus.NORMALIZED.value, first_seen_at=NOW,
                          last_seen_at=NOW))
    db_session.flush()
    task = TaskRow(task_id="task_1", kind="MATCH", entity_type="job", entity_id="job_1",
                   status="RUNNING", attempts=1)
    out = MatchAssessment(**valid_match(job_id="job_1", total_score=60.0,
                                        matched_evidence_fact_ids=[]))
    handler = make_match_handler(Provided(lambda: SearchPreferences(min_match_score=50)))
    assert handler(db_session, task, out) == []
    assert db_session.get(JobRow, "job_1").status == JobStatus.MATCHED.value


async def test_malformed_profile_dates_reject_match(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    import json

    from jobpilot.agents.match import MatchAgent
    from jobpilot.data.tables import CandidateProfileRow
    from jobpilot.orchestrator.engine import AgentContext, OutputRejected, TaskInput

    with factory() as s:
        import_profile(s, load_profile_file(FIXTURE))
        verify_fact(s, "fact_exp_001")
        row = s.get(CandidateProfileRow, ("default", 1))
        prefs = json.loads(row.preferences_json)
        prefs["experience_ranges"] = [["garbage", "present"]]
        row.preferences_json = json.dumps(prefs)
        s.commit()
    rt = build_runtime(_cfg(tmp_path), factory, client=_client(), clock=lambda: NOW)
    inp = TaskInput(task_id="t", kind=TaskKind.MATCH, entity_type="job", entity_id="j",
                    attempt=1)
    tools = {**rt.tools, "job_loader": lambda _id: {
        "requirements": {"job_id": "j", "task_id": "t", "created_at": NOW.isoformat(),
                         "required_skills": [], "preferred_skills": [],
                         "experience_min_years": None, "experience_max_years": None,
                         "seniority": "senior", "work_mode": "hybrid", "location": None,
                         "role_family": "genai_llm", "summary": "x",
                         "extraction_confidence": 0.9}}}
    with pytest.raises(OutputRejected, match="bad_profile_dates"):
        await MatchAgent().run(inp, AgentContext("t", object(), tools))  # type: ignore[arg-type]
