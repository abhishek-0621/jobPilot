from pathlib import Path

from sqlalchemy import select

from jobpilot.contracts.enums import JobStatus
from jobpilot.data.repositories import list_events, upsert_job
from jobpilot.data.tables import JobRow
from jobpilot.discovery.normalize import (
    dedupe_key,
    html_to_text,
    normalize_title,
    normalize_url,
)
from tests.factories import make_listing

PAGES = Path(__file__).parent.parent / "fixtures" / "job_pages"


def test_normalize_url_strips_tracking() -> None:
    assert normalize_url(
        "HTTPS://www.Example.com/jobs/123/?utm_source=x&b=2&a=1&gh_jid=9&Ref=z#frag"
    ) == "https://example.com/jobs/123?a=1&b=2"
    assert normalize_url("https://example.com/") == "https://example.com/"
    assert normalize_url("https://example.com/?lever-source%5B%5D=a") == "https://example.com/"


def test_dedupe_key_title_variants() -> None:
    assert normalize_title("Sr. AI Engineer (GenAI)") == normalize_title("Senior AI Engineer")
    assert normalize_title("ML Engg") == "ml engineer"
    assert dedupe_key("Acme ", "Sr. AI Engineer (GenAI)", "Pune, India") == dedupe_key(
        "acme", "Senior AI Engineer", "pune")


def test_html_to_text_strips_scripts_and_truncates() -> None:
    text = html_to_text((PAGES / "hostile.html").read_text())
    assert "IGNORE PREVIOUS INSTRUCTIONS" in text
    for gone in ("tracking", "secret", "Home", "Copyright", "enable js", "logo", "color: red"):
        assert gone not in text
    assert "Build & ship retrieval systems." in text
    assert "- Python\n- Evaluation harnesses" in text
    assert "\n\n\n" not in text
    big = html_to_text("<p>" + "word " * 20000 + "</p>")
    assert len(big) <= 12012 and big.endswith("\n[truncated]")


def test_html_to_text_plain_input() -> None:
    assert html_to_text("Just text &amp; more") == "Just text & more"
    assert html_to_text((PAGES / "plain.html").read_text()) == (
        "We are hiring an engineer.\nWork with the platform team.")


def test_cross_source_duplicate_links_canonical(db_session) -> None:
    a, created_a = upsert_job(db_session, make_listing(
        source="greenhouse", source_job_id="1", canonical_url="https://www.example.com/j/1?gh_src=x"))
    b, created_b = upsert_job(db_session, make_listing(
        source="lever", source_job_id="9", canonical_url="https://example.com/j/1"))
    assert created_a and not created_b
    assert a.status == JobStatus.DISCOVERED.value
    assert a.canonical_url == "https://example.com/j/1"
    assert b.status == JobStatus.DUPLICATE.value and b.canonical_job_id == a.job_id
    ev = [e for e in list_events(db_session, b.job_id) if e.action == "job_duplicate"]
    assert ev and '"match":"url"' in ev[0].redacted_details_json.replace(" ", "")
    # same content, different URL -> dedupe_key match
    c, created_c = upsert_job(db_session, make_listing(
        source="ashby", source_job_id="5", canonical_url="https://other.example.org/x"))
    assert not created_c and c.status == JobStatus.DUPLICATE.value
    assert c.canonical_job_id == a.job_id
    # duplicates are never canonical targets
    d, _ = upsert_job(db_session, make_listing(
        source="x", source_job_id="6", canonical_url="https://other.example.org/x"))
    assert d.canonical_job_id == a.job_id
    assert len(db_session.execute(select(JobRow)).scalars().all()) == 4


def test_html_to_text_deep_nesting() -> None:
    html = "<div>" * 5000 + "deep text" + "</div>" * 5000
    assert html_to_text(html) == "deep text"
