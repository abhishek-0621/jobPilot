"""Plan 10 Task 3: discovery, matching & documents correctness."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents import cover_letter as cl_mod
from jobpilot.agents.cover_letter import _mask, make_cover_letter_handler
from jobpilot.agents.extract import _truncate_words, extract_handler
from jobpilot.config import AppConfig
from jobpilot.data.tables import AuditEventRow, JobRow, TaskRow
from jobpilot.discovery.normalize import normalize_url
from jobpilot.discovery.prefilter import prefilter
from jobpilot.discovery.sources.base import RawPosting
from jobpilot.orchestrator.engine import OutputRejected
from jobpilot.profile.preferences import SearchPreferences
from jobpilot.util.textmatch import has_phrase
from tests.agent_contracts.test_cover_letter import (  # noqa: F401
    GOOD,
    SeqGateway,
    fake_pdf,
    run_agent,
    seed_cl,
)
from tests.agent_contracts.test_tailor import APP, cfg, factory  # noqa: F401
from tests.factories import make_app_row


def _raw(**kw) -> RawPosting:
    base = dict(source="greenhouse", source_job_id="1", company="Acme", title="AI Engineer",
                location="Pune", description_html="<p>Build</p>",
                canonical_url="https://example.com/1", application_url=None, posted_at=None)
    return RawPosting(**{**base, **kw})


def test_location_matches_on_word_boundaries() -> None:
    prefs = SearchPreferences(target_titles=["AI Engineer"], locations=["Pune"])
    assert prefilter(_raw(location="Pune, India"), prefs)[0]
    assert prefilter(_raw(location="Puneet Nagar"), prefs) == (False, "location_mismatch")


def test_exclude_keywords_checked_in_description() -> None:
    prefs = SearchPreferences(target_titles=["AI Engineer"], locations=["Pune"],
                              exclude_keywords=["clearance"])
    ok, reason = prefilter(_raw(description_html="<p>Needs security clearance</p>"), prefs)
    assert not ok and reason == "excluded_keyword:clearance"


@pytest.mark.parametrize("text,term,hit", [
    ("we use c++ daily", "c++", True), ("c++17 code", "c++", False),
    ("asp .net core", ".net", True), ("dotnet", ".net", False),
    ("c# and f#", "c#", True), ("node.js services", "node.js", True),
    ("nodejs", "node.js", False)])
def test_symbol_tech_terms(text: str, term: str, hit: bool) -> None:
    assert has_phrase(text, term) is hit


@pytest.mark.parametrize("url,want", [
    ("https://Example.com:443/jobs/1/", "https://example.com/jobs/1"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("https://example.com:8443/a", "https://example.com:8443/a")])
def test_default_ports_stripped(url: str, want: str) -> None:
    assert normalize_url(url) == want


def test_profile_category_path_urlencoded() -> None:
    tpl = Path("src/jobpilot/dashboard/templates/profile.html").read_text(encoding="utf-8")
    assert "/ui/profile/sections/{{ category|urlencode }}/verify" in tpl


def test_summary_truncated_at_word_boundary() -> None:
    s = "alpha beta gamma delta"
    assert _truncate_words(s, 13) == "alpha beta"
    assert _truncate_words(s, 100) == s
    assert len(_truncate_words("x" * 50, 10)) == 10


def test_malformed_extracted_json_recorded(db_session) -> None:
    from jobpilot.agents.extract import JobRequirements

    app = make_app_row(db_session)
    job = db_session.get(JobRow, app.job_id)
    job.extracted_json = "{broken"
    db_session.flush()
    out = JobRequirements(job_id=job.job_id, task_id="t", created_at=job.first_seen_at,
                          required_skills=[], preferred_skills=[], experience_min_years=None,
                          experience_max_years=None, seniority="unknown", work_mode="unknown",
                          location=None, role_family="other", summary="s",
                          extraction_confidence=0.9)
    task = TaskRow(task_id="t", kind="EXTRACT", entity_type="job", entity_id=job.job_id,
                   status="RUNNING", attempts=1)
    extract_handler(db_session, task, out)
    acts = [e.action for e in db_session.execute(select(AuditEventRow).where(
        AuditEventRow.entity_id == job.job_id)).scalars()]
    assert "extracted_json_malformed" in acts
    assert "requirements" in json.loads(job.extracted_json)


def test_partial_role_mention_not_masked() -> None:
    """Partial title mentions stay visible to validation (masking them would let an
    untrusted sub-phrase such as "40-person team" pass as source)."""
    out = _mask("I led a 40-person team.", "Example AI", "Lead of 40-person team")
    assert "40-person team" in out


async def test_orphan_pdf_removed_when_txt_write_fails(
    factory: sessionmaker[Session], cfg: AppConfig, fake_pdf: list[str],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_cl(factory)
    real = Path.write_text

    def boom(self: Path, *a, **kw):
        if self.suffix == ".txt" and self.name.startswith("cover_letter"):
            raise OSError("disk full")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        await run_agent(factory, cfg, SeqGateway(GOOD))
    out_dir = cfg.storage_path / "artifacts" / APP
    assert not list(out_dir.glob("cover_letter_v*.pdf"))


async def test_handler_cross_checks_company_and_role(
    factory: sessionmaker[Session], cfg: AppConfig, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed_cl(factory)
    out = await run_agent(factory, cfg, SeqGateway(GOOD))
    task = TaskRow(task_id="t_cl", kind="COVER_LETTER", entity_type="application",
                   entity_id=APP, status="RUNNING", attempts=1)
    for bad in ({"company": "Globex Labs"}, {"role": "Other Role"}):
        with factory() as s:
            with pytest.raises(OutputRejected, match="company_role_mismatch"):
                make_cover_letter_handler(cfg.storage_path)(s, task, out.model_copy(
                    update=bad))


_ = cl_mod
