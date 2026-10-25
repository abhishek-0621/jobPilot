"""Story bank: schema, import, verification, migration (fictional example data only)."""

import json
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from jobpilot.data.tables import AuditEventRow, StoryBankRow, StoryRow
from jobpilot.profile.stories import (
    QuestionKey,
    StoryFile,
    Theme,
    import_stories,
    load_stories_file,
    stored_bank,
    verified_stories,
    verify_story,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "stories.example.yaml"


def data() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_example_loads() -> None:
    sf = load_stories_file(EXAMPLE)
    assert [s.story_id for s in sf.stories] == ["latency-cut", "missed-deadline"]
    assert Theme.FAILURE in sf.stories[1].themes
    assert QuestionKey.WEAKNESSES in sf.canned
    assert not any(s.verified for s in sf.stories)


def test_schema_rejects_unknown_keys_and_themes() -> None:
    d = data()
    d["stories"][0]["colour"] = "blue"
    with pytest.raises(ValidationError):
        StoryFile.model_validate(d)
    d = data()
    d["stories"][0]["themes"] = ["heroism"]
    with pytest.raises(ValidationError):
        StoryFile.model_validate(d)
    d = data()
    d["canned"] = {"favourite_colour": "blue"}
    with pytest.raises(ValidationError):
        StoryFile.model_validate(d)
    d = data()
    d["extra_top"] = 1
    with pytest.raises(ValidationError):
        StoryFile.model_validate(d)
    d = data()
    d["stories"].append(dict(d["stories"][0]))
    with pytest.raises(ValidationError):  # duplicate story_id
        StoryFile.model_validate(d)
    d = data()
    d["stories"][0]["story_id"] = "Not A Slug"
    with pytest.raises(ValidationError):
        StoryFile.model_validate(d)


def test_import_idempotent(db_session: Session) -> None:
    sf = StoryFile.model_validate(data())
    first = import_stories(db_session, sf)
    second = import_stories(db_session, sf)
    rows = db_session.execute(select(StoryRow).order_by(StoryRow.position)).scalars().all()
    assert [r.story_id for r in rows] == ["latency-cut", "missed-deadline"]
    assert first["stories"] == second["stories"] == 2
    assert second["changed"] == 0 and first["added"] == 2
    bank = db_session.get(StoryBankRow, "default")
    assert bank is not None and json.loads(bank.canned_json)["weaknesses"]


def test_changed_story_becomes_unverified(db_session: Session) -> None:
    import_stories(db_session, StoryFile.model_validate(data()))
    verify_story(db_session, "latency-cut", True)
    verify_story(db_session, "missed-deadline", True)
    d = data()
    d["stories"][0]["result"] = "Median response time fell a lot."
    out = import_stories(db_session, StoryFile.model_validate(d))
    assert out["changed"] == 1
    assert {s.story_id for s in verified_stories(db_session)} == {"missed-deadline"}
    d["stories"] = d["stories"][1:]  # removed from the file -> removed from the bank
    import_stories(db_session, StoryFile.model_validate(d))
    assert db_session.get(StoryRow, "latency-cut") is None


def test_file_verified_flag_honoured(db_session: Session) -> None:
    d = data()
    d["stories"][1]["verified"] = True
    import_stories(db_session, StoryFile.model_validate(d))
    assert [s.story_id for s in verified_stories(db_session)] == ["missed-deadline"]


def test_verify_story_audit_event(db_session: Session) -> None:
    import_stories(db_session, StoryFile.model_validate(data()))
    verify_story(db_session, "latency-cut", True, actor="user")
    ev = db_session.execute(select(AuditEventRow).where(
        AuditEventRow.action == "story_verified")).scalars().one()
    assert ev.actor == "user" and ev.entity_id == "latency-cut"
    details = json.loads(ev.redacted_details_json)
    assert details == {"story_id": "latency-cut", "verified": True}
    imported = db_session.execute(select(AuditEventRow).where(
        AuditEventRow.action == "stories_imported")).scalars().one()
    assert "Example Corp" not in imported.redacted_details_json  # counts only
    with pytest.raises(LookupError):
        verify_story(db_session, "nope", True)


def test_stored_bank_round_trip(db_session: Session) -> None:
    import_stories(db_session, StoryFile.model_validate(data()))
    bank = stored_bank(db_session)
    assert bank.standing_rules and bank.canned[QuestionKey.WEAKNESSES].startswith("I can")
    assert [s.story.story_id for s in bank.stories] == ["latency-cut", "missed-deadline"]
    assert not any(s.verified for s in bank.stories)


def _alembic(db: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/jobpilot/data/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


def test_migration_0010_upgrade_downgrade(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    cfg = _alembic(db)
    command.upgrade(cfg, "head")

    def tables() -> set[str]:
        engine = create_engine(f"sqlite:///{db}")
        try:
            return set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    assert {"story", "story_bank"} <= tables()
    command.downgrade(cfg, "0009_outreach")
    assert not {"story", "story_bank"} & tables()
    command.upgrade(cfg, "head")
    assert "story" in tables()


def test_cli_profile_import_also_imports_stories(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from jobpilot import cli
    from tests.unit.test_profile import FIXTURE, _cli_config

    cfg = _cli_config(tmp_path)
    prof_dir = tmp_path / "profiles"
    prof_dir.mkdir()
    (prof_dir / "profile.yaml").write_text(FIXTURE.read_text())
    res = CliRunner().invoke(cli.app, ["profile", "import", "--path",
                                        str(prof_dir / "profile.yaml"), "--config-dir", str(cfg)])
    assert res.exit_code == 0, res.output
    assert "No story bank" in res.output
    (prof_dir / "stories.yaml").write_text(EXAMPLE.read_text())
    res = CliRunner().invoke(cli.app, ["profile", "import", "--path",
                                        str(prof_dir / "profile.yaml"), "--config-dir", str(cfg)])
    assert res.exit_code == 0, res.output
    assert "2 stories (0 verified" in res.output
    assert "Example Corp" not in res.output and "latency" not in res.output
