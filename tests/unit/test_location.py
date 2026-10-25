import json
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import JobStatus
from jobpilot.data.repositories import list_events, upsert_job
from jobpilot.discovery.location import is_india, location_eligible, remote_open_to_india
from jobpilot.discovery.normalize import company_key
from jobpilot.discovery.prefilter import prefilter
from jobpilot.discovery.sources.base import RawPosting
from jobpilot.profile.preferences import SearchPreferences
from tests.factories import NOW, make_listing

PREFS = SearchPreferences(target_titles=["AI Engineer"], locations=["India", "Remote"],
                          work_modes=["remote", "hybrid", "onsite"])


@pytest.mark.parametrize("loc", ["Bengaluru", "Bangalore, Karnataka", "Gurgaon", "Pune, MH",
                                 "Navi Mumbai", "Hyderabad, Telangana", "New Delhi", "India"])
def test_indian_places_match_india(loc: str) -> None:
    assert is_india(loc)
    assert location_eligible(loc, PREFS, remote_board=False) == (True, "ok")


@pytest.mark.parametrize("loc", ["Remote - USA only", "Remote (US)", "Remote, EU",
                                 "Remote - UK", "Remote - Europe", "Remote, Americas",
                                 "Remote LATAM", "Remote - Canada", "Remote - Berlin"])
def test_remote_restricted_elsewhere_rejected(loc: str) -> None:
    assert location_eligible(loc, PREFS, remote_board=False) == (
        False, "remote_region_mismatch")


@pytest.mark.parametrize("loc", ["Remote (Worldwide)", "Remote, APAC", "Anywhere", "Remote",
                                 "Remote - India", "Remote (IST)", "Remote, UTC+5:30",
                                 "Anywhere in the World", "Asia-Pacific (Remote)",
                                 "Europe, LATAM, APAC, Canada"])
def test_remote_open_to_india_accepted(loc: str) -> None:
    assert location_eligible(loc, PREFS, remote_board=True) == (True, "ok")
    assert remote_open_to_india(loc)


def test_remote_board_unknown_location_is_worldwide() -> None:
    assert location_eligible(None, PREFS, remote_board=True) == (True, "ok")
    assert location_eligible("", PREFS, remote_board=True) == (True, "ok")
    # a bare foreign country on a remote board is a region restriction
    assert location_eligible("United States", PREFS, remote_board=True) == (
        False, "remote_region_mismatch")
    # company boards keep the existing behaviour for missing locations
    assert location_eligible(None, PREFS, remote_board=False) == (True, "ok")


def test_onsite_elsewhere_is_location_mismatch() -> None:
    assert location_eligible("Berlin, Germany", PREFS, remote_board=False) == (
        False, "location_mismatch")


def test_remote_not_wanted() -> None:
    prefs = PREFS.model_copy(update={"locations": ["India"], "work_modes": ["onsite"]})
    assert location_eligible("Remote (Worldwide)", prefs, remote_board=True)[0] is False
    assert location_eligible("Pune", prefs, remote_board=False) == (True, "ok")


def test_legacy_city_prefs_still_work() -> None:
    prefs = SearchPreferences(locations=["Bengaluru"], work_modes=["remote"])
    assert location_eligible("Bengaluru", prefs, remote_board=False) == (True, "ok")
    assert location_eligible("Remote", prefs, remote_board=False) == (True, "ok")
    assert location_eligible("Remote - USA only", prefs, remote_board=False)[0] is False


def test_prefilter_uses_remote_board_rules() -> None:
    p = RawPosting(source="remotive", source_job_id="1", company="Example Corp",
                   title="AI Engineer", location="USA only", description_html="",
                   canonical_url="https://example.com/1", application_url=None,
                   posted_at=None)
    assert prefilter(p, PREFS) == (False, "remote_region_mismatch")
    assert prefilter(p.model_copy(update={"location": None}), PREFS) == (True, "ok")


def test_company_key_ignores_suffixes() -> None:
    assert company_key("Example Corp, Inc.") == company_key("example corp")
    assert company_key("Sample Labs Pvt Ltd") == company_key("Sample Labs")


def test_cross_source_dedupe_by_company_and_title(db_session: Session) -> None:
    a = make_listing(job_id="job_a", source="weworkremotely", source_job_id="w1",
                     canonical_url="https://example.com/wwr/1", company="Example Corp",
                     title="Senior AI Engineer", location="Anywhere in the World")
    b = make_listing(job_id="job_b", source="himalayas", source_job_id="h1",
                     canonical_url="https://example.com/him/1", company="Example Corp Inc.",
                     title="Sr. AI Engineer", location="India",
                     first_seen_at=NOW + timedelta(days=2), last_seen_at=NOW + timedelta(days=2))
    row_a, created_a = upsert_job(db_session, a)
    row_b, created_b = upsert_job(db_session, b)
    assert created_a and not created_b
    assert row_b.status == JobStatus.DUPLICATE.value and row_b.canonical_job_id == row_a.job_id
    [ev] = [e for e in list_events(db_session, row_b.job_id) if e.action == "job_duplicate"]
    details = json.loads(ev.redacted_details_json)
    assert details["match"] == "company_title" and details["source"] == "himalayas"


def test_cross_source_dedupe_window_is_30_days(db_session: Session) -> None:
    a = make_listing(job_id="job_a", source="remoteok", source_job_id="r1",
                     canonical_url="https://example.com/rok/1", company="Example Corp",
                     title="AI Engineer", location="Remote")
    later = NOW + timedelta(days=31)
    b = make_listing(job_id="job_b", source="jobicy", source_job_id="j1",
                     canonical_url="https://example.com/jcy/1", company="Example Corp",
                     title="AI Engineer", location="APAC", first_seen_at=later,
                     last_seen_at=later)
    upsert_job(db_session, a)
    _, created = upsert_job(db_session, b)
    assert created


def test_same_source_same_title_not_cross_deduped(db_session: Session) -> None:
    a = make_listing(job_id="job_a", source="greenhouse", source_job_id="1",
                     canonical_url="https://example.com/1", company="Example Corp",
                     title="AI Engineer", location="Pune")
    b = make_listing(job_id="job_b", source="greenhouse", source_job_id="2",
                     canonical_url="https://example.com/2", company="Example Corp",
                     title="AI Engineer", location="Chennai")
    upsert_job(db_session, a)
    _, created = upsert_job(db_session, b)
    assert created


def test_company_boards_and_manual_not_cross_deduped(db_session: Session) -> None:
    a = make_listing(job_id="job_a", source="manual", source_job_id=None,
                     canonical_url="https://example.com/m/1", company="Example Corp",
                     title="AI Engineer", location="Bengaluru")
    b = make_listing(job_id="job_b", source="greenhouse", source_job_id="9",
                     canonical_url="https://example.com/gh/9", company="Example Corp",
                     title="AI Engineer", location="Hyderabad")
    upsert_job(db_session, a)
    _, created = upsert_job(db_session, b)
    assert created
    c = make_listing(job_id="job_c", source="remotive", source_job_id="r9",
                     canonical_url="https://example.com/rem/9", company="Example Corp",
                     title="AI Engineer", location="Worldwide")
    row_c, created_c = upsert_job(db_session, c)
    assert not created_c and row_c.status == JobStatus.DUPLICATE.value
