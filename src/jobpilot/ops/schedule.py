"""macOS LaunchAgent scheduling for discovery (and optionally the server).

Only an allowlisted set of `jobpilot` subcommands can ever be scheduled: nothing that
submits or sends outreach. The home directory and `launchctl` are injected so tests
never touch the real system.
"""

import plistlib
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import AppConfig
from jobpilot.orchestrator.engine import Orchestrator, TaskKind

DISCOVER_LABEL = "com.jobpilot.discover"
SERVER_LABEL = "com.jobpilot.server"
ALLOWED_SUBCOMMANDS = frozenset({"discover", "maintenance", "backup", "start"})
DISCOVERY_KINDS = frozenset({TaskKind.DISCOVER, TaskKind.EXTRACT, TaskKind.MATCH})
LAUNCH_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class AgentStatus:
    label: str
    installed: bool
    loaded: bool
    next_run: str | None


def _subcommand(program_args: Sequence[str]) -> str:
    args = list(program_args)
    if "jobpilot" not in args:
        raise ValueError("program_args must invoke `jobpilot`")
    i = args.index("jobpilot") + 1
    if i >= len(args) or args[i] not in ALLOWED_SUBCOMMANDS:
        raise ValueError(
            f"jobpilot subcommand not allowed in a LaunchAgent: {args[i:i + 1] or 'none'}")
    return args[i]


def render_plist(
    label: str,
    program_args: Sequence[str],
    *,
    calendar: Mapping[str, int] | None = None,
    keep_alive: bool = False,
    log_path: Path,
    env: Mapping[str, str],
    working_dir: Path | None = None,
) -> bytes:
    """A LaunchAgent plist. Raises ValueError if the jobpilot subcommand is not allowed."""
    _subcommand(program_args)
    d: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [str(a) for a in program_args],
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": dict(env),
    }
    if working_dir is not None:
        d["WorkingDirectory"] = str(working_dir)
    if calendar is not None:
        d["StartCalendarInterval"] = dict(calendar)
    if keep_alive:
        d["RunAtLoad"] = True
        d["KeepAlive"] = True
    return plistlib.dumps(d)


def parse_hhmm(at: str) -> tuple[int, int]:
    try:
        h, m = at.split(":")
        hour, minute = int(h), int(m)
    except ValueError as e:
        raise ValueError("time must be HH:MM") from e
    if not (len(h) == 2 and len(m) == 2 and 0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time must be HH:MM (24h)")
    return hour, minute


def _agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def plist_path(home: Path, label: str) -> Path:
    return _agents_dir(home) / f"{label}.plist"


def logs_dir(cfg: AppConfig, repo_dir: Path) -> Path:
    base = cfg.storage_path if cfg.storage_path.is_absolute() else repo_dir / cfg.storage_path
    return base / "logs"


def _target(uid: int, label: str) -> str:
    return f"gui/{uid}/{label}"


def _loaded(runner: Runner, uid: int, label: str) -> bool:
    return runner(["launchctl", "print", _target(uid, label)]).returncode == 0


def _jobs(
    cfg: AppConfig, *, at: str, with_server: bool, uv_path: str, repo_dir: Path,
) -> list[tuple[str, list[str], dict[str, int] | None, bool, str]]:
    hour, minute = parse_hhmm(at)
    base = [uv_path, "run", "--project", str(repo_dir), "jobpilot"]
    jobs: list[tuple[str, list[str], dict[str, int] | None, bool, str]] = [
        (DISCOVER_LABEL, [*base, "discover", "--run-now"],
         {"Hour": hour, "Minute": minute}, False, "discover.log")]
    if with_server:
        jobs.append((SERVER_LABEL, [*base, "start"], None, True, "server.log"))
    return jobs


def install(
    cfg: AppConfig, *, home: Path, runner: Runner, uid: int, at: str, with_server: bool,
    uv_path: str, repo_dir: Path,
) -> list[Path]:
    """Write the plists and bootstrap them. Idempotent: a loaded agent is booted out first."""
    logs = logs_dir(cfg, repo_dir)
    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(0o700)
    _agents_dir(home).mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for label, args, calendar, keep_alive, log_name in _jobs(
            cfg, at=at, with_server=with_server, uv_path=uv_path, repo_dir=repo_dir):
        data = render_plist(label, args, calendar=calendar, keep_alive=keep_alive,
                            log_path=logs / log_name, env={"PATH": LAUNCH_PATH},
                            working_dir=repo_dir)
        path = plist_path(home, label)
        path.write_bytes(data)
        if _loaded(runner, uid, label):
            runner(["launchctl", "bootout", _target(uid, label)])
        r = runner(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed for {label}")
        written.append(path)
    return written


def uninstall(*, home: Path, runner: Runner, uid: int) -> list[str]:
    """Boot out and remove whichever JobPilot agents are installed. Returns their labels."""
    removed: list[str] = []
    for label in (DISCOVER_LABEL, SERVER_LABEL):
        path = plist_path(home, label)
        if _loaded(runner, uid, label):
            runner(["launchctl", "bootout", _target(uid, label)])
        if path.exists():
            path.unlink()
            removed.append(label)
    return removed


def next_run(at: str, now: datetime) -> datetime:
    hour, minute = parse_hhmm(at)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def status(
    cfg: AppConfig, *, home: Path, runner: Runner, uid: int,
    now: Callable[[], datetime] = datetime.now,
) -> list[AgentStatus]:
    out: list[AgentStatus] = []
    for label in (DISCOVER_LABEL, SERVER_LABEL):
        path = plist_path(home, label)
        loaded = _loaded(runner, uid, label)
        nxt: str | None = None
        if path.exists():
            plist = plistlib.loads(path.read_bytes())
            cal = plist.get("StartCalendarInterval")
            if loaded and cal:
                nxt = next_run(f"{cal['Hour']:02d}:{cal['Minute']:02d}",
                               now()).strftime("%Y-%m-%d %H:%M")
            elif loaded and plist.get("KeepAlive"):
                nxt = "running at login (kept alive)"
        out.append(AgentStatus(label, path.exists(), loaded, nxt))
    return out


async def run_discovery_now(
    cfg: AppConfig, factory: sessionmaker[Session], runtime: SimpleNamespace | Any,
) -> int:
    """One in-process discovery cycle: DISCOVER, EXTRACT and MATCH only. Other kinds
    (tailor, prepare, submit, ...) are neither registered nor leased, so they stay
    queued for the server. Returns the number of tasks run."""
    from jobpilot.api.actions import ActionConflict, start_discovery

    agents = {k: a for k, a in runtime.agents.items() if k in DISCOVERY_KINDS}
    handlers = {k: h for k, h in runtime.handlers.items() if k in DISCOVERY_KINDS}
    orch = Orchestrator(factory, cfg, agents, runtime.adapters, handlers=handlers,
                        tools=runtime.tools)
    with factory() as s:
        try:
            start_discovery(s, orch)
        except ActionConflict:
            # a discovery run is already queued/running: just work through it
            s.rollback()
    return await orch.run_until_idle(kinds=set(DISCOVERY_KINDS))
