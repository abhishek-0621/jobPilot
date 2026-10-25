from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import FieldDecision
from jobpilot.documents.render import chromium_available
from jobpilot.forms.browser import portal_page
from jobpilot.portals.lever import LeverAdapter
from tests.browser.helpers import answer

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium not installed"),
]


@pytest.fixture
async def lever(form_server: str, tmp_path: Path) -> AsyncIterator[LeverAdapter]:
    cfg = AppConfig(storage_path=tmp_path / "storage", browser_headless=True, model_routes={})
    async with portal_page(cfg, "lever", {"127.0.0.1"}) as s:
        adapter = LeverAdapter(s.page, upload_root=tmp_path)
        await adapter.open_job(f"{form_server}/lever.html")
        yield adapter


async def test_inspect_lever_fields(lever: LeverAdapter) -> None:
    f = {x.field_id: x for x in (await lever.inspect_form()).fields}
    assert f["name"].label == "Full name" and f["name"].required
    assert f["urls[LinkedIn]"].label == "LinkedIn URL"
    assert f["cards[abc][field0]"].label == "Describe a system you scaled"
    assert f["cards[abc][field0]"].maxlength == 500 and f["name"].maxlength is None
    assert f["resume"].field_type == "file" and f["resume"].required
    assert f["resume"].label == "Resume/CV"


async def test_lever_full_name_and_urls(lever: LeverAdapter, tmp_path: Path) -> None:
    await lever.inspect_form()
    resume = tmp_path / "asha_resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic")
    answers = [
        answer("name", "Asha Example"),
        answer("urls[LinkedIn]", "https://linkedin.example/asha"),
        answer("cards[abc][field0]", "A queue.", decision=FieldDecision.FILL_AND_REVIEW),
        answer("resume", str(resume), "file"),
    ]
    report = await lever.fill_fields(answers)
    assert report.failed == {} and len(report.filled) == 4
    assert (await lever.validate_form({a.field_id: a.proposed_value for a in answers})).ok


async def test_never_clicks_submit(lever: LeverAdapter) -> None:
    await lever.inspect_form()
    await lever.fill_fields([answer("name", "Asha Example")])
    await lever.validate_form({"name": "Asha Example"})
    assert await lever.page.evaluate("window.__submitted === true") is False
