import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from jobpilot.config import AppConfig
from jobpilot.contracts.models import AgentOutput
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import TaskRow
from jobpilot.ops import schedule as sched
from jobpilot.orchestrator.engine import Orchestrator, TaskKind, TaskStatus, noop_handler

UV = "/opt/homebrew/bin/uv"


class FakeLaunchctl:
    def __init__(self, loaded: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.loaded = loaded or set()

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        rc = 0
        if args[1] == "print":
            rc = 0 if args[2] in self.loaded else 113
        elif args[1] == "bootout":
            self.loaded.discard(args[2])
        elif args[1] == "bootstrap":
            self.loaded.add(f"{args[2]}/" + Path(args[3]).stem)
        return subprocess.CompletedProcess(args, rc)


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path / "repo" / "storage", model_routes={})


def _install(tmp_path: Path, run: FakeLaunchctl, **kw: object) -> list[Path]:
    args: dict = {"at": "08:30", "with_server": False}
    args.update(kw)
    return sched.install(_cfg(tmp_path), home=tmp_path / "home", runner=run, uid=501,
                         uv_path=UV, repo_dir=tmp_path / "repo", **args)


def test_plist_commands_allowlisted(tmp_path: Path) -> None:
    kw = {"log_path": tmp_path / "l.log", "env": {"PATH": "/bin"}}
    for sub in ("discover", "maintenance", "backup", "start"):
        sched.render_plist("x", [UV, "run", "jobpilot", sub], **kw)
    for bad in ("submit", "restore", "send", "setup", "delete"):
        with pytest.raises(ValueError):
            sched.render_plist("x", [UV, "run", "jobpilot", bad], **kw)
    with pytest.raises(ValueError):
        sched.render_plist("x", [UV, "run", "python", "-c", "pass"], **kw)
    with pytest.raises(ValueError):
        sched.render_plist("x", [UV, "run", "jobpilot"], **kw)


def test_install_writes_plist_and_bootstraps(tmp_path: Path) -> None:
    run = FakeLaunchctl()
    paths = _install(tmp_path, run, at="07:05", with_server=True)
    assert [p.name for p in paths] == ["com.jobpilot.discover.plist",
                                       "com.jobpilot.server.plist"]
    assert all(p.parent == tmp_path / "home" / "Library" / "LaunchAgents" for p in paths)
    d = plistlib.loads(paths[0].read_bytes())
    assert d["ProgramArguments"] == [UV, "run", "--project", str(tmp_path / "repo"),
                                     "jobpilot", "discover", "--run-now"]
    assert d["StartCalendarInterval"] == {"Hour": 7, "Minute": 5}
    log = str(tmp_path / "repo" / "storage" / "logs" / "discover.log")
    assert d["StandardOutPath"] == log == d["StandardErrorPath"]
    assert "/opt/homebrew/bin" in d["EnvironmentVariables"]["PATH"]
    s = plistlib.loads(paths[1].read_bytes())
    assert s["RunAtLoad"] is True and s["KeepAlive"] is True
    assert s["ProgramArguments"][-2:] == ["jobpilot", "start"]
    assert (tmp_path / "repo" / "storage" / "logs").stat().st_mode & 0o777 == 0o700
    boots = [c for c in run.calls if c[1] == "bootstrap"]
    assert boots[0] == ["launchctl", "bootstrap", "gui/501", str(paths[0])]
    assert not any(c[1] == "bootout" for c in run.calls)


def test_install_is_idempotent_boots_out_first(tmp_path: Path) -> None:
    run = FakeLaunchctl({"gui/501/com.jobpilot.discover"})
    _install(tmp_path, run)
    verbs = [c[1] for c in run.calls]
    assert verbs == ["print", "bootout", "bootstrap"]


def test_uninstall_bootout_and_removes(tmp_path: Path) -> None:
    run = FakeLaunchctl()
    paths = _install(tmp_path, run, with_server=True)
    removed = sched.uninstall(home=tmp_path / "home", runner=run, uid=501)
    assert removed == ["com.jobpilot.discover", "com.jobpilot.server"]
    assert not any(p.exists() for p in paths)
    assert ["launchctl", "bootout", "gui/501/com.jobpilot.discover"] in run.calls
    assert sched.uninstall(home=tmp_path / "home", runner=run, uid=501) == []


def test_status_reports_loaded_and_next_run(tmp_path: Path) -> None:
    from datetime import datetime
    run = FakeLaunchctl()
    _install(tmp_path, run)
    st = sched.status(_cfg(tmp_path), home=tmp_path / "home", runner=run, uid=501,
                      now=lambda: datetime(2026, 10, 25, 9, 0))
    assert st[0] == sched.AgentStatus("com.jobpilot.discover", True, True, "2026-10-26 08:30")
    assert st[1] == sched.AgentStatus("com.jobpilot.server", False, False, None)


class _Out(AgentOutput):
    pass


class _Agent:
    prompt_version = "v1"
    output_model: type[BaseModel] = _Out

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def run(self, inp, ctx):  # type: ignore[no-untyped-def]
        self.calls += 1
        return _Out(task_id=ctx.task_id, created_at=ctx.clock())


async def test_run_now_only_discovery_kinds(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    upgrade_to_head(db)
    engine = make_engine(db)
    factory = make_session_factory(engine)
    cfg = AppConfig(model_routes={})
    helper = Orchestrator(factory, cfg, {}, {})
    tailor_id = helper.enqueue(TaskKind.TAILOR, "job", "j1")
    submit_id = helper.enqueue(TaskKind.SUBMIT, "application", "a1")
    agents = {k: _Agent(k.value) for k in
              (TaskKind.DISCOVER, TaskKind.TAILOR, TaskKind.SUBMIT)}
    runtime = SimpleNamespace(
        agents=agents, handlers={k: noop_handler for k in agents}, adapters={}, tools={})
    steps = await sched.run_discovery_now(cfg, factory, runtime)
    assert steps == 1
    assert agents[TaskKind.DISCOVER].calls == 1
    assert agents[TaskKind.TAILOR].calls == agents[TaskKind.SUBMIT].calls == 0
    with factory() as s:
        rows = {t.task_id: t for t in s.execute(select(TaskRow)).scalars()}
    assert rows[tailor_id].status == TaskStatus.QUEUED.value
    assert rows[submit_id].status == TaskStatus.QUEUED.value
    # a second call while a DISCOVER is queued reuses it instead of raising
    helper.enqueue(TaskKind.DISCOVER, "run", "run_x")
    assert await sched.run_discovery_now(cfg, factory, runtime) == 1
    engine.dispose()


def test_schedule_config_validates_time() -> None:
    assert AppConfig(model_routes={}).schedule.discover_at == "08:30"
    with pytest.raises(ValueError):
        AppConfig(schedule={"discover_at": "25:00"}, model_routes={})
