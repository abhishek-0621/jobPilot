"""`jobpilot` command line: setup, doctor, start, migrate."""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import httpx
import typer
import uvicorn

from jobpilot.api.main import app_from_config, compute_health
from jobpilot.config import AppConfig, load_config
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head

app = typer.Typer(help="JobPilot: local-first job applications.", no_args_is_help=True)

ConfigDir = Annotated[Path, typer.Option(help="Directory holding app.yaml / model_routes.yaml")]
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
    typer.echo(f"JobPilot running at http://{cfg.bind_host}:{cfg.port}  (Ctrl+C to stop)")
    uvicorn.run(api, host=cfg.bind_host, port=cfg.port)


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
            version = import_profile(s, pf)
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
