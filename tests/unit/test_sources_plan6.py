import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.sources.ashby import AshbySource
from jobpilot.discovery.sources.base import SourceError
from jobpilot.discovery.sources.smartrecruiters import SmartRecruitersSource
from jobpilot.profile.preferences import CompanyBoard

FIX = Path(__file__).parent.parent / "fixtures"
AB = CompanyBoard(name="Example AI", source="ashby", token="exampleai")
SR = CompanyBoard(name="Sample Labs", source="smartrecruiters", token="samplelabs")
UA = "JobPilot/0.1 (+personal job search; contact via repo)"


def client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeTime:
    def __init__(self) -> None:
        self.t = 100.0
        self.request_times: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


async def test_ashby_parses_fixture() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=(FIX / "ashby_board.json").read_text())

    async with client(handler) as c:
        posts = await AshbySource(c, min_interval_s=0).list_postings(AB)
    assert str(seen[0].url) == (
        "https://api.ashbyhq.com/posting-api/job-board/exampleai?includeCompensation=true")
    assert seen[0].headers["User-Agent"] == UA
    assert len(posts) == 2  # unlisted skipped
    p, q = posts
    assert p.source == "ashby" and p.source_job_id == "a0000000-0000-4000-8000-000000000001"
    assert p.company == "Example AI" and p.title == "Applied AI Engineer"
    assert p.location == "Bengaluru, India; Pune, India; Remote - India (Remote)"
    assert p.description_html == "<p>Build <strong>agents</strong> at Example AI.</p>"
    assert p.canonical_url == (
        "https://jobs.ashbyhq.com/exampleai/a0000000-0000-4000-8000-000000000001")
    assert p.application_url == p.canonical_url + "/application"
    assert p.posted_at == datetime(2026, 10, 2, 9, 30, tzinfo=UTC)
    assert q.location == "Remote" and q.posted_at is None


def _sr_handler(seen: list[httpx.Request], total: int = 2):  # type: ignore[no-untyped-def]
    listing = json.loads((FIX / "smartrecruiters_postings.json").read_text())
    detail = (FIX / "smartrecruiters_detail.json").read_text()

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/postings"):
            off = int(req.url.params["offset"])
            n = min(100, total - off)
            content = [{**listing["content"][0], "id": str(off + i)} for i in range(n)]
            return httpx.Response(200, json={**listing, "offset": off, "totalFound": total,
                                             "content": content if total != 2
                                             else listing["content"]})
        return httpx.Response(200, text=detail)

    return handler


async def test_smartrecruiters_parses_fixture() -> None:
    seen: list[httpx.Request] = []
    async with client(_sr_handler(seen)) as c:
        posts = await SmartRecruitersSource(c, min_interval_s=0).list_postings(SR)
    assert str(seen[0].url) == (
        "https://api.smartrecruiters.com/v1/companies/samplelabs/postings?limit=100&offset=0")
    assert str(seen[1].url) == (
        "https://api.smartrecruiters.com/v1/companies/samplelabs/postings/744000000000001")
    assert all(r.headers["User-Agent"] == UA for r in seen)
    p, q = posts
    assert p.source == "smartrecruiters" and p.source_job_id == "744000000000001"
    assert p.title == "ML Engineer" and p.company == "Sample Labs"
    assert p.location == "Bengaluru, in" and q.location == "Pune, in (Remote)"
    assert p.description_html == (
        "<h3>Company Description</h3><p>Sample Labs builds tools.</p>"
        "<h3>Job Description</h3><p>Train models.</p>"
        "<h3>Qualifications</h3><ul><li>Python</li></ul>"
        "<h3>Additional Information</h3><p>Hybrid.</p>")
    assert p.canonical_url == "https://jobs.smartrecruiters.com/samplelabs/744000000000001"
    assert p.application_url == p.canonical_url
    assert p.posted_at == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


async def test_smartrecruiters_paginates_and_caps_detail_calls() -> None:
    seen: list[httpx.Request] = []
    async with client(_sr_handler(seen, total=250)) as c:
        posts = await SmartRecruitersSource(c, min_interval_s=0).list_postings(
            SR, max_postings=120)
    lists = [r for r in seen if r.url.path.endswith("/postings")]
    details = [r for r in seen if not r.url.path.endswith("/postings")]
    assert [r.url.params["offset"] for r in lists] == ["0", "100"]
    assert len(details) == 120 and len(posts) == 120


async def test_smartrecruiters_paginates_to_total() -> None:
    seen: list[httpx.Request] = []
    async with client(_sr_handler(seen, total=150)) as c:
        posts = await SmartRecruitersSource(c, min_interval_s=0).list_postings(SR)
    assert len(posts) == 100  # default cap
    seen.clear()
    async with client(_sr_handler(seen, total=150)) as c:
        posts = await SmartRecruitersSource(c, min_interval_s=0).list_postings(
            SR, max_postings=500)
    assert len(posts) == 150
    assert len([r for r in seen if r.url.path.endswith("/postings")]) == 2


def _raise_timeout(req: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=req)


@pytest.mark.parametrize("cls,board", [(AshbySource, AB), (SmartRecruitersSource, SR)])
@pytest.mark.parametrize("handler,code", [
    (lambda r: httpx.Response(404), ErrorCode.SOURCE_UNAVAILABLE),
    (lambda r: httpx.Response(429), ErrorCode.SOURCE_RATE_LIMIT),
    (_raise_timeout, ErrorCode.SOURCE_TIMEOUT),
    (lambda r: httpx.Response(200, text="not json"), ErrorCode.SOURCE_CHANGED),
    (lambda r: httpx.Response(200, json={"unexpected": True}), ErrorCode.SOURCE_CHANGED),
    (lambda r: httpx.Response(200, json={"jobs": [{"id": 1}], "content": [{"id": 1}],
                                         "totalFound": 1}), ErrorCode.SOURCE_CHANGED),
])
async def test_source_error_codes(cls, board, handler, code) -> None:  # type: ignore[no-untyped-def]
    async with client(handler) as c:
        with pytest.raises(SourceError) as ei:
            await cls(c, min_interval_s=0).list_postings(board)
    assert ei.value.code is code


async def test_smartrecruiters_detail_error_maps() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/postings"):
            return httpx.Response(200, text=(FIX / "smartrecruiters_postings.json").read_text())
        return httpx.Response(429)

    async with client(handler) as c:
        with pytest.raises(SourceError) as ei:
            await SmartRecruitersSource(c, min_interval_s=0).list_postings(SR)
    assert ei.value.code is ErrorCode.SOURCE_RATE_LIMIT


async def test_rate_limit_spacing_list_and_detail() -> None:
    ft = FakeTime()
    inner = _sr_handler([])

    def handler(req: httpx.Request) -> httpx.Response:
        ft.request_times.append(ft.t)
        return inner(req)

    async with client(handler) as c:
        await SmartRecruitersSource(c, sleep=ft.sleep, clock=ft.clock).list_postings(SR)
    gaps = [b - a for a, b in zip(ft.request_times, ft.request_times[1:], strict=False)]
    assert len(gaps) == 2 and all(g >= 1.0 for g in gaps)


async def test_ashby_rate_limit_spacing() -> None:
    ft = FakeTime()
    body = (FIX / "ashby_board.json").read_text()

    def handler(req: httpx.Request) -> httpx.Response:
        ft.request_times.append(ft.t)
        return httpx.Response(200, text=body)

    async with client(handler) as c:
        src = AshbySource(c, sleep=ft.sleep, clock=ft.clock)
        for _ in range(3):
            await src.list_postings(AB)
    gaps = [b - a for a, b in zip(ft.request_times, ft.request_times[1:], strict=False)]
    assert len(gaps) == 2 and all(g >= 1.0 for g in gaps)


def test_company_board_extra_validation() -> None:
    b = CompanyBoard(name="X", source="workday", token="x", extra={"site": "Ext_1", "wd": "wd5"})
    assert b.extra == {"site": "Ext_1", "wd": "wd5"}
    assert CompanyBoard(name="X", source="ashby", token="x").extra == {}
    for bad in ({"a b": "x"}, {"site": "x/y"}, {"site": ""}, {"k" * 65: "v"}):
        with pytest.raises(ValidationError):
            CompanyBoard(name="X", source="workday", token="x", extra=bad)
