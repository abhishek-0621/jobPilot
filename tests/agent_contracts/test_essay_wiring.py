"""Essay answers wired into PREPARE_FORM, MANUAL_PACKAGE, approval and the library."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from jobpilot.agents.essay import EssayContext, guard_essay_answers, library_key
from jobpilot.agents.manual_package import ESSAY_PREFIX, ESSAY_QUESTIONS, essay_answers
from jobpilot.contracts.enums import FieldDecision
from jobpilot.contracts.models import FieldAnswer
from jobpilot.data import mutations
from jobpilot.data.tables import FormFieldAnswerRow
from jobpilot.orchestrator.approval import ApprovalService
from jobpilot.orchestrator.engine import OutputRejected, TaskKind
from jobpilot.portals.base import FormField, FormSnapshot
from jobpilot.profile.stories import (
    QuestionKey,
    StoryFile,
    import_stories,
    stored_bank,
    verify_story,
)
from tests.agent_contracts.test_essay import EXAMPLE, PROUD_OK, SeqGateway, ok
from tests.agent_contracts.test_form_prep import Env, FakePortal, make_env, run, snapshot

PROUD = "What are you most proud of?"
CUSTOMER = "Tell us about a time you helped a customer"


def stories(**changes: str) -> StoryFile:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for k, v in changes.items():
        data["stories"][0][k] = v
    return StoryFile.model_validate(data)


def essay_env(tmp_path: Path, gw: SeqGateway | None = None) -> Env:
    snap = FormSnapshot(url="x", fields=[
        *snapshot().fields[:3],
        FormField(field_id="q_proud", label=PROUD, field_type="textarea", required=True),
        FormField(field_id="q_customer", label=CUSTOMER, field_type="textarea",
                  required=False),
        FormField(field_id="q_salary", label="Expected salary? Why are you proud of it?",
                  field_type="textarea", required=False),
    ])
    env = make_env(tmp_path, FakePortal(snap))
    env.orch._gateway = gw or SeqGateway(ok())  # type: ignore[assignment]
    with env.factory() as s:
        import_stories(s, stories())
        verify_story(s, "latency-cut", True)
        s.commit()
    return env


async def test_essay_answers_require_review(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        rows = env.rows(s)
        assert rows["q_proud"].decision == "FILL_AND_REVIEW"
        assert json.loads(rows["q_proud"].proposed_value_json) == PROUD_OK
        assert rows["q_customer"].decision == "ASK_USER"
        assert "needs_story:customer" in rows["q_customer"].rationale
        assert json.loads(rows["q_customer"].proposed_value_json) is None
        assert rows["q_salary"].decision == "ASK_USER"  # compensation, never an essay
        assert "Essay" not in rows["q_salary"].rationale
        assert not any(r.decision == "AUTO_FILL" and (r.source_fact_id or "")
                       .startswith("essay:") for r in rows.values())
        # the single writer refuses an essay answer that skips review
        bad = FieldAnswer(field_id="q_proud", label=PROUD, field_type="textarea",
                          proposed_value="x", decision=FieldDecision.AUTO_FILL,
                          source_fact_id="essay:latency-cut", confidence=1.0, rationale="x")
        with pytest.raises(OutputRejected):
            guard_essay_answers(s, [bad])
        with pytest.raises(ValueError):
            mutations.set_answer(s, env.app_id, "q_proud", "y", actor="test",
                                 decision=FieldDecision.AUTO_FILL)
    gw = env.orch._gateway
    assert isinstance(gw, SeqGateway) and len(gw.calls) == 1  # only the proud_of draft
    assert all(c["route"] == "essay" for c in gw.calls)


async def test_essay_source_recorded(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        row = env.rows(s)["q_proud"]
        assert row.source_fact_id == "essay:latency-cut"
        assert row.rationale.startswith("Essay (proud_of) drafted from 1 verified story")
        assert mutations.essay_story_ids(row.source_fact_id) == ["latency-cut"]


def _ready_with_essay(s: Session, app_id: str) -> None:
    for r in s.query(FormFieldAnswerRow).filter(
            FormFieldAnswerRow.application_id == app_id):
        if r.decision in ("ASK_USER", "USER_ONLY") and r.field_id != "q_proud":
            s.delete(r)
    s.flush()
    from jobpilot.data.repositories import get_application
    get_application(s, app_id).status = "READY_FOR_REVIEW"
    s.flush()


async def test_story_change_invalidates_answers(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        _ready_with_essay(s, env.app_id)
        svc = ApprovalService(s)
        b = svc.build_batch([env.app_id])
        svc.approve(b.batch_id, b.version, b.content_hash, [env.app_id])
        assert env.app(s).status == "APPROVED"
        out = import_stories(s, stories(result="Median response time fell a lot."))
        assert out["answers_invalidated"] == 1
        row = env.rows(s)["q_proud"]
        assert row.decision == "ASK_USER" and row.rationale.startswith("story_changed")
        assert json.loads(row.proposed_value_json) == PROUD_OK  # kept for reference
        assert svc.active_approval_for(env.app_id) is None
        assert env.app(s).status == "NEEDS_USER_INPUT"
        assert not next(st for st in stored_bank(s).stories
                        if st.story.story_id == "latency-cut").verified
        s.commit()


async def test_unverify_invalidates_but_user_answer_kept(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        verify_story(s, "latency-cut", False)
        assert env.rows(s)["q_proud"].decision == "ASK_USER"
        verify_story(s, "latency-cut", True)
        mutations.set_answer(s, env.app_id, "q_proud", "My own words.", actor="user",
                             source_fact_id="user")
        verify_story(s, "latency-cut", False)
        row = env.rows(s)["q_proud"]
        assert json.loads(row.proposed_value_json) == "My own words."
        assert row.source_fact_id == "user"


def test_manual_package_includes_essays(tmp_path: Path) -> None:
    keys = [k for k, _ in ESSAY_QUESTIONS]
    assert keys == ["proud_of", "failure", "challenge", "strengths", "weaknesses", "why_ai"]
    from tests.agent_contracts.test_essay import bank
    ectx = EssayContext(bank=bank(verified={"latency-cut"}), facts={}, skills=[],
                        job={"company": "Example AI", "title": "GenAI Engineer"})
    out = {a.field_id: a for a in asyncio.run(essay_answers(SeqGateway(ok()), ectx))}
    assert set(out) == {ESSAY_PREFIX + k for k in keys}
    assert out[ESSAY_PREFIX + "proud_of"].decision == FieldDecision.FILL_AND_REVIEW
    assert out[ESSAY_PREFIX + "weaknesses"].decision == FieldDecision.FILL_AND_REVIEW
    assert out[ESSAY_PREFIX + "weaknesses"].rationale.startswith("Essay (weaknesses): your")
    assert out[ESSAY_PREFIX + "failure"].decision == FieldDecision.ASK_USER  # unverified
    assert "needs_story:failure" in out[ESSAY_PREFIX + "failure"].rationale
    assert all(a.decision != FieldDecision.AUTO_FILL for a in out.values())


async def test_manual_pipeline_stores_essays(tmp_path: Path) -> None:
    from jobpilot.data.tables import JobRow
    env = essay_env(tmp_path)
    with env.factory() as s:
        app = env.app(s)
        job = s.get(JobRow, app.job_id)
        assert job is not None
        job.application_url = "https://www.linkedin.com/jobs/view/1"
        s.commit()
    await run(env, TaskKind.MANUAL_PACKAGE)
    with env.factory() as s:
        assert env.app(s).status == "MANUAL_APPLY_READY"
        rows = env.rows(s)
        assert rows[ESSAY_PREFIX + "proud_of"].source_fact_id == "essay:latency-cut"
        assert rows[ESSAY_PREFIX + "proud_of"].decision == "FILL_AND_REVIEW"


async def test_save_essay_to_library_reused(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    await run(env, TaskKind.PREPARE_FORM)
    with env.factory() as s:
        key = mutations.save_essay_to_library(s, env.app_id, "q_proud", actor="user")
        assert key == library_key(QuestionKey.PROUD_OF) == "essay_proud_of"
        with pytest.raises(ValueError):  # not an essay question
            mutations.save_essay_to_library(s, env.app_id, "first_name", actor="user")
        with pytest.raises(ValueError):  # nothing to save
            mutations.save_essay_to_library(s, env.app_id, "q_customer", actor="user")
        s.commit()
    ectx = env.tools["essay_context"](env.app_id)
    assert ectx.library == {"essay_proud_of": PROUD_OK}
    assert [s.story_id for s in ectx.bank.verified()] == ["latency-cut"]
    assert all(st.verified for st in ectx.bank.stories)  # unverified never loaded
