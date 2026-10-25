"""Plan 10 Task 4: sources, outreach & essays hardening."""

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jobpilot.agents import essay as essay_mod
from jobpilot.discovery.sources.ashby import AshbySource
from jobpilot.discovery.sources.base import RateLimitedFetcher
from jobpilot.discovery.sources.greenhouse import GreenhouseSource
from jobpilot.discovery.sources.lever import LeverSource
from jobpilot.discovery.sources.smartrecruiters import SmartRecruitersSource
from jobpilot.discovery.sources.workday import WorkdaySource
from jobpilot.ops import backup as bk
from jobpilot.outreach.contacts import extract_from_posting, fetch_careers_inbox
from jobpilot.portals.base import FormField
from jobpilot.profile.stories import QuestionKey
from tests.unit.test_board_finder import page_client  # noqa: F401
from tests.unit.test_sources_plan6 import FIX, WD, _wd_handler, client

# ---- Workday ----


async def test_workday_external_url_pinned_to_tenant_host() -> None:
    det = json.loads((FIX / "workday_detail.json").read_text(encoding="utf-8"))
    det["jobPostingInfo"]["externalUrl"] = "https://evil.example/External/job/x"
    async with client(_wd_handler([], detail=det)) as c:
        p = (await WorkdaySource(c, min_interval_s=0).list_postings(WD))[0]
    assert p.canonical_url.startswith("https://sample.wd3.myworkdayjobs.com/External/job/")


def test_sources_declare_supports_max_postings() -> None:
    c = httpx.AsyncClient()
    flags = {cls.__name__: cls(c).supports_max_postings for cls in (
        GreenhouseSource, LeverSource, AshbySource, SmartRecruitersSource, WorkdaySource)}
    assert flags == {"GreenhouseSource": False, "LeverSource": False, "AshbySource": False,
                     "SmartRecruitersSource": True, "WorkdaySource": True}
    assert all("supports_max_postings" in vars(cls) for cls in (
        GreenhouseSource, LeverSource, AshbySource, SmartRecruitersSource, WorkdaySource))


# ---- careers fetch: private addresses ----

def _fetcher(html: str, seen: list[str]) -> tuple[httpx.AsyncClient, RateLimitedFetcher]:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, text=html)
    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c, RateLimitedFetcher(c, min_interval_s=0)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254",
                                "::1", "fc00::1", "0.0.0.0"])
async def test_careers_fetch_refuses_private_resolution(ip: str) -> None:
    seen: list[str] = []
    c, f = _fetcher('<a href="mailto:careers@acme.com">x</a>', seen)

    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34", ip]
    async with c:
        assert await fetch_careers_inbox("https://acme.com/careers", f, "acme.com",
                                         resolve=resolve) == []
    assert seen == []


async def test_careers_fetch_public_resolution_ok() -> None:
    seen: list[str] = []
    c, f = _fetcher('<a href="mailto:careers@acme.com">x</a>', seen)

    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]
    async with c:
        found = await fetch_careers_inbox("https://acme.com/careers", f, "acme.com",
                                          resolve=resolve)
    assert [x.address for x in found] == ["careers@acme.com"]


async def test_careers_fetch_unresolvable_refused() -> None:
    seen: list[str] = []
    c, f = _fetcher("", seen)

    async def resolve(host: str) -> list[str]:
        raise OSError("nxdomain")
    async with c:
        assert await fetch_careers_inbox("https://acme.com/careers", f, "acme.com",
                                         resolve=resolve) == []
    assert seen == []


# ---- slug fallback ----

@pytest.mark.parametrize("domain,ok", [
    ("acmerobotics.com", True), ("acmerobotics.io", True), ("acmerobotics.co.in", True),
    ("acmerobotics.dev", True), ("acmerobotics.xyz", False), ("acmerobotics.zip", False),
    ("acmerobotics.example", False)])
def test_slug_fallback_only_common_tlds(domain: str, ok: bool) -> None:
    found = extract_from_posting(f"Mail careers@{domain}", None, company="Acme Robotics")
    assert bool(found) is ok


async def test_careers_inbox_tool_gets_company_for_slug(monkeypatch) -> None:
    from jobpilot import runtime

    calls: list[Any] = []

    async def fake(url, fetcher, domain, company=None, **kw):  # type: ignore[no-untyped-def]
        calls.append((url, domain, company))
        return []
    monkeypatch.setattr(runtime, "fetch_careers_inbox", fake)
    async with httpx.AsyncClient() as c:
        tool = runtime.make_careers_inbox(c)
        await tool("https://acmerobotics.com/careers", None, "Acme Robotics")
    assert calls == [("https://acmerobotics.com/careers", None, "Acme Robotics")]


# ---- maxlength ----

def test_formfield_has_maxlength() -> None:
    f = FormField(field_id="q", label="Why us?", field_type="textarea", required=False,
                  maxlength=500)
    assert f.maxlength == 500
    assert FormField(field_id="q", label="x", field_type="text", required=False).maxlength \
        is None


async def test_essay_draft_receives_field_maxlength(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_draft(self, gateway, **kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        raise _Stop
    monkeypatch.setattr(essay_mod.EssayAgent, "draft", fake_draft)
    monkeypatch.setattr(essay_mod, "essay_candidates",
                        lambda fields, answers: [(fields[0], QuestionKey.PROUD_OF)])
    f = FormField(field_id="q", label="Why us?", field_type="textarea", required=False,
                  maxlength=321)

    class _Ctx:
        library: dict = {}
        bank = facts = skills = job = None
    with pytest.raises(_Stop):
        await essay_mod.draft_essay_answers([f], [], None, _Ctx())  # type: ignore[arg-type]
    assert seen["maxlength"] == 321


class _Stop(Exception):
    pass


# ---- companies page ----

def test_companies_get_shows_corrupt_file_error(page_client: TestClient,  # noqa: F811
                                                tmp_path: Path) -> None:
    path = tmp_path / "storage" / "profiles" / "companies.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("companies: [unclosed\n", encoding="utf-8")
    r = page_client.get("/ui/companies")
    assert r.status_code == 200
    assert "companies.yaml" in r.text and 'class="err"' in r.text


def test_companies_add_is_post_redirect_get(page_client: TestClient,  # noqa: F811
                                            monkeypatch) -> None:
    from jobpilot.dashboard import routes
    from jobpilot.discovery.board_finder import BoardHit
    from jobpilot.profile.preferences import CompanyBoard

    async def one(names, client, **kw):  # type: ignore[no-untyped-def]
        return [BoardHit(CompanyBoard(name="Acme", source="greenhouse", token="acme"), 7)]
    monkeypatch.setattr(routes, "find_hits", one)
    r = page_client.post("/ui/companies", data={"name": "Acme"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui/companies?")
    page = page_client.get(r.headers["location"])
    assert "1 added" in page.text


# ---- backup restore caps ----

def _tar(tmp_path: Path, members: list[tuple[str, int]]) -> Path:
    p = tmp_path / "a.tar.gz"
    with tarfile.open(p, "w:gz") as t:
        for name, size in members:
            info = tarfile.TarInfo(name)
            info.size = size
            t.addfile(info, io.BytesIO(b"\0" * size))
    return p


def test_restore_member_count_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bk, "MAX_RESTORE_MEMBERS", 3)
    arc = _tar(tmp_path, [(f"artifacts/f{i}", 1) for i in range(5)])
    (tmp_path / "d").mkdir()
    with pytest.raises(bk.RestoreError, match="too many"):
        bk._extract_verified(arc, tmp_path / "d")
    assert not any((tmp_path / "d").iterdir())


def test_restore_total_size_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bk, "MAX_RESTORE_BYTES", 1000)
    arc = _tar(tmp_path, [("db.sqlite", 600), ("artifacts/x", 600)])
    (tmp_path / "d").mkdir()
    with pytest.raises(bk.RestoreError, match="too large"):
        bk._extract_verified(arc, tmp_path / "d")
    assert not any((tmp_path / "d").iterdir())
