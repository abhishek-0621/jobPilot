"""`jobpilot` command line: setup, doctor, start, migrate."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import uvicorn

from jobpilot.api.main import app_from_config, compute_health
from jobpilot.config import AppConfig, load_config
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head

app = typer.Typer(help="JobPilot: local-first job applications.", no_args_is_help=True)

ConfigDir = Annotated[Path, typer.Option(help="Directory holding app.yaml / model_routes.yaml")]
_BENCH_HINT = "Run `uv run jobpilot bench` once to pick model tags for your Mac."
_OLLAMA_DOWN = "Start Ollama (open the Ollama app or run `ollama serve`) and re-run setup"


def _wanted_models(cfg: AppConfig) -> list[str]:
    tags: set[str] = set()
    for route in cfg.model_routes.values():
        tags.add(route.model_tag)
        if route.fallback:
            tags.add(route.fallback)
    return sorted(tags)


def _ollama_tags(cfg: AppConfig) -> set[str] | None:
    """Model names the local Ollama server has, or None if it is unreachable."""
    try:
        r = httpx.get(f"{cfg.ollama_url.rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    return {m["name"] for m in r.json().get("models", [])}


def _migrate(cfg: AppConfig) -> None:
    cfg.storage_path.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(cfg.db_path)


@app.command()
def setup(config_dir: ConfigDir = Path("config")) -> None:
    """Install the browser, pull local models, create storage and migrate. Safe to re-run."""
    cfg = load_config(config_dir)
    typer.echo("Installing Playwright Chromium…")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    if shutil.which("ollama") is None:
        typer.echo("Ollama is not installed. Install it from https://ollama.com/download "
                   "and re-run setup.")
        raise typer.Exit(1)
    available = _ollama_tags(cfg)
    if available is None:
        typer.echo(_OLLAMA_DOWN)
        raise typer.Exit(1)
    for tag in _wanted_models(cfg):
        if tag in available:
            typer.echo(f"Model {tag}: present")
            continue
        typer.echo(f"Pulling model {tag}…")
        subprocess.run(["ollama", "pull", tag], check=True)  # streams to this terminal
    _migrate(cfg)
    typer.echo(f"Storage ready at {cfg.storage_path}; database migrated.")
    typer.echo("Setup complete. Next: uv run jobpilot start")
    typer.echo(_BENCH_HINT)


@app.command()
def doctor(config_dir: ConfigDir = Path("config")) -> None:
    """Run the /health checks locally; exit non-zero on any failure."""
    cfg = load_config(config_dir)
    available = _ollama_tags(cfg)
    if not cfg.db_path.exists():
        revision = ""
        missing_db = True
    else:
        missing_db = False
        engine = make_engine(cfg.db_path)
        try:
            health = compute_health(cfg, make_session_factory(engine), available)
        finally:
            engine.dispose()
        revision = health.db_revision
    wanted = _wanted_models(cfg)
    missing = wanted if available is None else [t for t in wanted if t not in available]
    checks = [
        ("ollama", available is not None, "reachable" if available is not None
         else f"unreachable at {cfg.ollama_url}"),
        ("models", not missing, "all present" if not missing
         else "missing: " + ", ".join(missing)),
        ("database", bool(revision), revision if revision
         else "not initialised — run `uv run jobpilot setup`" if missing_db
         else "not migrated — run `uv run jobpilot migrate`"),
    ]
    for name, ok, detail in checks:
        typer.echo(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    typer.echo(f"[info] dry_run: {cfg.dry_run}")
    if not all(ok for _, ok, _ in checks):
        raise typer.Exit(1)


@app.command()
def start(config_dir: ConfigDir = Path("config")) -> None:
    """Recover interrupted work, start the worker and serve the dashboard."""
    cfg = load_config(config_dir)
    _migrate(cfg)
    api = app_from_config(config_dir)  # lifespan runs recover_on_startup + worker loop
    from jobpilot.ops.backup import remove_pid_file, write_pid_file

    typer.echo(f"JobPilot running at http://{cfg.bind_host}:{cfg.port}  (Ctrl+C to stop)")
    write_pid_file(cfg)
    try:
        uvicorn.run(api, host=cfg.bind_host, port=cfg.port)
    finally:
        remove_pid_file(cfg)


@app.command()
def backup(
    keep: Annotated[int | None, typer.Option(min=1, help="Archives to keep")] = None,
    config_dir: ConfigDir = Path("config"),
) -> None:
    """Back up the database, profiles and artifacts to storage/backups."""
    from jobpilot.ops.backup import backup as do_backup

    cfg = load_config(config_dir)
    path = do_backup(cfg, keep=keep)
    typer.echo(f"Backup written: {path} ({path.stat().st_size:,} bytes)")
    typer.echo("Warning: backups contain your personal data. Keep them private.")


@app.command()
def restore(
    archive: Annotated[Path, typer.Argument(help="Backup .tar.gz to restore")],
    confirm: Annotated[str, typer.Option(help="Type RESTORE to confirm")] = "",
    config_dir: ConfigDir = Path("config"),
) -> None:
    """Replace the database, profiles and artifacts with a backup (CLI only)."""
    from jobpilot.ops.backup import RestoreError
    from jobpilot.ops.backup import restore as do_restore

    cfg = load_config(config_dir)
    try:
        pre = do_restore(cfg, archive, confirm=confirm)
    except RestoreError as e:
        typer.echo(f"Restore refused: {e}")
        raise typer.Exit(1) from e
    typer.echo(f"Restored from {archive}. Previous data saved in {pre}")


@app.command()
def migrate(config_dir: ConfigDir = Path("config")) -> None:
    """Upgrade the database schema to the latest revision."""
    cfg = load_config(config_dir)
    _migrate(cfg)
    typer.echo(f"Database at {cfg.db_path} is at head.")


profile_app = typer.Typer(help="Candidate profile commands.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")


@profile_app.command("import")
def profile_import(
    path: Annotated[Path, typer.Option(help="Profile YAML to import")] = Path(
        "storage/profiles/profile.yaml"),
    config_dir: ConfigDir = Path("config"),
) -> None:
    """Import the profile YAML as a new version. Prints counts only, never values."""
    from jobpilot.profile.importer import active_profile, import_profile
    from jobpilot.profile.schema import load_profile_file

    if not path.exists():
        typer.echo(f"Profile file not found: {path}. Create it (see "
                   "tests/fixtures/profile_example.yaml for the layout) or pass --path.")
        raise typer.Exit(1)
    cfg = load_config(config_dir)
    _migrate(cfg)
    pf = load_profile_file(path)
    engine = make_engine(cfg.db_path)
    try:
        with make_session_factory(engine)() as s:
            version = import_profile(s, pf, aliases_path=cfg.field_aliases_path)
            facts = active_profile(s)[1]
            s.commit()
    finally:
        engine.dispose()
    counts: dict[str, int] = {}
    for f in facts:
        counts[f.category] = counts.get(f.category, 0) + 1
    typer.echo(f"Imported profile version {version}")
    for cat in sorted(counts):
        typer.echo(f"  {cat}: {counts[cat]}")
    typer.echo(f"  verified (carried over): {sum(f.verified_by_user for f in facts)}")
    if pf.open_questions:
        typer.echo("Open questions:")
        for q in pf.open_questions:
            typer.echo(f"  - {q}")


def _server_running(cfg: AppConfig) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{cfg.port}/health", timeout=2)
    except httpx.HTTPError:
        return False
    return True


async def _discover_now(cfg: AppConfig) -> int:
    from jobpilot.ops.schedule import run_discovery_now
    from jobpilot.runtime import build_runtime

    engine = make_engine(cfg.db_path)
    try:
        factory = make_session_factory(engine)
        runtime = build_runtime(cfg, factory)
        try:
            return await run_discovery_now(cfg, factory, runtime)
        finally:
            await runtime.aclose()
    finally:
        engine.dispose()


def _run_maintenance_if_available(cfg: AppConfig) -> None:
    """Task 4 adds `jobpilot.ops.maintenance.run_maintenance`; until then this is a no-op."""
    import importlib
    import importlib.util

    if importlib.util.find_spec("jobpilot.ops.maintenance") is None:
        return
    importlib.import_module("jobpilot.ops.maintenance").run_maintenance(cfg)


@app.command()
def discover(
    config_dir: ConfigDir = Path("config"),
    run_now: Annotated[bool, typer.Option(
        "--run-now", help="Run discover, extract and match in-process (no server needed)")
    ] = False,
) -> None:
    """Queue a discovery run; the worker in `jobpilot start` processes it."""
    import asyncio

    from jobpilot.api.actions import ActionConflict, start_discovery
    from jobpilot.orchestrator.engine import Orchestrator

    cfg = load_config(config_dir)
    _migrate(cfg)
    if run_now:
        steps = asyncio.run(_discover_now(cfg))
        typer.echo(f"Discovery cycle finished ({steps} tasks). "
                   "Other work stays queued for `jobpilot start`.")
        _run_maintenance_if_available(cfg)
        return
    engine = make_engine(cfg.db_path)
    try:
        factory = make_session_factory(engine)
        with factory() as s:
            try:
                _task_id, run_id = start_discovery(s, Orchestrator(factory, cfg, {}, {}))
            except ActionConflict as e:
                typer.echo(str(e))
                raise typer.Exit(1) from e
    finally:
        engine.dispose()
    typer.echo(f"Queued discovery run {run_id}")
    if not _server_running(cfg):
        typer.echo("Run `uv run jobpilot start` to process it.")


schedule_app = typer.Typer(help="Daily discovery via macOS LaunchAgents.", no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")
_REPO_DIR = Path(__file__).resolve().parents[2]


def _require_macos() -> None:
    if sys.platform != "darwin":
        typer.echo("Scheduling uses macOS LaunchAgents and is only available on macOS.")
        raise typer.Exit(1)


def _launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


@schedule_app.command("install")
def schedule_install(
    config_dir: ConfigDir = Path("config"),
    at: Annotated[str | None, typer.Option(help="Daily time HH:MM (default: schedule.discover_at)")
                  ] = None,
    with_server: Annotated[bool, typer.Option(
        "--with-server", help="Also keep `jobpilot start` running from login")] = False,
) -> None:
    """Install the daily discovery LaunchAgent (never submits anything)."""
    from jobpilot.ops import schedule as sched

    _require_macos()
    cfg = load_config(config_dir)
    when = at or cfg.schedule.discover_at
    uv = shutil.which("uv")
    if uv is None:
        typer.echo("`uv` not found on PATH; install it first.")
        raise typer.Exit(1)
    try:
        paths = sched.install(cfg, home=Path.home(), runner=_launchctl, uid=os.getuid(),
                              at=when, with_server=with_server,
                              config_dir=(None if config_dir == Path("config")
                                          else config_dir.resolve()), uv_path=os.path.abspath(uv),
                              repo_dir=_REPO_DIR)
    except (ValueError, RuntimeError) as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e
    for p in paths:
        typer.echo(f"Installed {p}")
    typer.echo(f"Discovery runs daily at {when}. Logs: {sched.logs_dir(cfg, _REPO_DIR)}")


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Remove the JobPilot LaunchAgents."""
    from jobpilot.ops import schedule as sched

    _require_macos()
    try:
        removed = sched.uninstall(home=Path.home(), runner=_launchctl, uid=os.getuid())
    except RuntimeError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e
    typer.echo("Removed: " + ", ".join(removed) if removed else "Nothing was installed.")


@schedule_app.command("status")
def schedule_status(config_dir: ConfigDir = Path("config")) -> None:
    """Show whether each agent is installed/loaded and the next run."""
    from jobpilot.ops import schedule as sched

    _require_macos()
    cfg = load_config(config_dir)
    for a in sched.status(cfg, home=Path.home(), runner=_launchctl, uid=os.getuid()):
        state = "loaded" if a.loaded else ("installed, not loaded" if a.installed
                                           else "not installed")
        typer.echo(f"{a.label}: {state}" + (f" (next: {a.next_run})" if a.next_run else ""))


sources_app = typer.Typer(help="Job source commands.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")


async def _check_sources(cfg: AppConfig, client: httpx.AsyncClient) -> list[str]:
    from jobpilot.discovery.sources.base import SourceError
    from jobpilot.profile.preferences import load_companies
    from jobpilot.runtime import build_sources

    adapters = build_sources(client)
    lines: list[str] = []
    for board in load_companies(cfg):
        label = f"{board.name} ({board.source}:{board.token})"
        try:
            postings = await adapters[board.source].list_postings(board)
        except SourceError as e:
            lines.append(f"{label}: {e.code.value}")
            continue
        lines.append(f"{label}: ok {len(postings)} postings")
    return lines


def _sources_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@sources_app.command("check")
def sources_check(config_dir: ConfigDir = Path("config")) -> None:
    """Fetch each configured board once (live) and print ok / error per board."""
    import asyncio

    cfg = load_config(config_dir)

    async def run() -> list[str]:
        async with _sources_client() as client:
            return await _check_sources(cfg, client)

    for line in asyncio.run(run()):
        typer.echo(line)


boards_app = typer.Typer(help="Company board commands.", no_args_is_help=True)
app.add_typer(boards_app, name="boards")


def _boards_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@boards_app.command("find")
def boards_find(
    names: Annotated[list[str], typer.Argument(help="Company names")],
    write: Annotated[bool, typer.Option(
        help="Append the boards found to storage/profiles/companies.yaml")] = False,
    config_dir: ConfigDir = Path("config"),
) -> None:
    """Probe the public boards (Greenhouse, Lever, Ashby, SmartRecruiters) for companies."""
    import asyncio

    from jobpilot.discovery.board_finder import find_hits
    from jobpilot.profile.preferences import CompaniesFileError, append_companies

    cfg = load_config(config_dir)

    async def run() -> list[Any]:
        async with _boards_client() as client:
            return await find_hits(names, client)

    hits = asyncio.run(run())
    if not hits:
        typer.echo("No live boards found. Workday tenants must be added by hand.")
        return
    typer.echo(f"{'company':<28}{'source':<18}{'token':<28}postings")
    for h in hits:
        typer.echo(f"{h.board.name:<28}{h.board.source:<18}{h.board.token:<28}{h.postings}")
    if write:
        try:
            added = append_companies(cfg, [h.board for h in hits])
        except CompaniesFileError as e:
            typer.echo(str(e))
            raise typer.Exit(1) from e
        target = cfg.storage_path / "profiles" / "companies.yaml"
        typer.echo(f"Added {len(added)} board(s) to {target} "
                   f"({len(hits) - len(added)} already configured).")


@app.command()
def bench(
    tags: Annotated[str | None, typer.Option(help="Comma-separated model tags")] = None,
    route: Annotated[str, typer.Option(help="Model route to benchmark")] = "job_extraction",
    fixtures: Annotated[Path, typer.Option(help="Directory of labeled job fixtures")] = Path(
        "tests/fixtures/bench/jobs"),
    config_dir: ConfigDir = Path("config"),
) -> None:
    """Benchmark local model tags on labeled job postings. Never pulls models."""
    import asyncio

    from jobpilot.bench import format_table, run_bench, write_report

    cfg = load_config(config_dir)
    if route not in cfg.model_routes:
        typer.echo(f"Unknown model route: {route}")
        raise typer.Exit(1)
    if tags:
        wanted = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        r = cfg.model_routes[route]
        wanted = list(dict.fromkeys([r.model_tag, *([r.fallback] if r.fallback else [])]))
    if _ollama_tags(cfg) is None:
        typer.echo(_OLLAMA_DOWN.replace("re-run setup", "re-run bench"))
        raise typer.Exit(1)
    results = asyncio.run(run_bench(cfg, wanted, route, fixtures))
    typer.echo(format_table(results))
    typer.echo(f"Report: {write_report(cfg, route, results)}")
