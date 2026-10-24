import asyncio

import pytest

from jobpilot.contracts.enums import ApplicationStatus, PortalCapability
from jobpilot.portals.base import ApprovedApplication, idempotency_key
from jobpilot.portals.simulated import SimulatedAdapter


def _app() -> ApprovedApplication:
    return ApprovedApplication(
        application_id="app1",
        approved_hash="h",
        idempotency_key=idempotency_key("app1", "simulated", "submit", "1"),
    )


async def test_submit_twice_same_key_single_record() -> None:
    adapter = SimulatedAdapter()
    first = await adapter.submit(_app())
    second = await adapter.submit(_app())
    assert first == second
    assert first.status == ApplicationStatus.CONFIRMED
    assert adapter.submit_calls == 2
    assert adapter.simulated_submissions == 1
    assert adapter.real_submissions == 0


async def test_timeout_mode_raises() -> None:
    adapter = SimulatedAdapter(fail_mode="timeout")
    with pytest.raises(asyncio.TimeoutError):
        await adapter.submit(_app())
    assert adapter.submissions == {}
    assert adapter.submit_calls == 1


async def test_find_existing_after_submit() -> None:
    adapter = SimulatedAdapter()
    app = _app()
    assert await adapter.find_existing_submission(app.idempotency_key) is None
    result = await adapter.submit(app)
    assert await adapter.find_existing_submission(app.idempotency_key) == result


async def test_no_confirmation_mode_uncertain_and_recorded() -> None:
    adapter = SimulatedAdapter(fail_mode="no_confirmation")
    result = await adapter.submit(_app())
    assert result.status == ApplicationStatus.SUBMISSION_UNCERTAIN
    assert adapter.simulated_submissions == 1


async def test_capabilities_exclude_outreach_and_idempotency_key_stable() -> None:
    caps = await SimulatedAdapter().capabilities()
    assert PortalCapability.SEND_OUTREACH not in caps
    assert PortalCapability.SUBMIT_APPLICATION in caps
    assert idempotency_key("a", "p", "s", "1") == idempotency_key("a", "p", "s", "1")


async def test_validate_requires_filled_fields() -> None:
    adapter = SimulatedAdapter()
    assert (await adapter.validate_form()).ok is False


async def test_reference_uses_hex_after_prefix_and_form_is_per_instance() -> None:
    app = _app()
    result = await SimulatedAdapter().submit(app)
    hex8 = app.idempotency_key.split(":", 1)[-1][:8]
    assert result.confirmation_reference == f"SIM-{hex8}"
    assert result.evidence_url == f"simulated://confirmation/{hex8}"
    a, b = SimulatedAdapter(), SimulatedAdapter()
    a.form.fields.pop()
    assert len(b.form.fields) == 3
