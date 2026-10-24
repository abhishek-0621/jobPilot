from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jobpilot.config import AppConfig
from jobpilot.contracts.enums import FieldDecision, PortalCapability
from jobpilot.documents.render import chromium_available
from jobpilot.forms.browser import portal_page
from jobpilot.portals.base import FormSnapshot
from jobpilot.portals.greenhouse import GreenhouseAdapter
from tests.browser.helpers import answer

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium not installed"),
]

GH = "job_application"


@pytest.fixture
async def gh(form_server: str, tmp_path: Path) -> AsyncIterator[GreenhouseAdapter]:
    cfg = AppConfig(storage_path=tmp_path / "storage", browser_headless=True, model_routes={})
    async with portal_page(cfg, "greenhouse", {"127.0.0.1"}) as s:
        adapter = GreenhouseAdapter(s.page, upload_root=tmp_path)
        await adapter.open_job(f"{form_server}/greenhouse.html")
        yield adapter


def _by_id(snap: FormSnapshot) -> dict[str, Any]:
    return {f.field_id: f for f in snap.fields}


async def test_capabilities_prepare_only(gh: GreenhouseAdapter) -> None:
    assert await gh.capabilities() == {PortalCapability.PREPARE_APPLICATION}
    with pytest.raises(NotImplementedError):
        await gh.verify_submission()


async def test_inspect_greenhouse_fields(gh: GreenhouseAdapter) -> None:
    f = _by_id(await gh.inspect_form())
    first = f[f"{GH}[first_name]"]
    assert (first.label, first.field_type, first.required) == ("First Name", "text", True)
    assert f[f"{GH}[phone]"].required is False
    assert f[f"{GH}[resume]"].field_type == "file" and f[f"{GH}[resume]"].required
    assert f[f"{GH}[cover_letter]"].label == "Attach cover letter"
    q1 = f[f"{GH}[answers][question_1001]"]
    assert q1.label == "Why do you want to work here?"
    assert q1.help_text == "A few sentences is plenty."
    sel = f[f"{GH}[answers][question_1002]"]
    assert sel.field_type == "select" and sel.options == ["Job board", "Referral", "Other"]
    radio = f["question_1003"]
    assert radio.field_type == "radio" and radio.options == ["Yes", "No"] and radio.required
    assert radio.label == "Are you legally authorized to work in this country?"
    box = f["question_1004"]
    assert box.field_type == "checkbox" and box.options == ["I agree to the privacy policy"]
    assert not any(x.field_type in {"submit", "button"} for x in f.values())


async def test_snapshot_hash_stable(gh: GreenhouseAdapter) -> None:
    a = await gh.inspect_form()
    b = await gh.inspect_form()
    assert gh.snapshot_hash(a) == gh.snapshot_hash(b)
    changed = a.model_copy(update={"fields": a.fields[1:]})
    assert gh.snapshot_hash(changed) != gh.snapshot_hash(a)


async def test_fill_and_readback(gh: GreenhouseAdapter, tmp_path: Path) -> None:
    await gh.inspect_form()
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic")
    answers = [
        answer(f"{GH}[first_name]", "Asha"),
        answer(f"{GH}[email]", "asha@example.com"),
        answer(f"{GH}[resume]", str(resume), "file"),
        answer(f"{GH}[answers][question_1001]", "Interesting work.",
               decision=FieldDecision.FILL_AND_REVIEW),
        answer(f"{GH}[answers][question_1002]", "Referral", "select"),
        answer("question_1003", "Yes", "radio", decision=FieldDecision.USER_ONLY,
               source="user"),
    ]
    report = await gh.fill_fields(answers)
    assert report.failed == {}
    assert set(report.filled) == {a.field_id for a in answers}
    expected = {a.field_id: a.proposed_value for a in answers}
    assert (await gh.validate_form(expected)).ok
    wrong = await gh.validate_form({f"{GH}[first_name]": "Someone"})
    assert not wrong.ok and f"{GH}[first_name]" in wrong.errors


async def test_never_fills_gated_or_declarations(gh: GreenhouseAdapter) -> None:
    await gh.inspect_form()
    label = "I agree to the privacy policy"
    report = await gh.fill_fields([
        answer("question_1003", "Yes", "radio", decision=FieldDecision.USER_ONLY,
               source="fact_x"),
        answer(f"{GH}[phone]", "+1 555", decision=FieldDecision.ASK_USER, source=None),
        answer(f"{GH}[last_name]", "Example", decision=FieldDecision.BLOCKED),
        answer("question_1004", True, "checkbox", label=label),
    ])
    assert report.filled == [] and report.failed == {}
    v = await gh.validate_form({"question_1003": None, "question_1004": False,
                                f"{GH}[phone]": ""})
    assert v.ok, v.errors
    report = await gh.fill_fields(
        [answer("question_1004", True, "checkbox", label=label,
                decision=FieldDecision.USER_ONLY, source="user")])
    assert report.filled == ["question_1004"]
    assert (await gh.validate_form({"question_1004": True})).ok


async def test_never_clicks_submit(gh: GreenhouseAdapter) -> None:
    await gh.inspect_form()
    await gh.fill_fields([answer(f"{GH}[first_name]", "Asha")])
    await gh.validate_form({f"{GH}[first_name]": "Asha"})
    assert await gh.page.evaluate("window.__submitted === true") is False
    with pytest.raises(NotImplementedError):
        await gh.submit(None)  # type: ignore[arg-type]


async def test_layout_change_detected(gh: GreenhouseAdapter) -> None:
    await gh.inspect_form()
    report = await gh.fill_fields([answer(f"{GH}[middle_name]", "Q")])
    assert report.failed == {f"{GH}[middle_name]": "FORM_LAYOUT_CHANGED"}
    v = await gh.validate_form({f"{GH}[middle_name]": "Q"})
    assert v.errors == {f"{GH}[middle_name]": "FORM_LAYOUT_CHANGED"}


async def test_missing_file_fails(gh: GreenhouseAdapter, tmp_path: Path) -> None:
    report = await gh.fill_fields([answer(f"{GH}[resume]", str(tmp_path / "nope.pdf"), "file")])
    assert f"{GH}[resume]" in report.failed


async def test_page_validation_messages(gh: GreenhouseAdapter) -> None:
    await gh.page.evaluate("""() => {
      const e = document.createElement('div'); e.className = 'field-error';
      e.textContent = 'Email is invalid';
      document.getElementById('email').parentElement.appendChild(e); }""")
    v = await gh.validate_form({})
    assert v.errors == {f"{GH}[email]": "Email is invalid"}


async def test_captcha_blocks(gh: GreenhouseAdapter, form_server: str) -> None:
    await gh.open_job(f"{form_server}/captcha.html")
    assert await gh.blockers() == ["captcha"]
    report = await gh.fill_fields([answer("first_name", "Asha")])
    assert report.filled == [] and report.failed == {"first_name": "BLOCKED:captcha"}
    assert await gh.page.input_value("#first_name") == ""


async def test_hidden_sibling_with_same_name_ignored(gh: GreenhouseAdapter) -> None:
    f = _by_id(await gh.inspect_form())
    assert f["job_application[consent]"].field_type == "checkbox"
    report = await gh.fill_fields([answer("job_application[consent]", True, "checkbox",
                                          label="Send me job alerts")])
    assert report.filled == ["job_application[consent]"], report.failed
    assert (await gh.validate_form({"job_application[consent]": True})).ok


async def test_checkbox_group(gh: GreenhouseAdapter) -> None:
    f = _by_id(await gh.inspect_form())
    grp = f["locations[]"]
    assert grp.field_type == "checkbox_group" and grp.options == ["Remote", "Onsite"]
    assert grp.label == "Preferred locations"
    report = await gh.fill_fields([answer("locations[]", ["Remote"], "checkbox_group")])
    assert report.filled == ["locations[]"], report.failed
    assert (await gh.validate_form({"locations[]": ["Remote"]})).ok
    assert not (await gh.validate_form({"locations[]": ["Onsite"]})).ok


async def test_duplicate_field_id_warns(gh: GreenhouseAdapter) -> None:
    snap = await gh.inspect_form()
    assert [x.field_id for x in snap.fields].count("dup") == 1
    assert any("dup" in w for w in snap.warnings)


async def test_user_values_param_removed(gh: GreenhouseAdapter) -> None:
    await gh.inspect_form()
    with pytest.raises(TypeError):
        await gh.fill_fields([], user_values={"x"})  # type: ignore[call-arg]


async def test_declaration_gate_uses_page_label(gh: GreenhouseAdapter) -> None:
    await gh.inspect_form()
    report = await gh.fill_fields([answer("question_1004", True, "checkbox",
                                          label="checkbox 4")])
    assert report.filled == []


async def test_upload_outside_root_refused(gh: GreenhouseAdapter, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}_outside.pdf"
    outside.write_bytes(b"%PDF-1.4 synthetic")
    try:
        report = await gh.fill_fields([answer(f"{GH}[resume]", str(outside), "file")])
    finally:
        outside.unlink()
    assert report.failed == {f"{GH}[resume]": "UPLOAD_OUTSIDE_ROOT"}
