"""Company board finder: slug candidates, live-board probing, CLI --write, companies page."""

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from jobpilot import cli
from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.dashboard import routes
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.discovery.board_finder import BoardHit, find_boards, slug_candidates
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.profile.preferences import CompanyBoard

runner = CliRunner()


def test_slug_candidates_variants_and_suffixes() -> None:
    c = slug_candidates("Sample Labs")
    assert c == ["samplelabs", "sample", "sample-labs", "sample_labs"]
    assert slug_candidates("Acme Technologies India Inc.")[:2] == [
        "acmetechnologiesindiainc", "acme"]
    assert slug_candidates("Example AI") == ["exampleai", "example", "example-ai", "example_ai"]
    assert slug_candidates("Solo") == ["solo", "solo-"][:1]


def test_slug_candidates_bounded_deduped_and_safe() -> None:
    c = slug_candidates("  Foo   Bar Baz Labs Inc  ")
    assert len(c) <= 6 and len(set(c)) == len(c)
    assert all(x == x.lower() and " " not in x for x in c)
    assert slug_candidates("!!!") == []
    assert all(len(x) <= 64 for x in slug_candidates("x" * 80))
    assert slug_candidates("x" * 80) == []  # too long for the token pattern


def _handler(seen: list[str]) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        host, path = req.url.host, req.url.path
        if host == "boards-api.greenhouse.io" and "/acme/" in path:
            return httpx.Response(200, json={"jobs": [{"id": 1}, {"id": 2}]})
        if host == "api.lever.co":
            return httpx.Response(404, json={"ok": False})
        if host == "api.ashbyhq.com":
            return httpx.Response(200, json={"jobs": []})
        if host == "api.smartrecruiters.com":
            return httpx.Response(200, json={"totalFound": 0, "content": []})
        return httpx.Response(404)
    return handler


async def test_board_finder_records_only_live_boards() -> None:
    seen: list[str] = []
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(seen))) as client:
        boards = await find_boards("Acme Labs", client, sleep=sleep, clock=lambda: 0.0)
    assert boards == [CompanyBoard(name="Acme Labs", source="greenhouse", token="acme")]
    assert any("api.lever.co" in u for u in seen)  # missing boards were probed, not recorded
    assert sleeps and all(s == 1.0 for s in sleeps)  # >= 1 s between requests per host
    gh = [u for u in seen if "greenhouse" in u]
    assert gh[-1].endswith("/acme/jobs")  # stops probing greenhouse after the hit


async def test_board_finder_orders_by_source_and_survives_network_errors() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.lever.co":
            raise httpx.ConnectError("down", request=req)
        if req.url.host == "api.smartrecruiters.com":
            return httpx.Response(200, json={"totalFound": 4, "content": []})
        if req.url.host == "api.ashbyhq.com":
            return httpx.Response(200, json={"jobs": [{"id": "x"}]})
        return httpx.Response(200, text="not json")

    async def nosleep(_s: float) -> None: ...

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        boards = await find_boards("Zed", client, sleep=nosleep)
    assert [b.source for b in boards] == ["ashby", "smartrecruiters"]


def _cfg_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "app.yaml").write_text(yaml.safe_dump({
        "db_path": str(tmp_path / "storage" / "jobpilot.db"),
        "storage_path": str(tmp_path / "storage")}))
    (d / "model_routes.yaml").write_text(yaml.safe_dump({"routes": {
        "a": {"model_tag": "m1:small"}}}))
    return d


def test_boards_find_write_dedupes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _cfg_dir(tmp_path)
    prof = tmp_path / "storage" / "profiles"
    prof.mkdir(parents=True)
    path = prof / "companies.yaml"
    path.write_text(yaml.safe_dump({"companies": [
        {"name": "Mine", "source": "lever", "token": "mine"}]}))

    async def nosleep(_s: float) -> None: ...

    monkeypatch.setattr(cli, "_boards_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_handler([]))))
    import jobpilot.discovery.board_finder as bf
    monkeypatch.setattr(bf.asyncio, "sleep", nosleep)
    args = ["boards", "find", "Acme", "--write", "--config-dir", str(d)]
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    assert "Acme" in r.output and "greenhouse" in r.output and "2" in r.output
    r2 = runner.invoke(cli.app, args)
    assert r2.exit_code == 0, r2.output
    assert "Added 0 board(s)" in r2.output
    entries = yaml.safe_load(path.read_text())["companies"]
    assert entries == [{"name": "Mine", "source": "lever", "token": "mine"},
                       {"name": "Acme", "source": "greenhouse", "token": "acme"}]


def test_boards_find_without_write_and_creates_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _cfg_dir(tmp_path)
    monkeypatch.setattr(cli, "_boards_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_handler([]))))
    import jobpilot.discovery.board_finder as bf

    async def nosleep(_s: float) -> None: ...

    monkeypatch.setattr(bf.asyncio, "sleep", nosleep)
    target = tmp_path / "storage" / "profiles" / "companies.yaml"
    assert runner.invoke(cli.app, ["boards", "find", "Acme", "--config-dir", str(d)]
                         ).exit_code == 0
    assert not target.exists()
    assert runner.invoke(cli.app, ["boards", "find", "Acme", "--write", "--config-dir", str(d)]
                         ).exit_code == 0
    assert target.read_text().startswith("# Company boards")
    assert not Path("config/companies.example.yaml").read_text().count("acme")


@pytest.fixture
def page_client(tmp_path: Path) -> TestClient:
    upgrade_to_head(tmp_path / "m.db")
    factory: sessionmaker[Session] = make_session_factory(make_engine(tmp_path / "m.db"))
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    orch = Orchestrator(factory, cfg, {}, {})
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1",
                      headers={"origin": f"http://127.0.0.1:{cfg.port}"})


def test_companies_page_add_flow(page_client: TestClient, tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    page = page_client.get("/ui/companies")
    assert page.status_code == 200
    assert "Example AI" in page.text and "Add company" in page.text  # example list fallback
    assert 'href="/ui/companies"' in page.text

    async def fake(names: list[str], client: Any, **kw: Any) -> list[BoardHit]:
        assert names == ["Acme"]
        return [BoardHit(CompanyBoard(name="Acme", source="greenhouse", token="acme"), 7)]

    monkeypatch.setattr(routes, "find_hits", fake)
    r = page_client.post("/ui/companies", data={"name": " Acme "})
    assert r.status_code == 200
    assert "1 added" in r.text and "acme" in r.text  # PRG: summary + configured table
    path = tmp_path / "storage" / "profiles" / "companies.yaml"
    assert yaml.safe_load(path.read_text())["companies"] == [
        {"name": "Acme", "source": "greenhouse", "token": "acme"}]
    again = page_client.post("/ui/companies", data={"name": "Acme"})
    assert "0 added" in again.text and "1 already configured" in again.text
    assert len(yaml.safe_load(path.read_text())["companies"]) == 1
    assert page_client.post("/ui/companies", data={"name": "  "}).status_code == 422
    assert page_client.post("/ui/companies", data={"name": "x"},
                            headers={"origin": "https://evil.example"}).status_code == 403


def test_companies_page_none_found(page_client: TestClient,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    async def none(names: list[str], client: Any, **kw: Any) -> list[BoardHit]:
        return []

    monkeypatch.setattr(routes, "find_hits", none)
    r = page_client.post("/ui/companies", data={"name": "<b>Ghost</b>"})
    assert r.status_code == 200 and "No live public boards found" in r.text
    assert "<b>Ghost" not in r.text


# ---- atomic, validated companies.yaml writes ----

def _boards() -> list[CompanyBoard]:
    return [CompanyBoard(name="Acme", source="greenhouse", token="acme")]


@pytest.mark.parametrize("content", [
    "companies: [unclosed\n  - {",
    "- name: Acme\n  source: lever\n",
    "companies:\n  - just a string\n",
    "companies: {name: x}\n",
    "just text\n",
])
def test_append_companies_rejects_bad_file_untouched(tmp_path: Path, content: str) -> None:
    from jobpilot.profile.preferences import CompaniesFileError, append_companies
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    path = tmp_path / "storage" / "profiles" / "companies.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(CompaniesFileError):
        append_companies(cfg, _boards())
    assert path.read_text() == content
    assert [p.name for p in path.parent.iterdir()] == ["companies.yaml"]


def test_append_companies_atomic_keeps_entries_no_temp(tmp_path: Path) -> None:
    from jobpilot.profile.preferences import append_companies
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    path = tmp_path / "storage" / "profiles" / "companies.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"companies": [
        {"name": "Mine", "source": "lever", "token": "mine"}]}))
    assert len(append_companies(cfg, _boards())) == 1
    assert [e["name"] for e in yaml.safe_load(path.read_text())["companies"]] == ["Mine", "Acme"]
    assert [p.name for p in path.parent.iterdir()] == ["companies.yaml"]


def test_append_companies_failed_write_leaves_file_and_no_temp(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from jobpilot.profile.preferences import append_companies
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    path = tmp_path / "storage" / "profiles" / "companies.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("companies: []\n")

    def boom(*a: Any, **k: Any) -> None:
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        append_companies(cfg, _boards())
    assert path.read_text() == "companies: []\n"
    assert [p.name for p in path.parent.iterdir()] == ["companies.yaml"]


def test_boards_find_write_bad_file_exits_1(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    d = _cfg_dir(tmp_path)
    path = tmp_path / "storage" / "profiles" / "companies.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("companies: [unclosed\n")
    monkeypatch.setattr(cli, "_boards_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_handler([]))))
    import jobpilot.discovery.board_finder as bf

    async def nosleep(_s: float) -> None: ...

    monkeypatch.setattr(bf.asyncio, "sleep", nosleep)
    r = runner.invoke(cli.app, ["boards", "find", "Acme", "--write", "--config-dir", str(d)])
    assert r.exit_code == 1 and "companies.yaml" in r.output
    assert path.read_text() == "companies: [unclosed\n"


def test_companies_page_bad_file_is_422(page_client: TestClient, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    async def one(names: list[str], client: Any, **kw: Any) -> list[BoardHit]:
        return [BoardHit(CompanyBoard(name="Acme", source="greenhouse", token="acme"), 7)]

    monkeypatch.setattr(routes, "find_hits", one)
    path = tmp_path / "storage" / "profiles" / "companies.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("- a\n- b\n")
    r = page_client.post("/ui/companies", data={"name": "Acme"})
    assert r.status_code == 422 and "companies.yaml" in r.text
    assert path.read_text() == "- a\n- b\n"
