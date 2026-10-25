import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.finder import DiscoverAgent
from jobpilot.discovery.sources.base import REMOTE_BOARDS, RawPosting, SourceError
from jobpilot.discovery.sources.himalayas import HimalayasSource
from jobpilot.discovery.sources.jobicy import JobicySource
from jobpilot.discovery.sources.remoteok import RemoteOKSource
from jobpilot.discovery.sources.remotive import RemotiveSource
from jobpilot.discovery.sources.workingnomads import WorkingNomadsSource
from jobpilot.orchestrator.engine import AgentContext, TaskInput, TaskKind
from jobpilot.portals.registry import apply_mode
from jobpilot.profile.preferences import SearchPreferences
from jobpilot.runtime import build_sources

FIX = Path(__file__).parent.parent / "fixtures" / "remote_boards"
UA = "JobPilot/0.1 (+personal job search; contact via repo)"


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _serve(name: str, seen: list[httpx.Request]) -> Any:
    body = (FIX / name).read_text()

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=body)
    return handler


async def test_himalayas_parses_and_paginates() -> None:
    seen: list[httpx.Request] = []
    async with _client(_serve("himalayas.json", seen)) as c:
        posts = await HimalayasSource(c, min_interval_s=0).list_board(max_postings=100)
    assert str(seen[0].url) == "https://himalayas.app/jobs/api?limit=20&offset=0"
    assert seen[0].headers["User-Agent"] == UA
    assert len(seen) == 1  # totalCount 2 < page size: no second page
    p, q = posts
    assert p.source == "himalayas" and p.company == "Example Corp"
    assert p.source_job_id == "https://himalayas.app/companies/example-corp/jobs/senior-ai-engineer"
    assert p.location == "India, Singapore"
    assert p.canonical_url == "https://himalayas.app/companies/example-corp/jobs/senior-ai-engineer"
    assert p.application_url.endswith("/apply")  # type: ignore[union-attr]
    assert p.posted_at == datetime.fromtimestamp(1792944497, UTC)
    assert q.location is None and q.application_url == q.canonical_url  # js: link dropped


async def test_himalayas_stops_after_five_pages() -> None:
    seen: list[httpx.Request] = []
    job = json.loads((FIX / "himalayas.json").read_text())["jobs"][0]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        off = int(req.url.params["offset"])
        jobs = [{**job, "guid": f"https://himalayas.app/j/{off + i}"} for i in range(20)]
        return httpx.Response(200, json={"totalCount": 10_000, "jobs": jobs})

    async with _client(handler) as c:
        posts = await HimalayasSource(c, min_interval_s=0).list_board(max_postings=1000)
    assert len(seen) == 5 and len(posts) == 100
    seen.clear()
    async with _client(handler) as c:
        posts = await HimalayasSource(c, min_interval_s=0).list_board(max_postings=30)
    assert len(seen) == 2 and len(posts) == 30


async def test_remotive_one_call() -> None:
    seen: list[httpx.Request] = []
    async with _client(_serve("remotive.json", seen)) as c:
        posts = await RemotiveSource(c, min_interval_s=0).list_board(max_postings=100)
    assert [str(r.url) for r in seen] == [
        "https://remotive.com/api/remote-jobs?category=software-dev"]
    p = posts[0]
    assert p.source == "remotive" and p.source_job_id == "1001" and p.location == "Worldwide"
    assert p.canonical_url == "https://remotive.com/remote-jobs/software-dev/ai-engineer-1001"
    assert p.posted_at == datetime(2026, 10, 22, 18, 40, 19, tzinfo=UTC)
    assert posts[1].location == "USA Only"


async def test_remoteok_skips_legal_notice() -> None:
    seen: list[httpx.Request] = []
    async with _client(_serve("remoteok.json", seen)) as c:
        posts = await RemoteOKSource(c, min_interval_s=0).list_board(max_postings=100)
    assert str(seen[0].url) == "https://remoteok.com/api"
    assert [p.source_job_id for p in posts] == ["2001", "2002"]
    p, q = posts
    assert p.title == "AI Engineer" and p.company == "Example Corp" and p.location == "Remote"
    assert p.application_url == "https://remoteok.com/remote-jobs/2001/apply"
    assert q.location is None and q.application_url == q.canonical_url


async def test_jobicy_two_tags_deduped() -> None:
    seen: list[httpx.Request] = []
    async with _client(_serve("jobicy.json", seen)) as c:
        posts = await JobicySource(c, min_interval_s=0).list_board(max_postings=100)
    assert [str(r.url) for r in seen] == [
        "https://jobicy.com/api/v2/remote-jobs?count=50&tag=python",
        "https://jobicy.com/api/v2/remote-jobs?count=50&tag=ai"]
    assert [p.source_job_id for p in posts] == ["3001", "3002"]
    assert posts[0].location == "Anywhere" and posts[1].location == "APAC"
    assert posts[1].posted_at == datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


async def test_workingnomads_parses_and_drops_bad_urls() -> None:
    seen: list[httpx.Request] = []
    async with _client(_serve("workingnomads.json", seen)) as c:
        posts = await WorkingNomadsSource(c, min_interval_s=0).list_board(max_postings=100)
    assert str(seen[0].url) == "https://www.workingnomads.com/api/exposed_jobs/"
    [p] = posts
    assert p.source == "workingnomads" and p.company == "Example Corp"
    assert p.source_job_id == "ai-engineer-example-corp"
    assert p.canonical_url == "https://workingnomads.com/jobs/ai-engineer-example-corp"


ADAPTERS = [HimalayasSource, RemotiveSource, RemoteOKSource, JobicySource, WorkingNomadsSource]


@pytest.mark.parametrize("cls", ADAPTERS)
@pytest.mark.parametrize(("status", "code"), [(429, ErrorCode.SOURCE_RATE_LIMIT),
                                               (500, ErrorCode.SOURCE_UNAVAILABLE)])
async def test_http_errors(cls: Any, status: int, code: ErrorCode) -> None:
    async with _client(lambda r: httpx.Response(status)) as c:
        with pytest.raises(SourceError) as e:
            await cls(c, min_interval_s=0).list_board(max_postings=10)
    assert e.value.code == code


@pytest.mark.parametrize("cls", ADAPTERS)
@pytest.mark.parametrize("body", ['{"unexpected": 1}', "[1, 2]", "not json"])
async def test_changed_shape(cls: Any, body: str) -> None:
    async with _client(lambda r: httpx.Response(200, text=body)) as c:
        with pytest.raises(SourceError) as e:
            await cls(c, min_interval_s=0).list_board(max_postings=10)
    assert e.value.code == ErrorCode.SOURCE_CHANGED


@pytest.mark.parametrize("cls", ADAPTERS)
async def test_max_postings_cap(cls: Any) -> None:
    name = {"HimalayasSource": "himalayas.json", "RemotiveSource": "remotive.json",
            "RemoteOKSource": "remoteok.json", "JobicySource": "jobicy.json",
            "WorkingNomadsSource": "workingnomads.json"}[cls.__name__]
    async with _client(_serve(name, [])) as c:
        posts = await cls(c, min_interval_s=0).list_board(max_postings=1)
    assert len(posts) == 1
    assert cls.supports_max_postings and cls.kind == "remote_board"


async def test_feed_text_is_capped() -> None:
    job = json.loads((FIX / "remotive.json").read_text())["jobs"][0]
    big = {**job, "description": "<p>" + "x" * 500_000 + "</p>", "title": "T" * 5000}
    async with _client(lambda r: httpx.Response(200, json={"jobs": [big]})) as c:
        [p] = await RemotiveSource(c, min_interval_s=0).list_board(max_postings=5)
    assert len(p.description_html) <= 200_000 and len(p.title) <= 300


def test_remote_board_jobs_are_manual_apply() -> None:
    for src in REMOTE_BOARDS:
        assert apply_mode({"source": src, "canonical_url": "https://example.com/j/1",
                           "application_url": "https://boards.greenhouse.io/x/jobs/1"}
                          ) == "manual"


def test_remote_boards_preference_default_and_validation() -> None:
    assert SearchPreferences().remote_boards == list(REMOTE_BOARDS)
    assert SearchPreferences(remote_boards=[]).remote_boards == []
    with pytest.raises(ValidationError):
        SearchPreferences(remote_boards=["linkedin"])


def test_runtime_registers_remote_boards() -> None:
    sources = build_sources(httpx.AsyncClient())
    for name in ("himalayas", "remotive", "remoteok", "jobicy", "workingnomads"):
        assert getattr(sources[name], "kind", None) == "remote_board"


class FakeBoard:
    kind = "remote_board"
    supports_max_postings = True

    def __init__(self, name: str, posts: list[RawPosting] | None = None,
                 error: ErrorCode | None = None) -> None:
        self.source, self._posts, self._error = name, posts or [], error
        self.caps: list[int] = []

    async def list_board(self, max_postings: int) -> list[RawPosting]:
        self.caps.append(max_postings)
        if self._error:
            raise SourceError(self._error)
        return self._posts[:max_postings]


def _post(src: str, i: int, location: str | None) -> RawPosting:
    return RawPosting(source=src, source_job_id=str(i), company="Example Corp",  # type: ignore[arg-type]
                      title="AI Engineer", location=location, description_html="<p>x</p>",
                      canonical_url=f"https://example.com/{src}/{i}", application_url=None,
                      posted_at=None)


async def test_finder_runs_enabled_remote_boards() -> None:
    him = FakeBoard("himalayas", [_post("himalayas", 1, "India"),
                                  _post("himalayas", 2, "USA only")])
    rem = FakeBoard("remotive", error=ErrorCode.SOURCE_TIMEOUT)
    rok = FakeBoard("remoteok", [_post("remoteok", 3, None)])
    prefs = SearchPreferences(target_titles=["AI Engineer"], locations=["India", "Remote"],
                              remote_boards=["himalayas", "remotive", "jobicy"],
                              max_jobs_per_run=50)
    ctx = AgentContext(task_id="task_1", gateway=None, tools={
        "sources": {"himalayas": him, "remotive": rem, "remoteok": rok}, "prefs": prefs,
        "companies": [], "clock": lambda: datetime(2026, 10, 25, tzinfo=UTC)})
    out = await DiscoverAgent().run(
        TaskInput(task_id="task_1", kind=TaskKind.DISCOVER, entity_type="run",
                  entity_id="run_1", attempt=1), ctx)
    assert [li.source_job_id for li in out.listings] == ["1"]
    assert out.source_status == {"board:himalayas": "ok", "board:remotive": "SOURCE_TIMEOUT",
                                 "board:jobicy": "unsupported_source"}
    assert him.caps == [50] and rok.caps == []
