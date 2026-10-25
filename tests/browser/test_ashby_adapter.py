from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import FieldDecision
from jobpilot.documents.render import chromium_available
from jobpilot.forms.browser import portal_page
from jobpilot.portals.ashby import AshbyAdapter
from jobpilot.portals.registry import ADAPTERS, allowed_hosts, form_url
from tests.browser.helpers import answer

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium not installed"),
]


@pytest.fixture
async def ashby(form_server: str, tmp_path: Path) -> AsyncIterator[AshbyAdapter]:
    cfg = AppConfig(storage_path=tmp_path / "storage", browser_headless=True, model_routes={})
    async with portal_page(cfg, "ashby", {"127.0.0.1"}) as s:
        adapter = AshbyAdapter(s.page, upload_root=tmp_path)
        await adapter.open_job(f"{form_server}/ashby.html")
        yield adapter


def test_ashby_registered_with_asset_hosts() -> None:
    assert ADAPTERS["ashby"] is AshbyAdapter
    assert {"jobs.ashbyhq.com", "api.ashbyhq.com", "cdn.ashbyprd.com",
            "app.ashbyhq.com"} <= allowed_hosts(AshbyAdapter, "https://jobs.ashbyhq.com/x/1")


def test_ashby_form_url() -> None:
    assert form_url({"source": "ashby", "application_url": "https://jobs.ashbyhq.com/o/1/application",
                     "canonical_url": "https://jobs.ashbyhq.com/o/1"}) == \
        "https://jobs.ashbyhq.com/o/1/application"
    assert form_url({"source": "ashby", "canonical_url": "https://jobs.ashbyhq.com/o/1/"}) == \
        "https://jobs.ashbyhq.com/o/1/application"
    assert AshbyAdapter.apply_url("https://jobs.ashbyhq.com/o/1/application?x=1") == \
        "https://jobs.ashbyhq.com/o/1/application"


async def test_inspect_ashby_fields(ashby: AshbyAdapter) -> None:
    f = {x.field_id: x for x in (await ashby.inspect_form()).fields}
    assert f["_systemfield_name"].label == "Name" and f["_systemfield_name"].required
    assert f["_systemfield_email"].field_type == "email"
    assert f["_systemfield_resume"].field_type == "file"
    assert f["phone_q1"].label == "Phone" and not f["phone_q1"].required
    assert f["linkedin_q2"].label == "LinkedIn URL"
    assert f["auth_q4"].field_type == "radio"
    assert "declaration_q5" in f


async def test_ashby_fill_and_read_back(ashby: AshbyAdapter, tmp_path: Path) -> None:
    await ashby.inspect_form()
    resume = tmp_path / "asha_resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic")
    answers = [
        answer("_systemfield_name", "Asha Example"),
        answer("_systemfield_email", "asha@example.com", "email"),
        answer("linkedin_q2", "https://linkedin.example/asha"),
        answer("why_q3", "A queue.", decision=FieldDecision.FILL_AND_REVIEW),
        answer("_systemfield_resume", str(resume), "file"),
    ]
    report = await ashby.fill_fields(answers)
    assert report.failed == {} and len(report.filled) == 5
    assert (await ashby.validate_form({a.field_id: a.proposed_value for a in answers})).ok


async def test_never_clicks_submit(ashby: AshbyAdapter) -> None:
    await ashby.inspect_form()
    await ashby.fill_fields([answer("_systemfield_name", "Asha Example")])
    await ashby.validate_form({"_systemfield_name": "Asha Example"})
    assert await ashby.page.evaluate("window.__submitted === true") is False
