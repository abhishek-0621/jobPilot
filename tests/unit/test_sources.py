import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.prefilter import prefilter
from jobpilot.discovery.sources.base import RawPosting, SourceError
from jobpilot.discovery.sources.greenhouse import GreenhouseSource
from jobpilot.discovery.sources.lever import LeverSource
from jobpilot.profile.preferences import CompanyBoard, SearchPreferences

FIX = Path(__file__).parent.parent / "fixtures"
GH = CompanyBoard(name="Example AI", source="greenhouse", token="exampleai")
LV = CompanyBoard(name="Sample Labs", source="lever", token="samplelabs")
UA = "JobPilot/0.1 (+personal job search; contact via repo)"


def client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeTime:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []
        self.request_times: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


async def test_greenhouse_parses_fixture() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=(FIX / "greenhouse_jobs.json").read_text())

    async with client(handler) as c:
        posts = await GreenhouseSource(c, min_interval_s=0).list_postings(GH)
    req = seen[0]
    assert str(req.url) == "https://boards-api.greenhouse.io/v1/boards/exampleai/jobs?content=true"
    assert req.headers["User-Agent"] == UA
    assert req.headers["Accept"] == "application/json"
    assert len(posts) == 2
    p = posts[0]
    assert p.source == "greenhouse" and p.source_job_id == "1001"
    assert p.company == "Example AI" and p.title == "Senior Applied ML Engineer"
    assert p.location == "Bengaluru, India"
    assert p.description_html.startswith("<p>Build <strong>LLM</strong>")
    assert p.canonical_url == "https://boards.greenhouse.io/exampleai/jobs/1001"
    assert p.application_url == "https://job-boards.greenhouse.io/embed/job_app?for=exampleai&token=1001"
    assert p.posted_at == datetime(2026, 10, 1, 14, 0, tzinfo=UTC)


async def test_lever_parses_fixture() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=(FIX / "lever_postings.json").read_text())

    async with client(handler) as c:
        posts = await LeverSource(c, min_interval_s=0).list_postings(LV)
    assert str(seen[0].url) == "https://api.lever.co/v0/postings/samplelabs?mode=json"
    assert seen[0].headers["User-Agent"] == UA
    [p] = posts
    uid = "00000000-0000-4000-8000-000000000001"
    assert p.source == "lever" and p.source_job_id == uid
    assert p.company == "Sample Labs" and p.title == "GenAI Engineer"
    assert p.location == "Remote - India"
    assert p.description_html == (
        "<div>Ship RAG pipelines.</div><h3>Requirements</h3><ul><li>3+ years Python</li></ul>"
        "<div>We value curiosity.</div>"
    )
    assert p.canonical_url == f"https://jobs.lever.co/samplelabs/{uid}"
    assert p.application_url == f"https://jobs.lever.co/samplelabs/{uid}/apply"
    assert p.posted_at == datetime.fromtimestamp(1730000000, tz=UTC)


def _raise_timeout(req: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=req)


@pytest.mark.parametrize("cls,board", [(GreenhouseSource, GH), (LeverSource, LV)])
@pytest.mark.parametrize("handler,code", [
    (lambda r: httpx.Response(404), ErrorCode.SOURCE_UNAVAILABLE),
    (lambda r: httpx.Response(429), ErrorCode.SOURCE_RATE_LIMIT),
    (_raise_timeout, ErrorCode.SOURCE_TIMEOUT),
    (lambda r: httpx.Response(200, text="not json"), ErrorCode.SOURCE_CHANGED),
    (lambda r: httpx.Response(200, json={"unexpected": True}), ErrorCode.SOURCE_CHANGED),
    (lambda r: httpx.Response(200, json=[{"id": 1}]), ErrorCode.SOURCE_CHANGED),
])
async def test_source_error_codes(cls, board, handler, code) -> None:  # type: ignore[no-untyped-def]
    async with client(handler) as c:
        with pytest.raises(SourceError) as ei:
            await cls(c, min_interval_s=0).list_postings(board)
    assert ei.value.code is code


@pytest.mark.parametrize("cls,board,fixture", [
    (GreenhouseSource, GH, "greenhouse_jobs.json"),
    (LeverSource, LV, "lever_postings.json"),
])
async def test_rate_limit_spacing(cls, board, fixture) -> None:  # type: ignore[no-untyped-def]
    ft = FakeTime()
    body = (FIX / fixture).read_text()

    def handler(req: httpx.Request) -> httpx.Response:
        ft.request_times.append(ft.t)
        return httpx.Response(200, text=body)

    async with client(handler) as c:
        src = cls(c, sleep=ft.sleep, clock=ft.clock)
        for _ in range(3):
            await src.list_postings(board)
    gaps = [b - a for a, b in zip(ft.request_times, ft.request_times[1:], strict=False)]
    assert len(gaps) == 2 and all(g >= 1.0 for g in gaps)


def _post(title: str, location: str | None = "Bengaluru, India",
          desc: str = "<p>Work on things.</p>") -> RawPosting:
    return RawPosting(source="greenhouse", source_job_id="1", company="Example AI",
                      title=title, location=location, description_html=desc,
                      canonical_url="https://boards.greenhouse.io/exampleai/jobs/1",
                      application_url=None, posted_at=None)


PREFS = SearchPreferences(target_titles=["Backend Engineer"], locations=["Bengaluru"],
                          work_modes=["remote", "hybrid"], exclude_keywords=["intern"])


@pytest.mark.parametrize("posting,keep,reason", [
    (_post("Sr. Backend Engineer"), True, "ok"),
    (_post("Engineer, Backend (Payments)"), True, "ok"),
    (_post("ML Platform Engineer"), True, "ok"),
    (_post("Applied Scientist II"), True, "ok"),
    (_post("Head of Machine Learning"), True, "ok"),
    (_post("Email Marketing Lead"), False, "title_mismatch"),
    (_post("Frontend Engineer"), False, "title_mismatch"),
    (_post("LLM Engineer", location="Remote - India"), True, "ok"),
    (_post("LLM Engineer", location="Anywhere"), True, "ok"),
    (_post("LLM Engineer", location=None), True, "ok"),
    (_post("LLM Engineer", location="London, UK"), False, "location_mismatch"),
    (_post("AI Intern"), False, "excluded_keyword:intern"),
    (_post("AI Engineer", desc="<p>This is an Intern role</p>"), False,
     "excluded_keyword:intern"),
])
def test_prefilter_rules(posting: RawPosting, keep: bool, reason: str) -> None:
    assert prefilter(posting, PREFS) == (keep, reason)


def test_prefilter_remote_requires_remote_work_mode() -> None:
    prefs = PREFS.model_copy(update={"work_modes": ["onsite"]})
    assert prefilter(_post("AI Engineer", location="Remote"), prefs) == (
        False, "location_mismatch")


def test_raw_posting_json_roundtrip() -> None:
    p = _post("AI Engineer")
    assert RawPosting.model_validate(json.loads(p.model_dump_json())) == p


def test_prefilter_location_whole_word_only() -> None:
    prefs = PREFS.model_copy(update={"locations": ["Pune"]})
    assert prefilter(_post("LLM Engineer", location="Puneet Nagar"), prefs) == (
        False, "location_mismatch")
    assert prefilter(_post("LLM Engineer", location="PUNE, India"), prefs) == (True, "ok")


def test_prefilter_excludes_symbol_keywords() -> None:
    prefs = PREFS.model_copy(update={"exclude_keywords": ["c++", ".net"]})
    assert prefilter(_post("AI Engineer", desc="<p>Strong C++ skills</p>"), prefs) == (
        False, "excluded_keyword:c++")
    assert prefilter(_post("AI Engineer", desc="<p>Experience with .NET core</p>"), prefs) == (
        False, "excluded_keyword:.net")
    assert prefilter(_post("AI Engineer", desc="<p>Python and cpp-free</p>"), prefs) == (
        True, "ok")
