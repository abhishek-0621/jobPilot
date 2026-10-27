"""Company research: fetch safety, grounding, the agent/handler, cache and invalidation,
the PREPARE_FORM wait, migration and dashboard. No network: MockTransport + fake DNS."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from jobpilot.agents.company_research import (
    CompanyBrief,
    CompanyResearchAgent,
    company_research_handler,
    queue_company_research,
    request_refresh,
)
from jobpilot.contracts.enums import FieldDecision
from jobpilot.data.tables import (
    ApplicationRow,
    AuditEventRow,
    CompanyBriefRow,
    FormFieldAnswerRow,
    JobRow,
    TaskRow,
)
from jobpilot.orchestrator.engine import AgentContext, TaskInput, TaskKind
from jobpilot.portals.base import FormField, FormSnapshot
from jobpilot.profile.preferences import CompanyBoard
from jobpilot.research import fetch as rf
from jobpilot.research.brief import BriefItem, brief_hash, company_key, ground_items
from jobpilot.research.fetch import (
    MAX_BYTES,
    ResearchFetcher,
    candidate_urls,
    fetch_pages,
    safe_url,
)
from tests.agent_contracts.test_essay import SeqGateway
from tests.agent_contracts.test_form_prep import Env, FakePortal, make_env, snapshot
from tests.agent_contracts.test_why_company import GOOD, ok

HOST = "www.northwind-robotics.example"
BASE = f"https://{HOST}"
ROOT = "https://northwind-robotics.example"  # the homepage research derives
NEWS = ("<html><head><title>Northwind news</title></head><body><h1>News</h1>"
        "<p>March 2026: we launched Fleet Copilot, an assistant that answers questions "
        "about warehouse robot fleets.</p><p>2019: Old Gripper was retired.</p>"
        "<p>Open Grasp Benchmark is a public benchmark for robotic grasping.</p>"
        "</body></html>")
NOW = datetime(2026, 10, 27, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def fetcher(handler: Any, addrs: list[str] | None = None) -> tuple[ResearchFetcher, Clock]:
    clock = Clock()

    async def resolve(host: str) -> list[str]:
        return addrs or ["93.184.216.34"]
    f = ResearchFetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        resolve=resolve, sleep=clock.sleep, clock=clock)
    return f, clock


def site(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    if request.url.path == "/private":
        return httpx.Response(200, text="<p>secret</p>")
    return httpx.Response(200, text=NEWS, headers={"content-type": "text/html"})


# ---- fetch safety -----------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/company/northwind", "https://www.glassdoor.com/x",
    "https://www.crunchbase.com/organization/northwind", "https://boards.greenhouse.io/nw",
    "http://127.0.0.1/blog", "http://localhost/blog", "http://10.0.0.5/", "ftp://x.example/",
    "javascript:alert(1)", "https://in.linkedin.com/x",
])
def test_unsafe_urls_refused(url: str) -> None:
    assert safe_url(url) is None
    seen: list[str] = []
    f, _ = fetcher(lambda r: seen.append(str(r.url)) or httpx.Response(200, text="x"))
    assert asyncio.run(f.fetch(url)) is None and seen == []


def test_private_ip_resolution_refused() -> None:
    seen: list[str] = []
    for addr in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"):
        f, _ = fetcher(lambda r: seen.append(str(r.url)) or httpx.Response(200, text="x"),
                       [addr])
        assert asyncio.run(f.fetch(BASE + "/blog")) is None
    assert seen == []


def test_redirects_stay_on_the_registrable_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == HOST and request.url.path == "/news":
            return httpx.Response(301, headers={"location": "https://blog.northwind-robotics."
                                                            "example/posts"})
        if request.url.host == HOST and request.url.path == "/blog":
            return httpx.Response(302, headers={"location": "https://evil.example.net/"})
        if request.url.host == HOST and request.url.path == "/press":
            return httpx.Response(302, headers={"location": "https://www.linkedin.com/x"})
        return httpx.Response(200, text=NEWS, headers={"content-type": "text/html"})
    f, _ = fetcher(handler)
    page = asyncio.run(f.fetch(BASE + "/news"))
    assert page is not None and page.url == "https://blog.northwind-robotics.example/posts"
    assert page.title == "Northwind news"
    assert asyncio.run(f.fetch(BASE + "/blog")) is None
    assert asyncio.run(f.fetch(BASE + "/press")) is None
    assert not any("evil" in u or "linkedin" in u for u in f.requests)


def test_size_cap_and_non_html() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/logo":
            return httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"})
        return httpx.Response(200, content=b"a" * (MAX_BYTES + 500_000),
                              headers={"content-type": "text/html"})
    f, _ = fetcher(handler)
    page = asyncio.run(f.fetch(BASE + "/blog"))
    assert page is not None and len(page.html) == MAX_BYTES
    assert asyncio.run(f.fetch(BASE + "/logo")) is None


def test_page_cap_robots_and_politeness() -> None:
    f, clock = fetcher(site)
    urls = [BASE + "/private"] + [f"{BASE}/p{i}" for i in range(10)]
    pages = asyncio.run(fetch_pages(f, urls))
    assert len(pages) == rf.MAX_PAGES
    assert BASE + "/private" not in f.requests  # robots.txt disallows it
    assert f.requests[0] == BASE + "/robots.txt"
    assert f.requests.count(BASE + "/robots.txt") == 1
    assert len(clock.slept) == len(f.requests) - 1 and all(s >= 1.0 for s in clock.slept)


def test_robots_forbidden_means_disallow_all() -> None:
    f, _ = fetcher(lambda r: httpx.Response(403) if r.url.path == "/robots.txt"
                   else httpx.Response(200, text=NEWS))
    assert asyncio.run(f.fetch(BASE + "/blog")) is None


def test_candidate_urls_and_research_urls_validation() -> None:
    urls = candidate_urls("northwind-robotics.example",
                          ["https://research.northwind-robotics.example/papers",
                           "https://www.linkedin.com/company/nw"])
    assert urls[0] == "https://research.northwind-robotics.example/papers"
    assert "https://northwind-robotics.example/" in urls
    assert "https://northwind-robotics.example/newsroom" in urls
    assert not any("linkedin" in u for u in urls)
    assert candidate_urls(None, []) == []
    ok_board = CompanyBoard(name="Northwind Robotics", source="lever", token="nw", extra={
        "research_urls": "https://northwind-robotics.example/blog, https://x.example/a"})
    assert ok_board.extra["research_urls"]
    for bad in ("javascript:alert(1)", "not a url", "", " ".join(["https://a.example/"] * 7)):
        with pytest.raises(ValueError):
            CompanyBoard(name="N", source="lever", token="nw", extra={"research_urls": bad})


# ---- grounding --------------------------------------------------------------------------


def _raw(**kw: Any) -> dict[str, Any]:
    base = {"kind": "product", "name": "Fleet Copilot",
            "one_line": "An assistant that answers questions about warehouse robot fleets.",
            "date_if_stated": "March 2026", "source_url": BASE + "/news"}
    return base | kw


def test_grounding_drops_ungrounded_items_and_sorts_recent_first() -> None:
    pages = {BASE + "/news": NEWS.replace("<", " <")}
    raw = [
        _raw(name="Old Gripper", one_line="Old Gripper was retired.", date_if_stated="2019",
             kind="project"),
        _raw(name="Open Grasp Benchmark", kind="research", date_if_stated=None,
             one_line="A public benchmark for robotic grasping."),
        _raw(),
        _raw(name="Project Falcon"),                              # not on the page
        _raw(name="Fleet Copilot"),                               # duplicate
        _raw(name="Robot Cloud", source_url="https://evil.example.net/"),
        _raw(name="Open Grasp Benchmark", one_line="Used by 500 labs."),  # dup + number
        _raw(kind="rumour", name="Fleet"),                        # bad kind
        _raw(name="News", one_line="Run by Acme Holdings."),      # proper noun not on page
    ]
    items = ground_items(raw, pages, NOW)
    assert [i.name for i in items] == ["Fleet Copilot", "Open Grasp Benchmark", "Old Gripper"]
    raw2 = [_raw(one_line="Launched to 40 customers.")]
    assert ground_items(raw2, pages, NOW) == []
    moved = ground_items([_raw(date_if_stated="June 2026")], pages, NOW)
    assert moved[0].date_if_stated is None  # a date the page never states is dropped
    assert company_key("Northwind Robotics, Inc.") == company_key("northwind robotics")
    assert company_key("") is None


# ---- agent + handler + cache ------------------------------------------------------------


def research_env(tmp_path: Path, snap: FormSnapshot | None = None) -> Env:
    env = make_env(tmp_path, FakePortal(snap or snapshot()))
    with env.factory() as s:
        app = env.app(s)
        job = s.get(JobRow, app.job_id)
        assert job is not None
        job.company = "Northwind Robotics"
        job.canonical_url = BASE + "/careers/42"
        job.description_text = ("About Northwind Robotics: we build Fleet Copilot for "
                                "warehouse robot fleets.")
        s.commit()
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(site))
    env.tools["research_fetcher"] = ResearchFetcher(transport_client, sleep=_nosleep)
    return env


async def _nosleep(_s: float) -> None:
    return None


def items_reply() -> dict[str, Any]:
    return {"items": [_raw(source_url=f"{ROOT}/"),
                      _raw(name="Made Up Thing", source_url=f"{ROOT}/")]}


def brief_row(env: Env) -> CompanyBriefRow | None:
    with env.factory() as s:
        return s.get(CompanyBriefRow, "northwindrobotics")


async def test_research_agent_stores_grounded_brief(tmp_path: Path) -> None:
    env = research_env(tmp_path)
    env.orch._gateway = SeqGateway(items_reply())  # type: ignore[assignment]
    env.orch.enqueue(TaskKind.COMPANY_RESEARCH, "application", env.app_id)
    await env.orch.run_once()
    row = brief_row(env)
    assert row is not None and row.company == "Northwind Robotics"
    items = json.loads(row.items_json)
    assert [i["name"] for i in items] == ["Fleet Copilot"]  # "Made Up Thing" dropped
    sources = [x["url"] for x in json.loads(row.sources_json)]
    assert sources[0] == BASE + "/careers/42" and f"{ROOT}/" in sources
    assert len(sources) <= 1 + rf.MAX_PAGES
    assert row.content_hash == brief_hash([BriefItem.model_validate(i) for i in items])
    calls = env.orch._gateway.calls  # type: ignore[attr-defined]
    assert calls[0]["route"] == "company_research"
    assert "<untrusted_content>" in calls[0]["messages"][1]["content"]


async def test_cache_is_reused_and_refresh_forces(tmp_path: Path) -> None:
    env = research_env(tmp_path)
    gw = SeqGateway(items_reply())
    env.orch._gateway = gw  # type: ignore[assignment]
    with env.factory() as s:
        assert queue_company_research(s, env.app_id) is not None
        assert queue_company_research(s, env.app_id) is None  # already queued
        s.commit()
    await env.orch.run_until_idle()
    with env.factory() as s:
        assert queue_company_research(s, env.app_id) is None  # fresh brief
    env.orch.enqueue(TaskKind.COMPANY_RESEARCH, "application", env.app_id)
    await env.orch.run_until_idle()
    assert len(gw.calls) == 1  # cached: no fetch, no model call
    with env.factory() as s:
        row = s.get(CompanyBriefRow, "northwindrobotics")
        assert row is not None
        row.fetched_at = datetime.now(UTC) - timedelta(days=15)
        s.commit()
        assert queue_company_research(s, env.app_id) is not None  # stale
        s.rollback()
        assert request_refresh(s, "northwindrobotics") is not None
        assert request_refresh(s, "northwindrobotics") is None
        s.commit()
    await env.orch.run_until_idle()
    assert len(gw.calls) == 2


def test_handler_rejects_tampered_output(tmp_path: Path) -> None:
    env = research_env(tmp_path)
    item = BriefItem.model_validate(_raw())
    with env.factory() as s:
        task = TaskRow(task_id="t1", kind="COMPANY_RESEARCH", entity_type="application",
                       entity_id=env.app_id, status="RUNNING", attempts=1)
        good = CompanyBrief(task_id="t1", created_at=NOW, company_key="northwindrobotics",
                            company="Northwind Robotics", fetched_at=NOW, items=[item],
                            sources=[{"url": BASE + "/news", "fetched_at": NOW}],
                            content_hash=brief_hash([item]))
        from jobpilot.orchestrator.engine import OutputRejected
        for bad in (good.model_copy(update={"company_key": "other"}),
                    good.model_copy(update={"sources": []}),
                    good.model_copy(update={"content_hash": "x"})):
            with pytest.raises(OutputRejected):
                company_research_handler(s, task, bad)
        assert company_research_handler(s, task, good) == []


async def test_no_gateway_fails_research_and_no_pages_gives_empty_brief(
        tmp_path: Path) -> None:
    env = research_env(tmp_path)
    agent = CompanyResearchAgent()
    tools = {k: env.tools[k] for k in agent.tools}
    inp = TaskInput(task_id="t", kind=TaskKind.COMPANY_RESEARCH, entity_type="company",
                    entity_id="northwindrobotics", attempt=1)
    from jobpilot.orchestrator.engine import OutputRejected
    with pytest.raises(OutputRejected):
        await agent.run(inp, AgentContext(task_id="t", gateway=None, tools=tools))
    with env.factory() as s:
        job = s.get(JobRow, env.app(s).job_id)
        assert job is not None
        job.description_text = ""
        job.canonical_url = "https://www.linkedin.com/jobs/view/1"
        job.application_url = "https://www.linkedin.com/jobs/view/1/apply"
        s.commit()
    out = await agent.run(inp, AgentContext(task_id="t", gateway=SeqGateway(items_reply()),
                                            tools=tools))  # type: ignore[arg-type]
    assert out.items == [] and out.sources == []


# ---- PREPARE_FORM wiring ----------------------------------------------------------------

WHY = "Why do you want to work at Northwind Robotics?"


def why_snapshot() -> FormSnapshot:
    return FormSnapshot(url="x", fields=[*snapshot().fields[:3], FormField(
        field_id="q_why", label=WHY, field_type="textarea", required=True)])


async def test_prepare_form_waits_for_research_then_drafts(tmp_path: Path) -> None:
    env = research_env(tmp_path, why_snapshot())
    gw = SeqGateway(items_reply(), ok())
    env.orch._gateway = gw  # type: ignore[assignment]
    with env.factory() as s:
        queue_company_research(s, env.app_id)
        s.commit()
    prep = env.orch.enqueue(TaskKind.PREPARE_FORM, "application", env.app_id)
    with env.factory() as s:  # research is not leased yet: PREPARE_FORM goes first here
        research = s.execute(select(TaskRow).where(
            TaskRow.kind == "COMPANY_RESEARCH")).scalar_one()
        research.not_before = datetime.now(UTC) + timedelta(seconds=1)
        s.commit()
    await env.orch.run_once(kinds={TaskKind.PREPARE_FORM})
    with env.factory() as s:
        task = s.get(TaskRow, prep)
        assert task is not None and task.status == "QUEUED" and task.not_before is not None
        assert s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "task_deferred")).first() is not None
        assert env.portal.opened == []  # deferred before the form was opened
        research = s.execute(select(TaskRow).where(
            TaskRow.kind == "COMPANY_RESEARCH")).scalar_one()
        research.not_before = None
        task.not_before = None
        s.commit()
    await env.orch.run_once(kinds={TaskKind.COMPANY_RESEARCH})
    await env.orch.run_once(kinds={TaskKind.PREPARE_FORM})
    with env.factory() as s:
        row = env.rows(s)["q_why"]
        assert row.decision == "FILL_AND_REVIEW", row.rationale
        assert json.loads(row.proposed_value_json) == GOOD
        brief = s.get(CompanyBriefRow, "northwindrobotics")
        assert brief is not None and row.source_fact_id == "essay:why_company:" \
            + brief.content_hash


async def test_wait_is_capped_then_asks_user(tmp_path: Path) -> None:
    env = research_env(tmp_path, why_snapshot())
    env.orch._gateway = SeqGateway(ok())  # type: ignore[assignment]
    with env.factory() as s:
        queue_company_research(s, env.app_id)
        s.commit()
    prep = env.orch.enqueue(TaskKind.PREPARE_FORM, "application", env.app_id)
    with env.factory() as s:
        task = s.get(TaskRow, prep)
        assert task is not None
        task.attempts = 6  # waited the maximum number of times already
        s.commit()
    await env.orch.run_once(kinds={TaskKind.PREPARE_FORM})
    with env.factory() as s:
        row = env.rows(s)["q_why"]
        assert row.decision == "ASK_USER" and json.loads(row.proposed_value_json) is None
        assert "add research URLs for Northwind Robotics" in row.rationale


async def test_refreshed_brief_invalidates_why_answer(tmp_path: Path) -> None:
    env = research_env(tmp_path, why_snapshot())
    gw = SeqGateway(items_reply(), ok())
    env.orch._gateway = gw  # type: ignore[assignment]
    env.orch.enqueue(TaskKind.COMPANY_RESEARCH, "application", env.app_id)
    await env.orch.run_once()
    env.orch.enqueue(TaskKind.PREPARE_FORM, "application", env.app_id)
    await env.orch.run_once()
    with env.factory() as s:
        assert env.rows(s)["q_why"].decision == "FILL_AND_REVIEW"
    # same content again: nothing changes
    gw.canned = [items_reply()]
    gw.calls.clear()
    with env.factory() as s:
        request_refresh(s, "northwindrobotics")
        s.commit()
    await env.orch.run_once()
    with env.factory() as s:
        assert env.rows(s)["q_why"].decision == "FILL_AND_REVIEW"
    # different content: the draft goes back to the user
    gw.canned = [{"items": [_raw(name="Open Grasp Benchmark", kind="research",
                                 date_if_stated=None, source_url=f"{ROOT}/",
                                 one_line="A public benchmark for robotic grasping.")]}]
    with env.factory() as s:
        request_refresh(s, "northwindrobotics")
        s.commit()
    await env.orch.run_once()
    with env.factory() as s:
        row = env.rows(s)["q_why"]
        assert row.decision == "ASK_USER" and row.rationale.startswith("brief_changed")
        assert json.loads(row.proposed_value_json) == GOOD  # kept for reference
        # the single writer also refuses a stale draft
        from jobpilot.agents.essay import guard_essay_answers
        from jobpilot.contracts.models import FieldAnswer
        stale = FieldAnswer(field_id="q_why", label=WHY, field_type="textarea",
                            proposed_value=GOOD, decision=FieldDecision.FILL_AND_REVIEW,
                            source_fact_id="essay:why_company:oldhash", confidence=0.5,
                            rationale="x")
        (g,) = guard_essay_answers(s, [stale], env.app_id)
        assert g.decision == FieldDecision.ASK_USER and g.proposed_value is None


def test_select_job_queues_research_but_discovery_never_does() -> None:
    from jobpilot.ops.schedule import DISCOVERY_KINDS
    assert TaskKind.COMPANY_RESEARCH not in DISCOVERY_KINDS


def test_why_company_never_saved_to_library(tmp_path: Path) -> None:
    from jobpilot.data import mutations
    env = research_env(tmp_path, why_snapshot())
    with env.factory() as s:
        s.add(FormFieldAnswerRow(
            application_id=env.app_id, field_id="q_why", label=WHY, field_type="textarea",
            proposed_value_json=json.dumps(GOOD), decision="FILL_AND_REVIEW",
            source_fact_id="essay:why_company:abc", confidence=0.5, rationale="x",
            required=True, options_json="[]"))
        s.flush()
        with pytest.raises(ValueError):
            mutations.save_essay_to_library(s, env.app_id, "q_why")


# ---- dashboard --------------------------------------------------------------------------


def test_dashboard_shows_brief_escaped_with_safe_links(tmp_path: Path) -> None:
    env = research_env(tmp_path)
    evil = BriefItem(kind="product", name="<script>alert(1)</script>",
                     one_line="<img src=x onerror=alert(1)>", source_url="javascript:alert(1)")
    good = BriefItem.model_validate(_raw())
    with env.factory() as s:
        s.add(CompanyBriefRow(
            company_key="northwindrobotics", company="Northwind Robotics", fetched_at=NOW,
            sources_json=json.dumps([{"url": BASE + "/news", "title": "t",
                                      "fetched_at": NOW.isoformat()}]),
            items_json=json.dumps([good.model_dump(), evil.model_dump()]),
            content_hash="h"))
        s.commit()
    client = env.client()
    for path in (f"/ui/applications/{env.app_id}", "/ui/companies"):
        html = client.get(path).text
        assert "What JobPilot found about Northwind Robotics" in html
        assert "Fleet Copilot" in html and f'href="{BASE}/news"' in html
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
        assert "<img src=x" not in html and 'href="javascript:' not in html
        assert "/ui/companies/northwindrobotics/research" in html
    r = client.post("/ui/companies/northwindrobotics/research",
                    data={"back": f"/ui/applications/{env.app_id}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/ui/applications/{env.app_id}"
    r = client.post("/ui/companies/northwindrobotics/research",
                    data={"back": "https://evil.example.net/"}, follow_redirects=False)
    assert r.headers["location"] == "/ui/companies"
    assert client.post("/ui/companies/BAD..key/research").status_code == 404
    with env.factory() as s:
        tasks = s.execute(select(TaskRow).where(
            TaskRow.kind == "COMPANY_RESEARCH")).scalars().all()
        assert [(t.entity_type, t.entity_id) for t in tasks] == [
            ("company", "northwindrobotics")]
        assert s.get(ApplicationRow, env.app_id) is not None
    html = client.get(f"/ui/applications/{env.app_id}").text
    assert "research queued" in html


# ---- migration --------------------------------------------------------------------------


def test_migration_0012_upgrade_downgrade(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src/jobpilot/data/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "head")

    def tables() -> dict[str, set[str]]:
        engine = create_engine(f"sqlite:///{db}")
        try:
            insp = inspect(engine)
            return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}
        finally:
            engine.dispose()

    assert tables()["company_brief"] == {"company_key", "company", "fetched_at",
                                         "sources_json", "items_json", "content_hash"}
    command.downgrade(cfg, "0011_one_active_discover")
    assert "company_brief" not in tables()
    command.upgrade(cfg, "head")
    assert "company_brief" in tables()
