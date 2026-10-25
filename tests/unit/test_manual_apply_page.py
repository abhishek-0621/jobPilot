"""Manual apply page: documents, drafted answers, Mark as applied, Not applying."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import (
    ApplicationRow,
    AuditEventRow,
    DocumentArtifactRow,
    FormFieldAnswerRow,
)
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.util.hashing import canonical_json
from tests.factories import make_app_row

XSS = '<script>alert("x")</script> & "q"'


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    upgrade_to_head(tmp_path / "m.db")
    engine = make_engine(tmp_path / "m.db")
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "storage", model_routes={})


@pytest.fixture
def client(factory: sessionmaker[Session], cfg: AppConfig) -> TestClient:
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1",
                      headers={"origin": f"http://127.0.0.1:{cfg.port}"})


def _seed(factory: sessionmaker[Session], cfg: AppConfig,
          status: str = "MANUAL_APPLY_READY") -> str:
    with factory() as s:
        app = make_app_row(s, status=status)
        app_id = app.application_id
        base = cfg.storage_path / "artifacts" / app_id
        base.mkdir(parents=True)
        for type_, name, data in (
                ("resume", "resume_v1.pdf", b"%PDF-1"), ("resume_docx", "resume_v1.docx", b"PK"),
                ("cover_letter", "cover_letter_v1.pdf", b"%PDF-1"),
                ("cover_letter_text", "cover_letter_v1.txt", f"Dear team,\n\n{XSS}".encode())):
            (base / name).write_bytes(data)
            s.add(DocumentArtifactRow(
                artifact_id=f"art_{type_}_{app_id}", application_id=app_id, type=type_,
                path=f"artifacts/{app_id}/{name}", content_hash="sha256:x"))
        s.flush()
        app.resume_artifact_id = f"art_resume_{app_id}"
        for fid, label, value, decision in (
                ("manual:why_company", "Why do you want to work at Acme?", XSS,
                 "FILL_AND_REVIEW"),
                ("manual:expected_ctc", "What is your expected CTC?", "", "ASK_USER"),
                ("manual:email", "Email", "a@example.com", "AUTO_FILL")):
            s.add(FormFieldAnswerRow(
                application_id=app_id, field_id=fid, label=label, field_type="text",
                proposed_value_json=canonical_json(value), decision=decision, confidence=0.9,
                rationale="r", required=True))
        s.commit()
        return app_id


def _row(factory: sessionmaker[Session], app_id: str) -> ApplicationRow:
    with factory() as s:
        app = s.get(ApplicationRow, app_id)
        assert app is not None
        s.expunge(app)
        return app


def test_apply_page_shows_documents_and_answers(
        client: TestClient, factory: sessionmaker[Session], cfg: AppConfig) -> None:
    app_id = _seed(factory, cfg)
    r = client.get(f"/ui/applications/{app_id}/apply")
    assert r.status_code == 200
    html = r.text
    assert f"/artifacts/art_resume_{app_id}" in html
    assert f"/artifacts/art_resume_docx_{app_id}" in html
    assert f"/artifacts/art_cover_letter_{app_id}" in html
    assert 'rel="noopener noreferrer"' in html and 'target="_blank"' in html
    assert "<script>alert" not in html  # values are escaped everywhere
    assert "&lt;script&gt;alert" in html
    assert "<textarea readonly" in html
    assert "Why do you want to work at Acme?" in html and "FILL_AND_REVIEW" in html
    assert 'name="v:manual:why_company"' in html and 'name="v:manual:expected_ctc"' in html
    assert 'name="v:manual:email"' not in html  # AUTO_FILL is shown, not editable
    assert "a@example.com" in html
    assert "Mark as applied" in html and "Not applying" in html
    assert "navigator.clipboard" in html
    assert "http" not in "".join(
        p.split('"')[0] for p in html.split('<script src="')[1:] if "/static/" not in p)
    assert f"/ui/applications/{app_id}/apply" in client.get(f"/ui/applications/{app_id}").text
    assert client.get("/ui/applications/app_nope/apply").status_code == 404


def test_apply_page_status_gate_and_readonly_after(
        client: TestClient, factory: sessionmaker[Session], cfg: AppConfig) -> None:
    other = _seed(factory, cfg, "READY_FOR_REVIEW")
    assert client.get(f"/ui/applications/{other}/apply").status_code == 409
    app_id = _seed(factory, cfg)
    r = client.post(f"/ui/applications/{app_id}/mark-applied",
                    data={"reference": "R1", "applied_on": ""}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/ui/applications/{app_id}/apply")
    assert page.status_code == 200
    assert "Mark as applied" not in page.text and 'name="v:' not in page.text
    assert "Applied (manual)" in page.text and "R1" in page.text
    assert client.post(f"/ui/applications/{app_id}/mark-applied",
                       data={"reference": ""}).status_code == 409


def test_mark_applied_form_validation(
        client: TestClient, factory: sessionmaker[Session], cfg: AppConfig) -> None:
    app_id = _seed(factory, cfg)
    url = f"/ui/applications/{app_id}/mark-applied"
    assert client.post(url, data={"reference": "x" * 101}).status_code == 422
    assert client.post(url, data={"applied_on": "2999-01-01"}).status_code == 422
    assert client.post(url, data={"applied_on": "not-a-date"}).status_code == 422
    assert client.post(url, data={}, headers={"origin": "https://evil.example"}
                       ).status_code == 403
    assert _row(factory, app_id).status == "MANUAL_APPLY_READY"
    assert client.post(url, data={"reference": " ok ", "applied_on": "2026-01-02"},
                       follow_redirects=False).status_code == 303
    assert _row(factory, app_id).confirmation_reference == "ok"


def test_save_answers_on_apply_page(
        client: TestClient, factory: sessionmaker[Session], cfg: AppConfig) -> None:
    app_id = _seed(factory, cfg)
    url = f"/ui/applications/{app_id}/apply/answers"
    r = client.post(url, data={"v:manual:expected_ctc": "20 LPA",
                               "v:manual:email": "evil@example.com"}, follow_redirects=False)
    assert r.status_code == 409  # AUTO_FILL rows are not editable
    r = client.post(url, data={"v:manual:expected_ctc": "20 LPA"}, follow_redirects=False)
    assert r.status_code == 303
    with factory() as s:
        row = s.get(FormFieldAnswerRow, (app_id, "manual:expected_ctc"))
        assert row is not None
        assert json.loads(row.proposed_value_json) == "20 LPA"
        assert row.source_fact_id == "user" and row.decision == "ASK_USER"
        assert s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "answer_changed")).scalars().first() is not None
    assert _row(factory, app_id).status == "MANUAL_APPLY_READY"  # no state change
    assert "20 LPA" in client.get(f"/ui/applications/{app_id}/apply").text
    client.post(f"/ui/applications/{app_id}/mark-applied", data={})
    assert client.post(url, data={"v:manual:expected_ctc": "x"}).status_code == 409


def test_not_applying(client: TestClient, factory: sessionmaker[Session],
                      cfg: AppConfig) -> None:
    app_id = _seed(factory, cfg)
    r = client.post(f"/ui/applications/{app_id}/not-applying", follow_redirects=False)
    assert r.status_code == 303
    assert _row(factory, app_id).status == "REJECTED_BY_USER"
    assert client.post(f"/ui/applications/{app_id}/not-applying").status_code == 409
    other = _seed(factory, cfg, "READY_FOR_REVIEW")
    assert client.post(f"/ui/applications/{other}/not-applying").status_code == 409


def test_overview_counts_manual_applied(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        for st in ("CONFIRMED", "CONFIRMED", "SUBMITTED_BY_USER", "READY_FOR_REVIEW"):
            make_app_row(s, status=st)
        s.commit()
    html = client.get("/").text
    assert "Applied" in html and "3" in html
    assert "automated 2, manual 1" in html
    counts = client.get("/ui/overview/counts").text
    assert "automated 2, manual 1" in counts


def test_jobs_page_labels_manual_applied(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        make_app_row(s, status="SUBMITTED_BY_USER")
        s.commit()
    assert "Applied (manual)" in client.get("/ui/jobs").text
