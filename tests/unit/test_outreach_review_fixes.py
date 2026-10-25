"""Review findings: third-party hosts, company-key suffixes, opening sanitizing,
duplicate race and retention of contacts still under review."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from jobpilot.agents import outreach as outreach_mod
from jobpilot.agents.outreach import opening
from jobpilot.data import mutations
from jobpilot.data.tables import AuditEventRow, ContactCandidateRow, OutreachDraftRow
from jobpilot.discovery.sources.base import RateLimitedFetcher
from jobpilot.ops.maintenance import run_maintenance
from jobpilot.outreach.contacts import company_domain, extract_from_posting, fetch_careers_inbox
from jobpilot.outreach.state import company_key
from tests.unit.test_outreach_flow import _drafted, _orch, cfg, factory, seed_app  # noqa: F401


@pytest.mark.parametrize("url", [
    "https://acme.wd5.myworkdaysite.com/en-US/recruiting/acme/job/1",
    "https://jobs.jobvite.com/acme/job/1", "https://apply.workable.com/acme/j/1",
    "https://careers-acme.icims.com/jobs/1", "https://acme.bamboohr.com/careers/1",
    "https://acme.recruitee.com/o/1", "https://acme.taleo.net/careersection/1",
    "https://acme.breezy.hr/p/1", "https://wellfound.com/jobs/1",
    "https://acme.applytojob.com/apply/1", "https://acme.teamtailor.com/jobs/1",
    "https://acme.jobs.personio.de/job/1", "https://ats.rippling.com/acme/jobs/1",
    "https://app.dover.com/apply/acme/1", "https://acme.keka.com/careers/1",
    "https://acme.darwinbox.in/ms/candidate/1", "https://acme.zohorecruit.com/jobs/1",
    "https://acme.freshteam.com/jobs/1", "https://acme.github.io/jobs",
    "https://acme.herokuapp.com/jobs", "https://acme.vercel.app/jobs",
    "https://acme.netlify.app/jobs"])
def test_third_party_hosts_are_not_company_domains(url: str) -> None:
    assert company_domain(url, None, None) is None


def test_vendor_email_never_kept_for_company() -> None:
    dom = company_domain("https://jobs.jobvite.com/acme/job/1", None, None)
    assert extract_from_posting("Help: support@jobvite.com", dom, company="Acme") == []
    # even if a vendor domain were passed in as the company domain
    assert extract_from_posting("Help: careers@jobvite.com", "jobvite.com",
                                company="Acme") == []
    # slug fallback still works for the company's own domain
    assert [c.address for c in extract_from_posting(
        "careers@acme.example", "jobvite.com", company="Acme")] == ["careers@acme.example"]


async def test_vendor_careers_inbox_rejected() -> None:
    html = '<a href="mailto:careers@jobvite.com">x</a>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=html))) as client:
        fetcher = RateLimitedFetcher(client, min_interval_s=0)
        assert await fetch_careers_inbox("https://acme.example/careers", fetcher,
                                         "jobvite.com") == []


async def test_vendor_email_user_added_rejected(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        with pytest.raises(ValueError):
            mutations.add_contact(s, oid, name="Pat", title=None, channel="email",
                                  address="careers@jobvite.com",
                                  source_url="https://jobs.jobvite.com/acme",
                                  company_domain="jobvite.com", company="Acme")


def test_company_key_suffix_variants_dedupe() -> None:
    keys = {company_key(n) for n in ("Acme", "Acme, Inc.", "ACME Technologies Pvt. Ltd.",
                                     "Acme LLC", "Acme GmbH", "Acme Labs India")}
    assert keys == {"acme"}
    assert company_key("AI") == "ai"  # at least one token kept
    assert company_key("Acme Robotics") != company_key("Acme")


def test_opening_sanitizes_untrusted_title() -> None:
    assert opening("Engineer [Remote] mail x@evil.example", "Acme", None) == \
        "I recently applied for the role at Acme."
    assert opening("Engineer", "Acme {{x}}", None) == \
        "I recently applied for the Engineer role at the company."


async def test_duplicate_race_records_event(factory, cfg, monkeypatch) -> None:  # noqa: F811
    _app, _oid = await _drafted(factory, cfg)
    with factory() as s:
        other = seed_app(s, company="Acme Robotics")
    orch = _orch(factory, cfg)
    with factory() as s:
        from jobpilot.orchestrator.engine import TaskKind, add_task
        add_task(s, TaskKind.OUTREACH_RESEARCH, "application", other)
        s.commit()
    monkeypatch.setattr(outreach_mod, "existing_draft", lambda *a: None)
    await orch.run_until_idle()
    with factory() as s:
        assert len(list(s.execute(select(OutreachDraftRow)).scalars())) == 1
        assert s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "outreach_duplicate_company",
            AuditEventRow.entity_id == other)).scalars().first() is not None


async def test_prune_keeps_contacts_under_review(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        for c in s.execute(select(ContactCandidateRow)).scalars():
            c.created_at = datetime.now(UTC) - timedelta(days=400)
        s.commit()
    assert run_maintenance(cfg)["contacts_pruned"] == 0
    with factory() as s:
        d = s.get(OutreachDraftRow, oid)
        assert d is not None and d.contact_id is not None and d.status == "NEEDS_REVIEW"


