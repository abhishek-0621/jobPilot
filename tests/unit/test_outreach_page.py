"""Plan 8 Task 5: Outreach queue and detail pages."""

from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import select

from jobpilot.api.main import create_app
from jobpilot.data.tables import ContactCandidateRow, OutreachDraftRow
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.unit.test_outreach_flow import _drafted, cfg, factory, seed_app  # noqa: F401


def _client(factory, cfg, origin: bool = True) -> TestClient:  # noqa: F811
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    headers = {"origin": f"http://127.0.0.1:{cfg.port}"} if origin else {}
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1", headers=headers)


async def test_outreach_queue_renders(factory, cfg) -> None:  # noqa: F811
    app_id, oid = await _drafted(factory, cfg)
    c = _client(factory, cfg)
    r = c.get("/ui/outreach")
    assert r.status_code == 200
    assert 'href="/ui/outreach"' in r.text  # nav link
    assert "Acme Robotics" in r.text and "Backend Engineer" in r.text
    assert "NEEDS_REVIEW" in r.text and "careers@acmerobotics.example" in r.text
    assert "0.9" in r.text and "email" in r.text and f"/ui/outreach/{oid}" in r.text


async def test_detail_shows_draft_only_label(factory, cfg) -> None:  # noqa: F811
    app_id, oid = await _drafted(factory, cfg)
    c = _client(factory, cfg)
    r = c.get(f"/ui/outreach/{oid}")
    assert r.status_code == 200
    assert "Draft only — not sent" in r.text
    assert "JobPilot never sends" in r.text
    a = c.get(f"/ui/applications/{app_id}")
    assert "Recruiter contacted: draft only" in a.text
    # approve then mark sent through the forms
    with factory() as s:
        h = ApprovalService(s).outreach_hash(oid)
    assert c.post(f"/ui/outreach/{oid}/approve", data={"hash": "stale"},
                  follow_redirects=False).status_code == 409
    assert c.post(f"/ui/outreach/{oid}/approve", data={"hash": h},
                  follow_redirects=False).status_code == 303
    assert c.post(f"/ui/outreach/{oid}/mark-sent", data={"note": "via mail"},
                  follow_redirects=False).status_code == 303
    assert c.post(f"/ui/outreach/{oid}/mark-sent", data={},
                  follow_redirects=False).status_code == 409
    a = c.get(f"/ui/applications/{app_id}")
    assert "Recruiter contacted: yes (you marked sent on" in a.text


async def test_mailto_encoding(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    c = _client(factory, cfg)
    msg = 'Hello & "hi"?\nLine two #1'
    assert c.post(f"/ui/outreach/{oid}/message", data={"message": msg},
                  follow_redirects=False).status_code == 303
    r = c.get(f"/ui/outreach/{oid}")
    body = quote(msg, safe="")
    assert "mailto:careers@acmerobotics.example?subject=" in r.text
    assert f"body={body}" in r.text
    assert "\nLine two" not in r.text.split("mailto:", 1)[1].split('"', 1)[0]


async def test_unsafe_profile_url_not_linked(factory, cfg) -> None:  # noqa: F811
    _app, oid = await _drafted(factory, cfg)
    with factory() as s:
        d = s.get(OutreachDraftRow, oid)
        assert d is not None
        s.add(ContactCandidateRow(contact_id="con_bad", company="Acme Robotics", name="Bad",
                                  public_source_url="javascript:alert(2)", channel="linkedin",
                                  address="javascript:alert(1)", kind="user", confidence=1.0,
                                  application_id=d.application_id))
        s.flush()
        d.contact_id, d.channel = "con_bad", "linkedin"
        s.commit()
    r = _client(factory, cfg).get(f"/ui/outreach/{oid}")
    assert r.status_code == 200
    assert 'href="javascript:' not in r.text


async def test_forms_add_contact_skip_and_draft_button(factory, cfg) -> None:  # noqa: F811
    app_id, oid = await _drafted(factory, cfg)
    c = _client(factory, cfg)
    r = c.post(f"/ui/outreach/{oid}/contacts",
               data={"name": "Pat", "title": "Recruiter", "channel": "linkedin",
                     "address": "https://www.linkedin.com/in/pat",
                     "source_url": "https://www.linkedin.com/in/pat"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.get(f"/ui/outreach/{oid}")
    assert 'href="https://www.linkedin.com/in/pat"' in r.text and "Open profile" in r.text
    assert c.post(f"/ui/outreach/{oid}/contacts",
                  data={"name": "x", "channel": "email", "address": "x@evil.example",
                        "source_url": "https://evil.example"},
                  follow_redirects=False).status_code == 422
    with factory() as s:
        first = s.execute(select(ContactCandidateRow).where(
            ContactCandidateRow.kind != "user")).scalars().first()
        assert first is not None
    assert c.post(f"/ui/outreach/{oid}/contact", data={"contact_id": first.contact_id},
                  follow_redirects=False).status_code == 303
    assert c.post(f"/ui/outreach/{oid}/skip", follow_redirects=False).status_code == 303
    # Draft outreach button on another applied application
    with factory() as s:
        other = seed_app(s, company="Initech",
                         description="Mail jobs@initech.example", url="https://initech.example/j")
    a = c.get(f"/ui/applications/{other}")
    assert f'action="/ui/applications/{other}/outreach"' in a.text
    assert c.post(f"/ui/applications/{other}/outreach",
                  follow_redirects=False).status_code == 303
    assert c.post(f"/ui/applications/{other}/outreach",
                  follow_redirects=False).status_code == 409
    # cross-origin POSTs are rejected like every other form
    bad = TestClient(c.app, base_url="http://127.0.0.1",
                     headers={"origin": "http://evil.example"})
    assert bad.post(f"/ui/outreach/{oid}/skip").status_code == 403
    assert c.get("/ui/outreach/out_missing").status_code == 404
