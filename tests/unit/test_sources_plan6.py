import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.sources.ashby import AshbySource
from jobpilot.discovery.sources.base import SourceError
from jobpilot.discovery.sources.greenhouse import GreenhouseSource
from jobpilot.discovery.sources.lever import LeverSource
from jobpilot.discovery.sources.smartrecruiters import SmartRecruitersSource
from jobpilot.discovery.sources.workday import WorkdaySource
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
    b = CompanyBoard(name="X", source="workday", token="x",
                     extra={"site": "Ext_1", "wd_host": "wd5"})
    assert b.extra == {"site": "Ext_1", "wd_host": "wd5"}
    assert CompanyBoard(name="X", source="ashby", token="x").extra == {}
    for bad in ({"a b": "x"}, {"site": "x/y"}, {"site": ""}, {"k" * 65: "v"}):
        with pytest.raises(ValidationError):
            CompanyBoard(name="X", source="workday", token="x", extra=bad)


# ---- Workday ----

WD = CompanyBoard(name="Sample Corp", source="workday", token="sample",
                  extra={"wd_host": "wd3", "site": "External"})
WD_BASE = "https://sample.wd3.myworkdayjobs.com"


def _wd_handler(seen: list[httpx.Request], total: int = 2, detail: dict | None = None):  # type: ignore[no-untyped-def]
    listing = json.loads((FIX / "workday_jobs.json").read_text())
    det = detail or json.loads((FIX / "workday_detail.json").read_text())

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "POST":
            body = json.loads(req.content)
            off = body["offset"]
            if total == 2:
                return httpx.Response(200, json=listing)
            n = max(0, min(20, total - off))
            return httpx.Response(200, json={"total": total, "jobPostings": [
                {**listing["jobPostings"][0], "externalPath": f"/job/X/Job_{off + i}"}
                for i in range(n)]})
        return httpx.Response(200, json=det)

    return handler


async def test_workday_parses_fixture() -> None:
    seen: list[httpx.Request] = []
    async with client(_wd_handler(seen)) as c:
        posts = await WorkdaySource(c, min_interval_s=0).list_postings(WD)
    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{WD_BASE}/wday/cxs/sample/External/jobs"
    assert json.loads(seen[0].content) == {
        "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    assert seen[0].headers["User-Agent"] == UA
    assert seen[1].method == "GET"
    assert str(seen[1].url) == (
        f"{WD_BASE}/wday/cxs/sample/External/job/Bengaluru/Machine-Learning-Engineer_R-1001")
    p = posts[0]
    assert p.source == "workday" and p.source_job_id == "R-1001"
    assert p.company == "Sample Corp" and p.title == "Machine Learning Engineer"
    assert p.location == "Bengaluru, India"
    assert p.description_html == "<p>Train and ship <strong>models</strong>.</p>"
    assert p.canonical_url == (
        f"{WD_BASE}/External/job/Bengaluru/Machine-Learning-Engineer_R-1001")
    assert p.application_url == p.canonical_url
    assert p.posted_at == datetime(2026, 10, 8, tzinfo=UTC)


async def test_workday_urls_are_normalized() -> None:
    det = json.loads((FIX / "workday_detail.json").read_text())
    det["jobPostingInfo"]["externalUrl"] = (
        f"{WD_BASE.replace('https://', 'HTTPS://www.')}/External/job/x/?utm_source=a#frag")
    async with client(_wd_handler([], detail=det)) as c:
        p = (await WorkdaySource(c, min_interval_s=0).list_postings(WD))[0]
    assert p.canonical_url == "https://sample.wd3.myworkdayjobs.com/External/job/x"
    assert p.application_url == p.canonical_url


async def test_workday_remote_and_public_url_fallback() -> None:
    det = json.loads((FIX / "workday_detail.json").read_text())
    det["jobPostingInfo"].update(externalUrl="javascript:alert(1)", remoteType="Remote",
                                 startDate=None)
    async with client(_wd_handler([], detail=det)) as c:
        p = (await WorkdaySource(c, min_interval_s=0).list_postings(WD))[0]
    assert p.location == "Bengaluru, India (Remote)" and p.posted_at is None
    assert p.canonical_url == (
        f"{WD_BASE}/External/job/Bengaluru/Machine-Learning-Engineer_R-1001")


async def test_workday_pagination_caps() -> None:
    seen: list[httpx.Request] = []
    async with client(_wd_handler(seen, total=500)) as c:
        posts = await WorkdaySource(c, min_interval_s=0).list_postings(WD, max_postings=500)
    lists = [r for r in seen if r.method == "POST"]
    assert [json.loads(r.content)["offset"] for r in lists] == [0, 20, 40, 60, 80]
    assert len(posts) == 100  # 5 pages x 20
    seen.clear()
    async with client(_wd_handler(seen, total=500)) as c:
        posts = await WorkdaySource(c, min_interval_s=0).list_postings(WD, max_postings=25)
    assert len([r for r in seen if r.method == "POST"]) == 2
    assert len([r for r in seen if r.method == "GET"]) == 25 and len(posts) == 25


async def test_workday_stops_on_short_page() -> None:
    seen: list[httpx.Request] = []
    async with client(_wd_handler(seen, total=30)) as c:
        posts = await WorkdaySource(c, min_interval_s=0).list_postings(WD, max_postings=100)
    assert len([r for r in seen if r.method == "POST"]) == 2 and len(posts) == 30


@pytest.mark.parametrize("handler,code", [
    (lambda r: httpx.Response(404), ErrorCode.SOURCE_UNAVAILABLE),
    (lambda r: httpx.Response(429), ErrorCode.SOURCE_RATE_LIMIT),
    (_raise_timeout, ErrorCode.SOURCE_TIMEOUT),
    (lambda r: httpx.Response(200, text="not json"), ErrorCode.SOURCE_CHANGED),
    (lambda r: httpx.Response(200, json={"unexpected": True}), ErrorCode.SOURCE_CHANGED),
])
async def test_workday_post_error_codes(handler, code) -> None:  # type: ignore[no-untyped-def]
    async with client(handler) as c:
        with pytest.raises(SourceError) as ei:
            await WorkdaySource(c, min_interval_s=0).list_postings(WD)
    assert ei.value.code is code


async def test_workday_detail_shape_change() -> None:
    async with client(_wd_handler([], detail={"jobPostingInfo": {}})) as c:
        with pytest.raises(SourceError) as ei:
            await WorkdaySource(c, min_interval_s=0).list_postings(WD)
    assert ei.value.code is ErrorCode.SOURCE_CHANGED


async def test_workday_rejects_bad_external_path() -> None:
    bad = ["/job/../admin", "/other/x", "job/x", "/job/a b", "https://evil.test/job/x",
           "/job/%2e%2e/x/..", "/job/a%2Fb", "/job/a%2fb", "/job/%2E/x", ""]
    listing = {"total": 8, "jobPostings": [
        {"title": "T", "externalPath": p, "locationsText": ""} for p in bad] + [
        {"title": "Good", "externalPath": "/job/Bengaluru/Machine-Learning-Engineer_R-1001"}]}
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "POST":
            return httpx.Response(200, json=listing)
        return httpx.Response(200, text=(FIX / "workday_detail.json").read_text())

    async with client(handler) as c:
        posts = await WorkdaySource(c, min_interval_s=0).list_postings(WD)
    assert len(posts) == 1
    assert [r.url.path for r in seen if r.method == "GET"] == [
        "/wday/cxs/sample/External/job/Bengaluru/Machine-Learning-Engineer_R-1001"]


@pytest.mark.parametrize("extra", [
    {}, {"site": "External"}, {"wd_host": "wd3"}, {"wd_host": "evil", "site": "External"},
    {"wd_host": "wd123", "site": "External"}, {"wd_host": "wd3.evil", "site": "External"},
    {"wd_host": "wd3", "site": "a/b"},
])
def test_workday_board_requires_valid_extra(extra) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        CompanyBoard(name="X", source="workday", token="x", extra=extra)


async def test_workday_rate_limit_spacing_post_and_get() -> None:
    ft = FakeTime()
    inner = _wd_handler([])

    def handler(req: httpx.Request) -> httpx.Response:
        ft.request_times.append(ft.t)
        return inner(req)

    async with client(handler) as c:
        await WorkdaySource(c, sleep=ft.sleep, clock=ft.clock).list_postings(WD)
    gaps = [b - a for a, b in zip(ft.request_times, ft.request_times[1:], strict=False)]
    assert len(gaps) == 2 and all(g >= 1.0 for g in gaps)


# ---- Task 1 carry-overs ----

def test_require_http() -> None:
    from jobpilot.discovery.normalize import require_http

    assert require_http("https://a.test/x") == "https://a.test/x"
    assert require_http("HTTP://a.test") == "HTTP://a.test"
    assert require_http("  https://a.test/x \n") == "https://a.test/x"
    for bad in ("javascript:alert(1)", "ftp://a.test", "//a.test/x", "/x", "", None,
                "https://"):
        assert require_http(bad) is None


async def test_ashby_skips_non_http_canonical_and_drops_bad_apply_url() -> None:
    data = json.loads((FIX / "ashby_board.json").read_text())
    data["jobs"][0]["jobUrl"] = "javascript:alert(1)"
    data["jobs"][1]["applyUrl"] = "data:text/html,x"
    async with client(lambda r: httpx.Response(200, json=data)) as c:
        posts = await AshbySource(c, min_interval_s=0).list_postings(AB)
    assert len(posts) == 1 and posts[0].application_url is None


async def test_greenhouse_lever_skip_non_http() -> None:
    gh = json.loads((FIX / "greenhouse_jobs.json").read_text())
    n = len(gh["jobs"])
    gh["jobs"][0]["absolute_url"] = "javascript:x"
    lv = json.loads((FIX / "lever_postings.json").read_text())
    lv.append({**lv[0], "id": "00000000-0000-4000-8000-000000000002"})
    m = len(lv)
    lv[0]["hostedUrl"] = "ftp://x.test/a"
    lv[1]["applyUrl"] = "javascript:x"
    async with client(lambda r: httpx.Response(200, json=gh)) as c:
        g = await GreenhouseSource(c, min_interval_s=0).list_postings(
            CompanyBoard(name="E", source="greenhouse", token="e"))
    async with client(lambda r: httpx.Response(200, json=lv)) as c:
        lp = await LeverSource(c, min_interval_s=0).list_postings(
            CompanyBoard(name="E", source="lever", token="e"))
    assert len(g) == n - 1 and len(lp) == m - 1
    assert lp[0].application_url is None


async def test_smartrecruiters_invalid_id_empty_page_and_escaped_titles(
    caplog: pytest.LogCaptureFixture,
) -> None:
    listing = json.loads((FIX / "smartrecruiters_postings.json").read_text())
    listing["content"][1]["id"] = "../../evil"
    listing["totalFound"] = 5000  # lying total; empty page must end pagination
    detail = json.loads((FIX / "smartrecruiters_detail.json").read_text())
    detail["jobAd"]["sections"]["jobDescription"]["title"] = "<script>x</script>"
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/postings"):
            if int(req.url.params["offset"]) == 0:
                return httpx.Response(200, json=listing)
            return httpx.Response(200, json={**listing, "content": []})
        return httpx.Response(200, json=detail)

    with caplog.at_level("WARNING"):
        async with client(handler) as c:
            posts = await SmartRecruitersSource(c, min_interval_s=0).list_postings(
                SR, max_postings=500)
    assert len(posts) == 1
    assert len([r for r in seen if r.url.path.endswith("/postings")]) == 2
    assert not any("evil" in str(r.url) for r in seen)
    assert "&lt;script&gt;x&lt;/script&gt;" in posts[0].description_html
    assert "<script>" not in posts[0].description_html
    assert "skipped 1" in caplog.text
