import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from jobpilot import cli
from jobpilot.config import AppConfig
from jobpilot.contracts.enums import ApplicationStatus
from jobpilot.data.tables import (
    AnswerLibraryRow,
    ApplicationRow,
    AuditEventRow,
    FormFieldAnswerRow,
)
from jobpilot.profile.importer import (
    active_profile,
    candidate_years,
    import_profile,
    verified_facts,
    verify_fact,
)
from jobpilot.profile.preferences import load_companies, load_search_preferences
from jobpilot.profile.schema import ProfileFile, load_profile_file

FIXTURE = Path("tests/fixtures/profile_example.yaml")
# 1 education + 2 experience + 1 project + 1 achievement + 2 skill groups
FIXTURE_FACT_COUNT = 7


def edited(**changes: str) -> ProfileFile:
    data = yaml.safe_load(FIXTURE.read_text())
    for role in data["experience"]:
        for f in role["facts"]:
            if f["fact_id"] in changes:
                f["value"] = changes[f["fact_id"]]
    return ProfileFile.model_validate(data)


def test_import_creates_versioned_facts(db_session: Session) -> None:
    v = import_profile(db_session, load_profile_file(FIXTURE))
    version, facts = active_profile(db_session)
    assert v == 1 == version and len(facts) == FIXTURE_FACT_COUNT
    by_id = {f.fact_id: f for f in facts}
    assert by_id["fact_edu_001"].value == \
        "M.Tech Computer Science, Example Institute of Technology (2022)"
    assert by_id["fact_skill_genai"].value == "RAG, LLM evaluation"
    assert by_id["fact_exp_001"].category == "work_experience"
    assert all(f.allowed_for_resume for f in facts)
    rows = db_session.execute(select(AnswerLibraryRow)).scalars().all()
    keys = {r.question_key: r for r in rows}
    assert set(keys) == {"notice_period", "preferred_cities"}
    assert json.loads(keys["preferred_cities"].value_json) == ["Bengaluru", "Hyderabad"]
    assert keys["notice_period"].decision_default == "FILL_AND_REVIEW"


def test_reimport_keeps_verification_for_unchanged_facts(db_session: Session) -> None:
    import_profile(db_session, load_profile_file(FIXTURE))
    verify_fact(db_session, "fact_exp_001")
    verify_fact(db_session, "fact_exp_002")
    v2 = import_profile(db_session, edited(fact_exp_002="Reduced latency by 40 percent"))
    facts = {f.fact_id: f for f in active_profile(db_session)[1]}
    assert v2 == 2
    assert facts["fact_exp_001"].verified_by_user and not facts["fact_exp_002"].verified_by_user
    assert [f.fact_id for f in verified_facts(db_session)] == ["fact_exp_001"]


def test_import_event_has_no_values(db_session: Session) -> None:
    import_profile(db_session, load_profile_file(FIXTURE))
    ev = db_session.execute(select(AuditEventRow).where(
        AuditEventRow.action == "profile_imported")).scalar_one()
    assert "asha@example.com" not in ev.redacted_details_json
    assert "Asha" not in ev.redacted_details_json
    assert json.loads(ev.redacted_details_json)["fact_counts"]["skill"] == 2


def test_changed_fact_invalidates_cited_applications(db_session: Session) -> None:
    from jobpilot.orchestrator.approval import ApprovalService
    from tests.factories import seed_ready_apps

    import_profile(db_session, load_profile_file(FIXTURE))
    app1, app2 = seed_ready_apps(db_session, 2)
    for app_id, fact in ((app1, "fact_exp_002"), (app2, "fact_exp_001")):
        row = db_session.execute(select(FormFieldAnswerRow).where(
            FormFieldAnswerRow.application_id == app_id)).scalar_one()
        row.source_fact_id = fact
    db_session.flush()
    svc = ApprovalService(db_session)
    for app_id in (app1, app2):
        batch = svc.build_batch([app_id])
        svc.approve(batch.batch_id, 1, batch.content_hash, [app_id])
    assert svc.active_approval_for(app1) is not None

    import_profile(db_session, edited(fact_exp_002="Reduced latency by 40 percent"))

    status = {a.application_id: a.status for a in db_session.execute(
        select(ApplicationRow)).scalars()}
    assert status[app1] == ApplicationStatus.READY_FOR_REVIEW
    assert status[app2] == ApplicationStatus.APPROVED
    assert svc.active_approval_for(app1) is None
    assert svc.active_approval_for(app2) is not None


def test_candidate_years(db_session: Session) -> None:
    import_profile(db_session, load_profile_file(FIXTURE))
    assert candidate_years(db_session, datetime(2026, 7, 15, tzinfo=UTC)) == 3.5


def test_unknown_keys_forbidden() -> None:
    data = yaml.safe_load(FIXTURE.read_text())
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        ProfileFile.model_validate(data)


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "storage", db_path=tmp_path / "storage/x.db",
                     model_routes={})


def test_preferences_defaults_and_storage_override(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    prefs = load_search_preferences(cfg)
    assert prefs.min_match_score == 70 and "Remote India" in prefs.locations
    assert [c.token for c in load_companies(cfg)] == ["exampleai", "samplelabs"]

    prof = tmp_path / "storage" / "profiles"
    prof.mkdir(parents=True)
    (prof / "search_preferences.yaml").write_text(
        yaml.safe_dump({"search": {"target_titles": ["Custom Role"], "min_match_score": 55}}))
    (prof / "companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"name": "Mine", "source": "lever", "token": "mine"}]}))
    prefs = load_search_preferences(cfg)
    assert prefs.target_titles == ["Custom Role"] and prefs.min_match_score == 55
    assert [c.name for c in load_companies(cfg)] == ["Mine"]


def _cli_config(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "app.yaml").write_text(yaml.safe_dump({
        "db_path": str(tmp_path / "storage" / "jobpilot.db"),
        "storage_path": str(tmp_path / "storage")}))
    (d / "model_routes.yaml").write_text(yaml.safe_dump({"routes": {}}))
    return d


def test_cli_profile_import_prints_counts_not_values(tmp_path: Path) -> None:
    cfg = _cli_config(tmp_path)
    res = CliRunner().invoke(cli.app, ["profile", "import", "--path", str(FIXTURE),
                                        "--config-dir", str(cfg)])
    assert res.exit_code == 0, res.output
    assert "version 1" in res.output and "skill: 2" in res.output
    assert "Confirm expected CTC range" in res.output
    assert "asha@example.com" not in res.output and "Asha" not in res.output
    assert "RAG" not in res.output


def test_cli_profile_import_missing_file(tmp_path: Path) -> None:
    cfg = _cli_config(tmp_path)
    res = CliRunner().invoke(cli.app, ["profile", "import", "--path",
                                        str(tmp_path / "nope.yaml"), "--config-dir", str(cfg)])
    assert res.exit_code == 1 and "not found" in res.output
