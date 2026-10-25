import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.contracts.enums import ErrorCode, JobStatus
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import list_events
from jobpilot.data.tables import JobRow, TaskRow
from jobpilot.discovery.finder import DiscoverAgent, DiscoverOutput, discover_handler
from jobpilot.discovery.sources.base import RawPosting, SourceError
from jobpilot.discovery.sources.greenhouse import GreenhouseSource
from jobpilot.discovery.sources.lever import LeverSource
from jobpilot.orchestrator.engine import AgentContext, TaskInput, TaskKind
from jobpilot.profile.preferences import CompanyBoard, SearchPreferences

FIX = Path(__file__).parent.parent / "fixtures"
T0 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
PREFS = SearchPreferences(target_titles=["AI Engineer"], locations=["Bengaluru"],
                          work_modes=["remote"], remote_boards=[])
GH = CompanyBoard(name="Example AI", source="greenhouse", token="exampleai")
LV = CompanyBoard(name="Sample Labs", source="lever", token="samplelabs")


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "f.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


class FailingSource:
    source = "greenhouse"

    async def list_postings(self, board: CompanyBoard) -> list[RawPosting]:
        raise SourceError(ErrorCode.SOURCE_UNAVAILABLE)


def _inp(run_id: str = "run_1") -> TaskInput:
    return TaskInput(task_id="task_1", kind=TaskKind.DISCOVER, entity_type="run",
                     entity_id=run_id, attempt=1)


def _ctx(sources: dict, companies: list[CompanyBoard],
         prefs: SearchPreferences = PREFS) -> AgentContext:
    return AgentContext(task_id="task_1", gateway=None, tools={
        "sources": sources, "prefs": prefs, "companies": companies, "clock": lambda: T0})


def _fixture_client() -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        name = "lever_postings.json" if req.url.host == "api.lever.co" else "greenhouse_jobs.json"
        return httpx.Response(200, text=(FIX / name).read_text())
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _discover(companies: list[CompanyBoard], prefs: SearchPreferences = PREFS,
                    failing_gh: bool = False) -> DiscoverOutput:
    async with _fixture_client() as c:
        sources = {"greenhouse": FailingSource() if failing_gh
                   else GreenhouseSource(c, min_interval_s=0),
                   "lever": LeverSource(c, min_interval_s=0)}
        return await DiscoverAgent().run(_inp(), _ctx(sources, companies, prefs))


async def test_discover_agent_builds_listings() -> None:
    out = await _discover([GH, LV])
    assert out.run_id == "run_1" and out.task_id == "task_1"
    assert out.source_status == {"greenhouse:exampleai": "ok", "lever:samplelabs": "ok"}
    assert out.fetched == 3
    titles = [li.title for li in out.listings]
    assert titles == ["Senior Applied ML Engineer", "GenAI Engineer"]
    gh = out.listings[0]
    assert gh.canonical_url == "https://boards.greenhouse.io/exampleai/jobs/1001"
    assert gh.description_text == "Build LLM systems.\n- Python"
    assert gh.extraction_confidence == 0.0 and gh.first_seen_at == T0
    assert gh.task_id == "task_1" and gh.source_job_id == "1001"
    assert gh.job_id.startswith("job")


async def test_one_source_failure_does_not_stop_others() -> None:
    unknown = CompanyBoard.model_construct(name="X", source="workday", token="x")
    out = await _discover([GH, unknown, LV], failing_gh=True)
    assert out.source_status == {
        "greenhouse:exampleai": ErrorCode.SOURCE_UNAVAILABLE.value,
        "workday:x": "unsupported_source",
        "lever:samplelabs": "ok",
    }
    assert [li.title for li in out.listings] == ["GenAI Engineer"]


async def test_discover_stops_at_max_jobs() -> None:
    out = await _discover([GH, LV], prefs=PREFS.model_copy(update={"max_jobs_per_run": 1}))
    assert len(out.listings) == 1


async def test_discover_handler_enqueues_extract_only_for_new(
        factory: sessionmaker[Session]) -> None:
    out = await _discover([GH, LV])
    task = TaskRow(task_id="task_1", kind="DISCOVER", entity_type="run", entity_id="run_1",
                   status="RUNNING", attempts=1)
    with factory() as s:
        follow = discover_handler(s, task, out)
        s.commit()
        jobs = s.execute(select(JobRow)).scalars().all()
        assert len(jobs) == 2 and all(j.status == JobStatus.DISCOVERED.value for j in jobs)
        assert sorted(follow) == sorted((TaskKind.EXTRACT, "job", j.job_id) for j in jobs)
        [ev] = [e for e in list_events(s, "run_1") if e.action == "discovery_run"]
        first_event = ev.event_id
        details = json.loads(ev.redacted_details_json)
        assert details["counts"] == {"fetched": 3, "kept": 2, "new": 2,
                                     "duplicates": 0, "reseen": 0, "skipped": 0}
        assert details["source_status"] == out.source_status

    # second run: same postings re-seen, plus a cross-posted duplicate of the lever job
    out2 = await _discover([GH, LV])
    dup = out2.listings[1].model_copy(update={
        "job_id": "job_dup", "source": "greenhouse", "source_job_id": "9999",
        "canonical_url": "https://boards.greenhouse.io/samplelabs/jobs/9999"})
    out2 = out2.model_copy(update={"listings": [*out2.listings, dup]})
    with factory() as s:
        follow = discover_handler(s, task, out2)
        s.commit()
        assert follow == []
        [ev] = [e for e in list_events(s, "run_1")
                if e.action == "discovery_run" and e.event_id != first_event]
        counts = json.loads(ev.redacted_details_json)["counts"]
        assert counts == {"fetched": 3, "kept": 3, "new": 0, "duplicates": 1, "reseen": 2,
                          "skipped": 0}


class BadPostingSource:
    source = "greenhouse"

    async def list_postings(self, board: CompanyBoard) -> list[RawPosting]:
        base = dict(source="greenhouse", company="Example AI", title="AI Engineer",
                    location="Bengaluru", description_html="<p>x</p>",
                    application_url=None, posted_at=None)
        return [
            RawPosting(source_job_id="bad", canonical_url="ftp://example.com/x", **base),
            RawPosting(source_job_id="ok", canonical_url="https://example.com/ok", **base),
        ]


async def test_bad_posting_skipped(factory: sessionmaker[Session]) -> None:
    ctx = _ctx({"greenhouse": BadPostingSource()}, [GH])
    out = await DiscoverAgent().run(_inp(), ctx)
    assert [li.source_job_id for li in out.listings] == ["ok"]
    assert out.skipped == 1 and out.fetched == 2
    task = TaskRow(task_id="task_1", kind="DISCOVER", entity_type="run", entity_id="run_1",
                   status="RUNNING", attempts=1)
    with factory() as s:
        discover_handler(s, task, out)
        [ev] = [e for e in list_events(s, "run_1") if e.action == "discovery_run"]
        assert json.loads(ev.redacted_details_json)["counts"]["skipped"] == 1


@pytest.mark.parametrize("token", ["a/b", "", "x" * 65, "a b", "../x"])
def test_company_board_token_validated(token: str) -> None:
    with pytest.raises(ValidationError):
        CompanyBoard(name="X", source="greenhouse", token=token)


def test_extract_task_kind() -> None:
    assert TaskKind.EXTRACT.value == "EXTRACT"


async def test_finder_passes_max_jobs_cap_to_capable_adapters() -> None:
    calls: list[dict] = []

    class Capped:
        source = "smartrecruiters"
        supports_max_postings = True

        async def list_postings(self, board: CompanyBoard, max_postings: int = 100):  # type: ignore[no-untyped-def]
            calls.append({"max_postings": max_postings})
            return []

    class Plain:
        source = "greenhouse"

        async def list_postings(self, board: CompanyBoard):  # type: ignore[no-untyped-def]
            calls.append({})
            return []

    boards = [CompanyBoard(name="S", source="smartrecruiters", token="s"), GH]
    prefs = PREFS.model_copy(update={"max_jobs_per_run": 7})
    await DiscoverAgent().run(_inp(), _ctx({"smartrecruiters": Capped(), "greenhouse": Plain()},
                                           boards, prefs))
    assert calls == [{"max_postings": 7}, {}]


async def test_finder_caps_real_sr_and_workday_detail_calls() -> None:
    from jobpilot.discovery.sources.smartrecruiters import SmartRecruitersSource
    from jobpilot.discovery.sources.workday import WorkdaySource

    sr_listing = json.loads((FIX / "smartrecruiters_postings.json").read_text())
    sr_detail = (FIX / "smartrecruiters_detail.json").read_text()
    wd_detail = (FIX / "workday_detail.json").read_text()
    details: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.smartrecruiters.com":
            if req.url.path.endswith("/postings"):
                content = [{**sr_listing["content"][0], "id": str(i)} for i in range(40)]
                return httpx.Response(200, json={**sr_listing, "totalFound": 40,
                                                 "content": content})
            details.append(req.url.path)
            return httpx.Response(200, text=sr_detail)
        if req.method == "POST":
            return httpx.Response(200, json={"total": 99, "jobPostings": [
                {"title": "ML Engineer", "externalPath": f"/job/X/J_{i}"} for i in range(20)]})
        details.append(req.url.path)
        return httpx.Response(200, text=wd_detail)

    boards = [
        CompanyBoard(name="S", source="smartrecruiters", token="s"),
        CompanyBoard(name="W", source="workday", token="w",
                     extra={"wd_host": "wd3", "site": "External"})]
    prefs = PREFS.model_copy(update={"max_jobs_per_run": 5})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        sources = {"smartrecruiters": SmartRecruitersSource(c, min_interval_s=0),
                   "workday": WorkdaySource(c, min_interval_s=0)}
        for board in boards:  # one run per board so the run-level stop cannot hide a cap
            await DiscoverAgent().run(_inp(), _ctx(sources, [board], prefs))
    assert len([d for d in details if "/v1/companies/" in d]) == 5
    assert len([d for d in details if "/wday/" in d]) == 5
