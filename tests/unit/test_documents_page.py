"""Application documents page, artifact serving, staleness on re-import, regenerate."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.actions import ActionConflict, regenerate_documents
from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import (
    ApplicationRow,
    AuditEventRow,
    DocumentArtifactRow,
    TaskRow,
)
from jobpilot.ids import new_id
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.profile.importer import import_profile
from jobpilot.profile.schema import load_profile_file
from jobpilot.util.hashing import canonical_json
from tests.factories import make_app_row, seed_ready_apps
from tests.unit.test_profile import FIXTURE, edited


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "docs.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "storage", model_routes={})


def _orch(factory: sessionmaker[Session], cfg: AppConfig) -> Orchestrator:
    return Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})


def _client(factory: sessionmaker[Session], cfg: AppConfig) -> TestClient:
    return TestClient(create_app(cfg, factory, _orch(factory, cfg), start_worker=False),
                      base_url="http://127.0.0.1")


def _artifact(s: Session, app_id: str, type_: str, rel: str, facts: list[str],
              version: int = 1) -> str:
    art_id = new_id("art")
    s.add(DocumentArtifactRow(artifact_id=art_id, application_id=app_id, type=type_,
                              path=rel, content_hash="sha256:x", version=version,
                              source_fact_ids_json=canonical_json(facts)))
    s.flush()
    return art_id


def _approved_app(s: Session) -> str:
    (app_id,) = seed_ready_apps(s, 1)
    svc = ApprovalService(s)
    batch = svc.build_batch([app_id])
    svc.approve(batch.batch_id, 1, batch.content_hash or "", [app_id])
    return app_id


def _tasks(s: Session, app_id: str) -> list[tuple[str, str]]:
    return [(t.kind, t.status) for t in s.execute(
        select(TaskRow).where(TaskRow.entity_id == app_id)).scalars()]


def test_reimport_invalidates_artifact_citing_changed_fact(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        import_profile(s, load_profile_file(FIXTURE))
        cited = _approved_app(s)
        (other,) = seed_ready_apps(s, 1)
        for app_id, fact in ((cited, "fact_exp_002"), (other, "fact_exp_001")):
            app = s.get(ApplicationRow, app_id)
            assert app is not None
            app.resume_artifact_id = _artifact(s, app_id, "resume",
                                               f"artifacts/{app_id}/resume_v2.pdf", [fact])
        # a superseded artifact citing the changed fact does not make `other` stale
        _artifact(s, other, "resume", "artifacts/old/resume_v1.pdf", ["fact_exp_002"])
        s.commit()
        import_profile(s, edited(fact_exp_002="Reduced latency by 40 percent"))
        s.commit()
        status = {a.application_id: a.status for a in s.execute(
            select(ApplicationRow)).scalars()}
        assert status[cited] == "DOCUMENTS_PENDING"
        assert status[other] == "READY_FOR_REVIEW"
        assert ApprovalService(s).active_approval_for(cited) is None
        assert _tasks(s, cited) == [("TAILOR", "QUEUED")]
        assert _tasks(s, other) == []
        ev = s.execute(select(AuditEventRow).where(
            AuditEventRow.action == "documents_stale")).scalar_one()
        assert ev.entity_id == cited
        details = json.loads(ev.redacted_details_json)
        assert details["fact_ids"] == ["fact_exp_002"]
        assert "40 percent" not in ev.redacted_details_json


def test_regenerate_revokes_approval(factory: sessionmaker[Session], tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    orch = _orch(factory, cfg)
    with factory() as s:
        app_id = _approved_app(s)
        s.commit()
        assert ApprovalService(s).active_approval_for(app_id) is not None
        regenerate_documents(s, orch, app_id)
        app = s.get(ApplicationRow, app_id)
        assert app is not None and app.status == "DOCUMENTS_PENDING"
        assert ApprovalService(s).active_approval_for(app_id) is None
        assert _tasks(s, app_id) == [("TAILOR", "QUEUED")]
        with pytest.raises(ActionConflict):  # TAILOR already queued
            regenerate_documents(s, orch, app_id)
        s.rollback()
        done = make_app_row(s, status="SUBMITTED")
        s.commit()
        with pytest.raises(ActionConflict):
            regenerate_documents(s, orch, done.application_id)
        s.rollback()
        with pytest.raises(LookupError):
            regenerate_documents(s, orch, "nope")
    client = _client(factory, cfg)
    with factory() as s:
        fip = make_app_row(s, status="FORM_IN_PROGRESS").application_id
        s.commit()
    r = client.post(f"/ui/applications/{fip}/regenerate")
    assert r.status_code == 200 and "Regeneration queued" in r.text
    assert client.post(f"/ui/applications/{fip}/regenerate").status_code == 409
    assert client.post("/ui/applications/nope/regenerate").status_code == 404
    with factory() as s:
        app = s.get(ApplicationRow, fip)
        assert app is not None and app.status == "DOCUMENTS_PENDING"


def test_artifact_route_path_containment(
    factory: sessionmaker[Session], tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    good = cfg.storage_path / "artifacts" / "app_1" / "resume_v1.pdf"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"%PDF-1.4 synthetic")
    outside = cfg.storage_path / "secret.txt"
    outside.write_text("nope")
    with factory() as s:
        app_id = make_app_row(s).application_id
        ok = _artifact(s, app_id, "resume", "artifacts/app_1/resume_v1.pdf", [])
        bad = _artifact(s, app_id, "resume", "../../etc/passwd", [])
        sneaky = _artifact(s, app_id, "resume", "artifacts/../secret.txt", [])
        missing = _artifact(s, app_id, "resume", "artifacts/app_1/none.pdf", [])
        s.commit()
    client = _client(factory, cfg)
    r = client.get(f"/artifacts/{ok}")
    assert r.status_code == 200 and r.content == b"%PDF-1.4 synthetic"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert "resume_v1.pdf" in r.headers["content-disposition"]
    for art in (bad, sneaky, missing, "art_unknown"):
        assert client.get(f"/artifacts/{art}").status_code == 404


def test_application_page_renders_diff_and_previews(
    factory: sessionmaker[Session], tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    d = cfg.storage_path / "artifacts" / "app_x"
    d.mkdir(parents=True)
    (d / "resume_v1_diff.json").write_text(canonical_json([
        {"fact_id": "fact_exp_001", "section": "experience", "original": "Built a pipeline",
         "tailored": "Built a retrieval pipeline", "status": "rewritten"}]))
    (d / "cover_letter_v1.txt").write_text("Dear Hiring Manager,\n\nSynthetic letter body.")
    with factory() as s:
        import_profile(s, load_profile_file(FIXTURE))
        app = make_app_row(s, status="FORM_IN_PROGRESS")
        app_id = app.application_id
        resume = _artifact(s, app_id, "resume", "artifacts/app_x/resume_v1.pdf",
                           ["fact_exp_001"])
        docx = _artifact(s, app_id, "resume_docx", "artifacts/app_x/resume_v1.docx", [])
        _artifact(s, app_id, "resume_diff", "artifacts/app_x/resume_v1_diff.json", [])
        _artifact(s, app_id, "cover_letter_text", "artifacts/app_x/cover_letter_v1.txt",
                  ["fact_proj_001"])
        job_id = app.job_id
        s.commit()
    client = _client(factory, cfg)
    html = client.get(f"/ui/applications/{app_id}").text
    assert f'<iframe src="/artifacts/{resume}"' in html
    assert 'referrerpolicy="no-referrer"' in html
    assert f'href="/artifacts/{docx}"' in html
    assert "Built a retrieval pipeline" in html and "rewritten" in html
    assert "fact_exp_001" in html
    assert "Synthetic letter body." in html
    assert "Built a document question answering assistant" in html  # evidence fact value
    assert f'hx-post="/ui/applications/{app_id}/regenerate"' in html
    assert 'href="https://example.com/jobs/' in html
    assert client.get("/ui/applications/nope").status_code == 404
    assert f'href="/ui/applications/{app_id}"' in client.get(f"/ui/jobs/{job_id}").text
    assert f'href="/ui/applications/{app_id}"' in client.get("/ui/jobs").text
