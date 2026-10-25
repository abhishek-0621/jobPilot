"""Plan 11 Task 4: jobs filter bar, remote-board attribution, settings boards."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import QueryParams

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.dashboard.job_filters import parse_filters, query_string
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head, utcnow
from jobpilot.data.repositories import upsert_job
from jobpilot.data.tables import JobRow, MatchAssessmentRow
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import make_listing


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "j.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> TestClient:
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1")


def _job(s: Session, jid: str, *, score: float | None = None, days_ago: int = 0,
         **kw: Any) -> None:
    seen = utcnow() - timedelta(days=days_ago)
    base: dict[str, Any] = dict(job_id=jid, source_job_id=jid, title=f"Title {jid}",
                                company="Example Corp", location="Bengaluru",
                                canonical_url=f"https://example.com/{jid}",
                                first_seen_at=seen, last_seen_at=seen)
    upsert_job(s, make_listing(**(base | kw)))
    row = s.query(JobRow).filter_by(source_job_id=jid).one()
    if score is not None:
        s.add(MatchAssessmentRow(job_id=row.job_id, profile_version=1, skill_fit=0,
                                 experience_fit=0, project_fit=0, role_seniority_fit=0,
                                 location_fit=0, total_score=score, hard_filter_passed=True,
                                 evidence_json="{}", confidence=0.9))


@pytest.fixture
def seeded(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        _job(s, "a", score=90, company="Example Corp", title="AI Engineer",
             source="greenhouse", work_mode="remote", location="Remote - India")
        _job(s, "b", score=60, company="Sample Labs", title="ML Engineer",
             source="remotive", work_mode="remote", location="Worldwide", days_ago=20)
        _job(s, "c", score=75, company="Demo Inc", title="Data Scientist",
             source="himalayas", work_mode="onsite", location="Pune", days_ago=40)
        _job(s, "d", company="Example Corp", title="Platform Engineer", source="lever",
             work_mode="hybrid", location="Berlin", description_text="100% remote_ok")
        s.commit()


def _titles(html: str) -> set[str]:
    return {t for t in ("AI Engineer", "ML Engineer", "Data Scientist", "Platform Engineer")
            if f">{t}</a>" in html}


def test_combined_filters_are_anded(client: TestClient, seeded: None) -> None:
    page = client.get("/ui/jobs?company=Example+Corp&work_mode=remote").text
    assert _titles(page) == {"AI Engineer"}
    page = client.get("/ui/jobs?source=remotive&min_score=70").text
    assert _titles(page) == set()
    page = client.get("/ui/jobs?q=engineer&posted_within=30&min_score=50").text
    assert _titles(page) == {"AI Engineer", "ML Engineer"}
    assert "2 jobs match these filters" in page


def test_multi_select_is_or_within_column(client: TestClient, seeded: None) -> None:
    page = client.get("/ui/jobs?source=remotive&source=himalayas").text
    assert _titles(page) == {"ML Engineer", "Data Scientist"}
    page = client.get("/ui/jobs?company=Sample+Labs&company=Demo+Inc&work_mode=onsite").text
    assert _titles(page) == {"Data Scientist"}


def test_apply_mode_location_and_sort(client: TestClient, seeded: None) -> None:
    page = client.get("/ui/jobs?apply_mode=manual").text
    assert _titles(page) == {"ML Engineer", "Data Scientist"}
    assert _titles(client.get("/ui/jobs?location=india").text) == {"AI Engineer"}
    page = client.get("/ui/jobs?sort=score").text
    order = [page.index(f">{t}</a>") for t in ("AI Engineer", "Data Scientist",
                                                "ML Engineer", "Platform Engineer")]
    assert order == sorted(order)
    page = client.get("/ui/jobs?sort=company").text
    assert page.index(">Data Scientist</a>") < page.index(">ML Engineer</a>")


def test_invalid_params_ignored(client: TestClient, seeded: None) -> None:
    resp = client.get("/ui/jobs?status=BOGUS&min_score=abc&posted_within=9&sort=evil"
                      "&work_mode=moon&apply_mode=x&page=-3&unknown=1")
    assert resp.status_code == 200
    assert _titles(resp.text) == {"AI Engineer", "ML Engineer", "Data Scientist",
                                  "Platform Engineer"}
    f = parse_filters(QueryParams("min_score=500&posted_within=7&status=MATCHED&status=x"))
    assert f.min_score is None and f.posted_within == 7 and f.status == ["MATCHED"]


def test_injection_like_text_is_literal(client: TestClient, seeded: None) -> None:
    for q in ("' OR 1=1 --", "%", "_", "\\"):
        assert _titles(client.get("/ui/jobs", params={"q": q}).text) == (
            {"Platform Engineer"} if q in ("%", "_") else set())
    assert client.get("/ui/jobs", params={"company": "x' OR '1'='1"}).status_code == 200


def test_filter_values_are_autoescaped(client: TestClient, seeded: None) -> None:
    page = client.get("/ui/jobs", params={"q": "<script>alert(1)</script>"}).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_htmx_returns_partial_with_chips(client: TestClient, seeded: None) -> None:
    resp = client.get("/ui/jobs?source=remotive&source=himalayas",
                      headers={"HX-Request": "true"})
    assert resp.text.lstrip().startswith('<div id="jobs-results">')
    assert "<form" not in resp.text
    assert 'href="/ui/jobs?source=himalayas"' in resp.text  # chip removes only remotive
    assert "Clear all" in resp.text


def test_query_string_round_trip() -> None:
    f = parse_filters(QueryParams("q=ai&company=A&company=B&min_score=70&sort=score&page=2"))
    assert parse_filters(QueryParams(query_string(f))) == f


def test_pagination(client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        for i in range(55):
            _job(s, f"p{i}", title=f"Role {i:02d}")
        s.commit()
    first = client.get("/ui/jobs").text
    assert "55 jobs" in first and "page 1 of 2" in first and "page=2" in first
    second = client.get("/ui/jobs?page=2").text
    assert second.count('<a href="/ui/jobs/job_') == 5


def test_job_detail_credits_remote_board(client: TestClient, factory: sessionmaker[Session],
                                         ) -> None:
    with factory() as s:
        _job(s, "rok1", source="remoteok", canonical_url="https://remoteok.com/remote-jobs/1")
        s.commit()
        jid = s.query(JobRow).filter_by(source_job_id="rok1").one().job_id
    page = client.get(f"/ui/jobs/{jid}").text
    assert 'via <a href="https://remoteok.com/remote-jobs/1"' in page and ">RemoteOK</a>" in page


def test_settings_lists_remote_boards(client: TestClient) -> None:
    page = client.get("/ui/settings").text
    assert "Remote job boards" in page
    assert "Himalayas, Remotive, We Work Remotely, RemoteOK, Jobicy, Working Nomads" in page


def test_foundit_and_indeed_india_are_deny_listed() -> None:
    from jobpilot.portals.registry import denied_host
    assert denied_host("www.foundit.in") and denied_host("in.indeed.com")
    assert denied_host("indeed.co.in")
