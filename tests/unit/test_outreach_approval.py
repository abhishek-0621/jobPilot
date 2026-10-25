"""Plan 8 Task 4: outreach approval (own scope, hash-bound) and the user's Mark as sent."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.api.actions import (
    ActionConflict,
    add_outreach_contact,
    approve_outreach,
    choose_outreach_contact,
    edit_outreach_message,
    mark_outreach_sent,
    skip_outreach,
)
from jobpilot.contracts.enums import OutreachStatus
from jobpilot.data import mutations
from jobpilot.data.tables import AuditEventRow, ContactCandidateRow, OutreachDraftRow
from jobpilot.orchestrator.approval import ApprovalService, StaleApprovalError
from jobpilot.orchestrator.state_machine import IllegalTransition, transition
from tests.factories import seed_ready_apps
from tests.unit.test_outreach_flow import _drafted, cfg, factory  # noqa: F401


def _hash(s: Session, oid: str) -> str:
    return ApprovalService(s).outreach_hash(oid)


async def test_mark_sent_requires_current_approval(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        with pytest.raises(ActionConflict):  # not approved
            mark_outreach_sent(s, oid, None)
        h = _hash(s, oid)
        approve_outreach(s, oid, h)
        # content changed behind the approval's back (no mutation): the hash no longer
        # matches, so marking sent is refused
        d = s.get(OutreachDraftRow, oid)
        assert d is not None
        d.message_text = d.message_text + " Extra."
        s.commit()
        with pytest.raises(ActionConflict):
            mark_outreach_sent(s, oid, None)
        d.message_text = d.message_text.removesuffix(" Extra.")
        s.commit()
        mark_outreach_sent(s, oid, "Sent from my mail app")
        d = s.get(OutreachDraftRow, oid)
        assert d is not None and d.status == "SENT" and d.sent_at is not None
        assert d.sent_note == "Sent from my mail app" and d.evidence_ref == "user_marked"
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "user_marked_outreach_sent")).scalars().one()
        assert ev.actor == "user" and "mail app" not in ev.redacted_details_json


async def test_mark_sent_twice_409(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        approve_outreach(s, oid, _hash(s, oid))
        mark_outreach_sent(s, oid, None)
        with pytest.raises(ActionConflict):
            mark_outreach_sent(s, oid, None)
        with pytest.raises(ActionConflict):
            edit_outreach_message(s, oid, "new text")


async def test_edit_invalidates_outreach_approval(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        old = _hash(s, oid)
        approve_outreach(s, oid, old)
        edit_outreach_message(s, oid, "Hello, I applied for the role. Thanks!")
        d = s.get(OutreachDraftRow, oid)
        assert d is not None and d.status == "NEEDS_REVIEW"
        assert ApprovalService(s).active_outreach_approval_for(oid) is None
        with pytest.raises(StaleApprovalError):
            ApprovalService(s).approve_outreach(oid, old)
        with pytest.raises(ActionConflict):
            mark_outreach_sent(s, oid, None)
        for bad in ("", "   ", "x" * 3001):
            with pytest.raises(ValueError):
                edit_outreach_message(s, oid, bad)
        # switching to an alternate / user-added contact also invalidates
        approve_outreach(s, oid, _hash(s, oid))
        cid = add_outreach_contact(s, oid, name="Pat Example", title="Recruiter",
                                   channel="linkedin",
                                   address="https://www.linkedin.com/in/pat-example",
                                   source_url="https://www.linkedin.com/in/pat-example")
        d = s.get(OutreachDraftRow, oid)
        assert d is not None and d.contact_id == cid and d.channel == "linkedin"
        assert d.status == "NEEDS_REVIEW"
        c = s.get(ContactCandidateRow, cid)
        assert c is not None and c.kind == "user" and c.confidence == 1.0 and c.verified_at
        first = s.execute(select(ContactCandidateRow).where(
            ContactCandidateRow.contact_id != cid)).scalars().first()
        assert first is not None
        approve_outreach(s, oid, _hash(s, oid))
        choose_outreach_contact(s, oid, first.contact_id)
        assert ApprovalService(s).active_outreach_approval_for(oid) is None


async def test_add_contact_validation(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        bad = [
            ("email", "pat@other-company.example"),        # off the company domain
            ("email", "not-an-email"),
            ("linkedin", "https://evil.example/in/pat"),    # not a LinkedIn profile URL
            ("linkedin", "javascript:alert(1)"),
            ("fax", "123"),
        ]
        for channel, address in bad:
            with pytest.raises(ValueError):
                add_outreach_contact(s, oid, name="Pat", title=None, channel=channel,
                                     address=address,
                                     source_url="https://acmerobotics.example/team")
        with pytest.raises(ValueError):
            add_outreach_contact(s, oid, name="Pat", title=None, channel="email",
                                 address="pat@acmerobotics.example", source_url="ftp://x")
        add_outreach_contact(s, oid, name="Pat", title=None, channel="email",
                             address="pat@talent.acmerobotics.example",
                             source_url="https://acmerobotics.example/team")
        # another application's contact cannot be chosen
        other = ContactCandidateRow(contact_id="con_other", company="x", name="x",
                                    public_source_url="https://x.example", confidence=1.0)
        s.add(other)
        s.commit()
        with pytest.raises(ActionConflict):
            choose_outreach_contact(s, oid, "con_other")


async def test_user_contact_revives_no_contact_skip(factory, cfg) -> None:  # noqa: F811
    from jobpilot.api.actions import request_outreach
    from tests.unit.test_outreach_flow import _orch, seed_app

    with factory() as s:
        app_id = seed_app(s, description="No contact here.")
        request_outreach(s, _orch(factory, cfg), app_id, cfg)
    await _orch(factory, cfg).run_until_idle()
    with factory() as s:
        d = s.execute(select(OutreachDraftRow)).scalars().one()
        assert d.status == "SKIPPED"
        add_outreach_contact(s, d.outreach_id, name="Pat", title="Recruiter",
                             channel="linkedin", address="https://linkedin.com/in/pat",
                             source_url="https://linkedin.com/in/pat")
        d = s.get(OutreachDraftRow, d.outreach_id)
        assert d is not None and d.status == "NEEDS_REVIEW" and d.skip_reason is None


async def test_skip_outreach(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        approve_outreach(s, oid, _hash(s, oid))
        skip_outreach(s, oid)
        d = s.get(OutreachDraftRow, oid)
        assert d is not None and d.status == "SKIPPED" and d.skip_reason == "user_skipped"
        assert ApprovalService(s).active_outreach_approval_for(oid) is None
        with pytest.raises(ActionConflict):
            skip_outreach(s, oid)
        with pytest.raises(ActionConflict):  # a user skip is final
            add_outreach_contact(s, oid, name="Pat", title=None, channel="linkedin",
                                 address="https://linkedin.com/in/pat",
                                 source_url="https://linkedin.com/in/pat")


async def test_application_approval_does_not_approve_outreach(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        ids = seed_ready_apps(s, 1)
        svc = ApprovalService(s)
        batch = svc.build_batch(ids)
        svc.approve(batch.batch_id, batch.version, batch.content_hash or "", ids)
        s.commit()
        assert svc.active_outreach_approval_for(oid) is None
        # an application approval id never satisfies the outreach check
        with pytest.raises(ActionConflict):
            mark_outreach_sent(s, oid, None)
        # and outreach approval/invalidation never touches application approvals
        approve_outreach(s, oid, _hash(s, oid))
        edit_outreach_message(s, oid, "Hello again, thanks for your time.")
        assert svc.active_approval_for(ids[0]) is not None


async def test_send_edges_user_only(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        approve_outreach(s, oid, _hash(s, oid))
        d = s.get(OutreachDraftRow, oid)
        with pytest.raises(IllegalTransition):
            transition(s, d, OutreachStatus.SEND_IN_PROGRESS, actor="orchestrator",
                       reason="x")


async def test_approve_needs_contact(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        d = s.get(OutreachDraftRow, oid)
        assert d is not None
        mutations.detach_contacts(s, [d.contact_id or ""], reason="test")
        s.commit()
        with pytest.raises(ActionConflict):
            approve_outreach(s, oid, _hash(s, oid))


def test_no_send_code_paths() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "jobpilot"
    files = [*(root / "outreach").rglob("*.py"), root / "agents" / "outreach.py"]
    banned = ("smtplib", "playwright", "sendgrid", "mailgun", "boto3", "email.mime",
              "webbrowser", "forms.browser", "portals")
    for f in files:
        text = f.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{f.name} references {word}"
