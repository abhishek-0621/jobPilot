"""Plan 10 review findings."""

import io
import math
import sqlite3
import tarfile
from pathlib import Path

import httpcore
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

from jobpilot.agents.match import MatchAssessment, make_match_handler
from jobpilot.api.actions import ActionConflict, start_discovery
from jobpilot.config import AppConfig
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import TaskRow
from jobpilot.ops import backup as bk
from jobpilot.orchestrator.engine import Orchestrator, OutputRejected
from jobpilot.outreach import contacts
from jobpilot.profile.preferences import SearchPreferences
from tests.factories import make_app_row


def _alembic(db: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "src/jobpilot/data/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


def test_migration_0011_keeps_one_active_discover(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    command.upgrade(_alembic(db), "0010_story_bank")
    with sqlite3.connect(db) as c:
        for tid, st in (("t1", "RUNNING"), ("t2", "RUNNING"), ("t3", "QUEUED")):
            c.execute("INSERT INTO task (task_id, kind, entity_type, entity_id, status, "
                      "attempts) VALUES (?, 'DISCOVER', 'run', ?, ?, 0)", (tid, tid, st))
    command.upgrade(_alembic(db), "head")
    with sqlite3.connect(db) as c:
        rows = dict(c.execute("SELECT task_id, status FROM task").fetchall())
    assert rows == {"t1": "RUNNING", "t2": "CANCELLED", "t3": "CANCELLED"}


@pytest.mark.parametrize("ip,ok", [
    ("93.184.216.34", True), ("2606:2800:220:1::1", True),
    ("100.64.0.1", False), ("10.0.0.1", False), ("127.0.0.1", False),
    ("169.254.1.1", False), ("::ffff:10.0.0.1", False),
    ("64:ff9b::a00:1", False),          # NAT64 of 10.0.0.1
    ("2002:a00:1::1", False),           # 6to4 of 10.0.0.1
    ("2001:0:4136:e378:8000:63bf:f5ff:fffe", False),  # Teredo, client 10.0.0.1
])
def test_public_ip_uses_is_global(ip: str, ok: bool) -> None:
    assert contacts._public_ip(ip) is ok


async def test_pinned_backend_connects_to_checked_ip() -> None:
    seen: list[str] = []

    class Inner(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None,  # type: ignore[no-untyped-def]
                              socket_options=None):
            seen.append(host)
            raise httpcore.ConnectError("stop")

    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    backend = contacts.PinnedBackend(resolve, inner=Inner())
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("acme.com", 443)
    assert seen == ["93.184.216.34"]  # the checked address, not a fresh lookup

    async def rebound(host: str) -> list[str]:
        return ["10.0.0.1"]
    seen.clear()
    with pytest.raises(httpcore.ConnectError, match="non-public"):
        await contacts.PinnedBackend(rebound, inner=Inner()).connect_tcp("acme.com", 443)
    assert seen == []


def test_restore_ratio_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bk, "MIN_RESTORE_CAP_BYTES", 0)
    arc = tmp_path / "a.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        info = tarfile.TarInfo("db.sqlite")
        info.size = 2_000_000
        t.addfile(info, io.BytesIO(b"\0" * info.size))
    (tmp_path / "d").mkdir()
    with pytest.raises(bk.RestoreError, match="too large"):
        bk._extract_verified(arc, tmp_path / "d")


def test_start_discovery_db_locked_is_conflict(tmp_path: Path, monkeypatch) -> None:
    upgrade_to_head(tmp_path / "x.db")
    factory = make_session_factory(make_engine(tmp_path / "x.db"))
    orch = Orchestrator(factory, AppConfig(model_routes={}), {}, {})

    def locked(*a, **kw):  # type: ignore[no-untyped-def]
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(orch, "enqueue_in", locked)
    with factory() as s, pytest.raises(ActionConflict):
        start_discovery(s, orch)


def test_nan_score_rejected_by_match_handler(db_session) -> None:
    app = make_app_row(db_session)
    out = MatchAssessment.model_construct(
        job_id=app.job_id, profile_version=1, task_id="t", total_score=math.nan,
        skill_fit=50, project_fit=50, role_seniority_fit=50, experience_fit=50,
        location_fit=50, hard_filter_passed=True, matched_evidence_fact_ids=[],
        missing_requirements=[], concerns=[], explanation="", confidence=0.5)
    task = TaskRow(task_id="t", kind="MATCH", entity_type="job", entity_id=app.job_id,
                   status="RUNNING", attempts=1)
    with pytest.raises(OutputRejected, match="non_finite_score"):
        make_match_handler(SearchPreferences())(db_session, task, out)
