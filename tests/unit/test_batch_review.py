"""Batch review page: answers, documents, evidence, change detection and rebuild."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.repositories import batch_items
from jobpilot.data.tables import ApplicationRow, DocumentArtifactRow, FormFieldAnswerRow
from jobpilot.ids import new_id
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import edit_answer, seed_ready_apps


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "review.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> TestClient:
    cfg = AppConfig(storage_path=tmp_path / "storage", model_routes={})
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, start_worker=False),
                      base_url="http://127.0.0.1")


def _batch(factory: sessionmaker[Session], n: int = 2) -> tuple[list[str], str, str]:
    with factory() as s:
        ids = seed_ready_apps(s, n)
        batch = ApprovalService(s).build_batch(ids)
        out = (ids, batch.batch_id, batch.content_hash or "")
        s.commit()
    return out


def _approve(client: TestClient, batch_id: str, h: str, version: int, ids: list[str]) -> int:
    return client.post(f"/ui/batches/{batch_id}/approve",
                       data={"content_hash": h, "version": version, "ids": ids}).status_code


def test_batch_page_shows_answers_and_docs(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        ids = seed_ready_apps(s, 1)
        s.commit()
    with factory() as s:
        app = s.get(ApplicationRow, ids[0])
        assert app is not None
        cover = new_id("art")
        shot = new_id("art")
        s.add_all([
            DocumentArtifactRow(artifact_id=cover, application_id=ids[0], type="cover_letter",
                                path="x.pdf", content_hash="sha256:c"),
            DocumentArtifactRow(artifact_id=shot, application_id=ids[0],
                                type="form_screenshot", path="x.png", content_hash="sha256:s"),
            FormFieldAnswerRow(application_id=ids[0], field_id="why", label="Why us",
                               field_type="text", proposed_value_json='"Because."',
                               decision="FILL_AND_REVIEW", confidence=0.4,
                               rationale="Model-suggested mapping")])
        app.cover_letter_artifact_id = cover
        resume = app.resume_artifact_id
        batch_id = ApprovalService(s).build_batch(ids).batch_id
        s.commit()
    page = client.get(f"/ui/batches/{batch_id}").text
    assert f"/artifacts/{resume}" in page and f"/artifacts/{cover}" in page
    assert f"/artifacts/{shot}" in page
    assert "AUTO_FILL 1" in page and "FILL_AND_REVIEW 1" in page
    assert "Notice period" in page and "30 days" in page and "Because." in page
    assert "1 to review" in page and "1 model-suggested" in page
    assert "Changed since batch was built" not in page
    assert re.search(r'title="sha256:[0-9a-f]+"', page)


def test_edit_after_approval_requires_reapproval(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    ids, batch_id, h = _batch(factory, 2)
    assert _approve(client, batch_id, h, 1, [ids[0]]) == 200
    with factory() as s:
        edit_answer(s, ids[0], "notice_period", "60 days")
        s.commit()
        assert s.get(ApplicationRow, ids[0]).status == "READY_FOR_REVIEW"  # type: ignore[union-attr]
    page = client.get(f"/ui/batches/{batch_id}").text
    assert page.count("Changed since batch was built") == 1
    assert f'value="{ids[0]}" disabled' in page
    assert _approve(client, batch_id, h, 1, [ids[0]]) == 409


def test_rebuild_after_change(client: TestClient, factory: sessionmaker[Session]) -> None:
    ids, batch_id, h = _batch(factory, 2)
    with factory() as s:
        edit_answer(s, ids[0], "notice_period", "60 days")
        s.commit()
    r = client.post(f"/ui/batches/{batch_id}/rebuild", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/ui/batches/{batch_id}"
    page = client.get(f"/ui/batches/{batch_id}").text
    assert "version 2" in page and "Changed since batch was built" not in page
    assert _approve(client, batch_id, h, 1, ids) == 409
    h2 = re.search(r'name="content_hash" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    assert _approve(client, batch_id, h2, 2, ids) == 200


def test_rebuild_excludes_non_ready(client: TestClient,
                                    factory: sessionmaker[Session]) -> None:
    ids, batch_id, _ = _batch(factory, 2)
    with factory() as s:
        app = s.get(ApplicationRow, ids[1])
        assert app is not None
        app.status = "NEEDS_USER_INPUT"
        s.commit()
    assert "Changed since batch was built" in client.get(f"/ui/batches/{batch_id}").text
    r = client.post(f"/ui/batches/{batch_id}/rebuild", follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/ui/batches/{batch_id}").text
    assert "version 2" in page and ids[0] in page and "Changed since" not in page
    with factory() as s:
        assert [i.application_id for i in batch_items(s, batch_id, 2)] == [ids[0]]
        for app in s.execute(select(ApplicationRow)).scalars():
            app.status = "NEEDS_USER_INPUT"
        s.commit()
    assert client.post(f"/ui/batches/{batch_id}/rebuild").status_code == 422


def test_batches_list_page(client: TestClient, factory: sessionmaker[Session]) -> None:
    assert "No batches are waiting" in client.get("/ui/batches").text
    ids, batch_id, h = _batch(factory, 2)
    assert _approve(client, batch_id, h, 1, [ids[0]]) == 200
    page = client.get("/ui/batches").text
    assert f"/ui/batches/{batch_id}" in page and "PARTIALLY_APPROVED" in page
    assert re.search(r"<td>2</td><td>1</td>", page)
    assert 'href="/ui/batches"' in page


def test_checkbox_disabled_unless_ready_for_review(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    ids, batch_id, _ = _batch(factory, 2)
    with factory() as s:
        app = s.get(ApplicationRow, ids[0])
        assert app is not None
        app.status = "APPROVED"  # hash unchanged, but no longer approvable here
        s.commit()
    page = client.get(f"/ui/batches/{batch_id}").text
    assert f'value="{ids[0]}" disabled' in page
    assert f'value="{ids[1]}" checked' in page


def test_rebuild_button_only_when_useful(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    ids, batch_id, h = _batch(factory, 1)
    assert "Rebuild batch" in client.get(f"/ui/batches/{batch_id}").text  # pending
    assert _approve(client, batch_id, h, 1, ids) == 200
    page = client.get(f"/ui/batches/{batch_id}").text  # fully approved, nothing changed
    assert "Rebuild batch" not in page and "Approve selected" not in page
    with factory() as s:
        edit_answer(s, ids[0], "notice_period", "60 days")
        s.commit()
    assert "Rebuild batch" in client.get(f"/ui/batches/{batch_id}").text  # a row changed


def test_untrusted_answer_text_is_escaped(
        client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        ids = seed_ready_apps(s, 1)
        s.add(FormFieldAnswerRow(
            application_id=ids[0], field_id="x", label="<script>alert(1)</script>",
            field_type="text", proposed_value_json='"<script>alert(2)</script>"',
            decision="FILL_AND_REVIEW", confidence=0.3, rationale="From library."))
        batch_id = ApprovalService(s).build_batch(ids).batch_id
        s.commit()
    page = client.get(f"/ui/batches/{batch_id}").text
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(1)" in page and "&lt;script&gt;alert(2)" in page
    assert "model-suggested" not in page  # low-confidence alone is not "suggested"
