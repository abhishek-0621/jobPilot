"""Open the form, check it is the layout that was read, fill the stored answers and read
them back. Shared by VALIDATE and the live pre-submit check; never clicks submit."""

from dataclasses import dataclass, field
from typing import Any

from jobpilot.contracts.models import FieldAnswer
from jobpilot.portals.base import FillReport, ValidationReport
from jobpilot.portals.playwright_form import PlaywrightFormAdapter


@dataclass
class VerifyOutcome:
    blockers: list[str] = field(default_factory=list)
    layout_changed: bool = False
    report: FillReport | None = None
    checked: ValidationReport | None = None

    @property
    def ok(self) -> bool:
        return (not self.blockers and not self.layout_changed
                and self.report is not None and not self.report.failed
                and self.checked is not None and self.checked.ok)

    @property
    def errors(self) -> dict[str, str]:
        """Raw per-field errors (callers reduce them to codes)."""
        checked = self.checked.errors if self.checked else {}
        failed = self.report.failed if self.report else {}
        return {**checked, **failed}


def with_bound_files(answers: list[FieldAnswer], app: dict[str, Any]) -> list[FieldAnswer]:
    """File answers upload only the currently bound artifacts; anything else is dropped."""
    bound = {"artifact:resume": app.get("resume_path"),
             "artifact:cover_letter": app.get("cover_letter_path")}
    out: list[FieldAnswer] = []
    for a in answers:
        if a.field_type != "file":
            out.append(a)
        elif bound.get(a.source_fact_id or ""):
            out.append(a.model_copy(update={"proposed_value": str(bound[a.source_fact_id
                                                                          or ""])}))
    return out


async def verify_against_stored(adapter: PlaywrightFormAdapter, url: str,
                                expected_snapshot_hash: str | None,
                                answers: list[FieldAnswer]) -> VerifyOutcome:
    """open -> blockers -> inspect + snapshot-hash compare -> fill -> validate. Stops at the
    first failing stage; ``answers`` must already carry the bound files."""
    await adapter.open_job(url)
    blockers = await adapter.blockers()
    if blockers:
        return VerifyOutcome(blockers=sorted(blockers))
    snap = await adapter.inspect_form()
    if not expected_snapshot_hash or adapter.snapshot_hash(snap) != expected_snapshot_hash:
        return VerifyOutcome(layout_changed=True)
    report = await adapter.fill_fields(answers)
    expected = {a.field_id: a.proposed_value for a in answers if a.field_id in report.filled}
    checked = await adapter.validate_form(expected)
    return VerifyOutcome(report=report, checked=checked)
