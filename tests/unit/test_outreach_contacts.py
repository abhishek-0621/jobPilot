"""Plan 8 Task 1: public contact finding (no guessing), careers inbox, ranking, migration."""

from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from jobpilot.config import AppConfig, OutreachConfig
from jobpilot.discovery.sources.base import RateLimitedFetcher
from jobpilot.outreach.contacts import (
    FoundContact,
    company_domain,
    domain_matches,
    extract_from_posting,
    fetch_careers_inbox,
    rank,
)
from jobpilot.profile.preferences import CompanyBoard

POSTING = """
Senior Backend Engineer at Acme Robotics.
Questions? Contact: Priya Raman, Talent Partner (priya.raman@acmerobotics.example).
You can also write to careers@acmerobotics.example.
Our partner agency is at jobs@globex-staffing.example; ignore that.
Hiring manager: Sam Lee, Engineering Manager.
"""


def test_no_guessed_or_offdomain_emails() -> None:
    found = extract_from_posting(POSTING, "acmerobotics.example")
    addresses = {c.address for c in found}
    assert addresses == {"priya.raman@acmerobotics.example", "careers@acmerobotics.example"}
    assert all(c.channel == "email" and c.confidence == 0.9 for c in found)
    named = next(c for c in found if c.address.startswith("priya"))
    assert named.kind == "posting_named" and named.name == "Priya Raman"
    assert named.professional_title == "Talent Partner"
    # a name with no published email: nothing is constructed for it
    assert not any("sam" in c.address for c in found)
    only_name = "Contact: Sam Lee, Recruiter. Apply via the button below."
    assert extract_from_posting(only_name, "acmerobotics.example") == []


def test_posting_email_without_known_domain_uses_slug() -> None:
    text = "Write to careers@acmerobotics.example or hr@other.example."
    found = extract_from_posting(text, None, company="Acme Robotics")
    assert [c.address for c in found] == ["careers@acmerobotics.example"]
    assert extract_from_posting(text, None, company="Initech") == []


def test_company_domain_maps_ats_hosts() -> None:
    assert company_domain("https://careers.acmerobotics.example/jobs/1", None, None) \
        == "acmerobotics.example"
    assert company_domain("https://boards.greenhouse.io/acme/jobs/1",
                          "https://job-boards.greenhouse.io/acme/jobs/1/apply", None) is None
    assert company_domain("https://jobs.lever.co/acme/1", None,
                          "https://www.acmerobotics.example/careers") == "acmerobotics.example"
    assert domain_matches("talent.acmerobotics.example", "acmerobotics.example")
    assert not domain_matches("acmerobotics.example.evil.example", "acmerobotics.example")
    assert not domain_matches("notacmerobotics.example", "acmerobotics.example")


CAREERS_HTML = """<html><body>
<a href="mailto:recruiting@acmerobotics.example?subject=Hi">Recruiting</a>
<a href="mailto:ceo@acmerobotics.example">CEO</a>
<a href="mailto:talent@elsewhere.example">Other</a>
<a href="MAILTO:Jobs@AcmeRobotics.example">Jobs</a>
<p>Email first.last@acmerobotics.example</p>
</body></html>"""


async def test_careers_inbox_allowlist() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=CAREERS_HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RateLimitedFetcher(client, min_interval_s=0)
        found = await fetch_careers_inbox("https://acmerobotics.example/careers", fetcher,
                                          "acmerobotics.example")
        assert sorted(c.address for c in found) == ["jobs@acmerobotics.example",
                                                    "recruiting@acmerobotics.example"]
        assert all(c.kind == "careers_inbox" and c.confidence == 0.7 for c in found)
        assert all(c.public_source_url == "https://acmerobotics.example/careers"
                   for c in found)
        assert len(seen) == 1 and seen[0].method == "GET"
        assert seen[0].headers["user-agent"].startswith("JobPilot/")
        # never fetched: non-http(s), loopback/private hosts
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "http://127.0.0.1/x",
                    "http://localhost/careers", "http://10.0.0.2/careers"):
            assert await fetch_careers_inbox(bad, fetcher, "acmerobotics.example") == []
        assert len(seen) == 1


async def test_careers_fetch_error_is_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RateLimitedFetcher(client, min_interval_s=0)
        assert await fetch_careers_inbox("https://acmerobotics.example/careers", fetcher,
                                         "acmerobotics.example") == []


def _c(kind: str, address: str, confidence: float) -> FoundContact:
    return FoundContact(kind=kind, name="x", professional_title=None,  # type: ignore[arg-type]
                        channel="email", address=address,
                        public_source_url="https://acmerobotics.example/careers",
                        confidence=confidence)


def test_rank_prefers_user_then_named() -> None:
    inbox = _c("careers_inbox", "careers@acmerobotics.example", 0.7)
    posted = _c("posting_inbox", "jobs@acmerobotics.example", 0.9)
    named = _c("posting_named", "priya@acmerobotics.example", 0.9)
    user = _c("user", "sam@acmerobotics.example", 1.0)
    assert rank([inbox, posted, named, user]) == [user, named, posted, inbox]
    dup = _c("careers_inbox", "priya@acmerobotics.example", 0.7)
    assert rank([dup, named]) == [named]


def test_outreach_config_and_careers_url() -> None:
    cfg = AppConfig(model_routes={})
    assert cfg.outreach == OutreachConfig(auto_draft=True, campaign="default")
    with pytest.raises(ValueError):
        OutreachConfig(campaign="bad campaign!")
    b = CompanyBoard(name="Acme", source="lever", token="acme",
                     extra={"careers_url": "https://acmerobotics.example/careers"})
    assert b.extra["careers_url"] == "https://acmerobotics.example/careers"
    for bad in ("javascript:alert(1)", "ftp://acmerobotics.example/x", "not a url"):
        with pytest.raises(ValueError):
            CompanyBoard(name="Acme", source="lever", token="acme",
                         extra={"careers_url": bad})


def _alembic(db: Path) -> Config:
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src/jobpilot/data/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


def test_migration_0009_upgrade_downgrade(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    cfg = _alembic(db)
    command.upgrade(cfg, "head")

    def cols(table: str) -> set[str]:
        engine = create_engine(f"sqlite:///{db}")
        try:
            return {c["name"] for c in inspect(engine).get_columns(table)}
        finally:
            engine.dispose()

    assert {"created_at", "application_id", "address", "kind"} <= cols("contact_candidate")
    assert {"created_at", "skip_reason", "sent_note"} <= cols("outreach_draft")
    command.downgrade(cfg, "0008_submit_live_intent")
    assert "application_id" not in cols("contact_candidate")
    assert "skip_reason" not in cols("outreach_draft")
    command.upgrade(cfg, "head")
    assert "skip_reason" in cols("outreach_draft")
