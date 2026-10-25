import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from jobpilot import cli

runner = CliRunner()


def _config(tmp_path: Path, **app: Any) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "app.yaml").write_text(yaml.safe_dump({
        "db_path": str(tmp_path / "storage" / "jobpilot.db"),
        "storage_path": str(tmp_path / "storage"), **app}))
    (d / "model_routes.yaml").write_text(yaml.safe_dump({"routes": {
        "a": {"model_tag": "m1:small"}, "b": {"model_tag": "m1:small"},
        "c": {"model_tag": "m2:big", "fallback": "m3:tiny"}}}))
    return d


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0)


def _tags(*names: str) -> Any:
    def get(url: str, **kw: Any) -> httpx.Response:
        assert url == "http://127.0.0.1:11434/api/tags"
        return httpx.Response(200, json={"models": [{"name": n} for n in names]},
                              request=httpx.Request("GET", url))
    return get


def _down(url: str, **kw: Any) -> httpx.Response:
    raise httpx.ConnectError("refused", request=httpx.Request("GET", url))


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    run = FakeRun()
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    return run


def test_setup_installs_pulls_missing_and_migrates(
    tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.httpx, "get", _tags("m1:small"))
    d = _config(tmp_path)
    r = runner.invoke(cli.app, ["setup", "--config-dir", str(d)])
    assert r.exit_code == 0, r.output
    assert fake_run.calls == [
        [sys.executable, "-m", "playwright", "install", "chromium"],
        ["ollama", "pull", "m2:big"],
        ["ollama", "pull", "m3:tiny"],
    ]
    assert (tmp_path / "storage" / "jobpilot.db").exists()
    assert "Run `uv run jobpilot bench` once to pick model tags for your Mac." in r.output
    # Idempotent: everything present -> no pulls, still succeeds.
    monkeypatch.setattr(cli.httpx, "get", _tags("m1:small", "m2:big", "m3:tiny"))
    fake_run.calls.clear()
    r2 = runner.invoke(cli.app, ["setup", "--config-dir", str(d)])
    assert r2.exit_code == 0, r2.output
    assert fake_run.calls == [[sys.executable, "-m", "playwright", "install", "chromium"]]


def test_setup_exits_when_ollama_server_down(
    tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.httpx, "get", _down)
    r = runner.invoke(cli.app, ["setup", "--config-dir", str(_config(tmp_path))])
    assert r.exit_code == 1
    assert "Start Ollama (open the Ollama app or run `ollama serve`) and re-run setup" \
        in r.output
    assert not any(c[:2] == ["ollama", "pull"] for c in fake_run.calls)


def test_setup_exits_when_ollama_not_installed(
    tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    r = runner.invoke(cli.app, ["setup", "--config-dir", str(_config(tmp_path))])
    assert r.exit_code == 1 and "ollama.com/download" in r.output


def test_doctor_ok_and_failing(
    tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _config(tmp_path)
    assert runner.invoke(cli.app, ["migrate", "--config-dir", str(d)]).exit_code == 0
    monkeypatch.setattr(cli.httpx, "get", _tags("m1:small", "m2:big", "m3:tiny"))
    ok = runner.invoke(cli.app, ["doctor", "--config-dir", str(d)])
    assert ok.exit_code == 0, ok.output
    assert "dry_run" in ok.output
    monkeypatch.setattr(cli.httpx, "get", _tags("m1:small"))
    bad = runner.invoke(cli.app, ["doctor", "--config-dir", str(d)])
    assert bad.exit_code == 1 and "m2:big" in bad.output
    monkeypatch.setattr(cli.httpx, "get", _down)
    assert runner.invoke(cli.app, ["doctor", "--config-dir", str(d)]).exit_code == 1


def test_doctor_fails_without_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.httpx, "get", _tags("m1:small", "m2:big", "m3:tiny"))
    r = runner.invoke(cli.app, ["doctor", "--config-dir", str(_config(tmp_path))])
    assert r.exit_code == 1 and "jobpilot setup" in r.output
    assert not (tmp_path / "storage" / "jobpilot.db").exists()


def test_start_binds_configured_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_uvicorn_run(app: Any, **kw: Any) -> None:
        seen.update(kw, app=app)

    monkeypatch.setattr(cli.uvicorn, "run", fake_uvicorn_run)
    r = runner.invoke(cli.app, ["start", "--config-dir", str(_config(tmp_path, port=9911))])
    assert r.exit_code == 0, r.output
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9911
    assert "http://127.0.0.1:9911" in r.output
    assert (tmp_path / "storage" / "jobpilot.db").exists()


def test_discover_enqueues_and_refuses_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.httpx, "get", _down)
    d = _config(tmp_path)
    r = runner.invoke(cli.app, ["discover", "--config-dir", str(d)])
    assert r.exit_code == 0, r.output
    assert "Queued discovery run run" in r.output
    assert "Run `uv run jobpilot start` to process it." in r.output
    r2 = runner.invoke(cli.app, ["discover", "--config-dir", str(d)])
    assert r2.exit_code == 1 and "already queued or running" in r2.output


def test_sources_check_reports_per_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _config(tmp_path)
    prof = tmp_path / "storage" / "profiles"
    prof.mkdir(parents=True)
    (prof / "companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"name": "Example AI", "source": "greenhouse", "token": "exampleai"},
        {"name": "Sample Labs", "source": "lever", "token": "samplelabs"}]}))
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    gh = (fixtures / "greenhouse_jobs.json").read_text()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, text=gh)
        return httpx.Response(503)

    monkeypatch.setattr(cli, "_sources_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    r = runner.invoke(cli.app, ["sources", "check", "--config-dir", str(d)])
    assert r.exit_code == 0, r.output
    n = len(__import__("json").loads(gh)["jobs"])
    assert f"Example AI (greenhouse:exampleai): ok {n} postings" in r.output
    assert "Sample Labs (lever:samplelabs): SOURCE_UNAVAILABLE" in r.output


def test_sources_check_covers_all_registered_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _config(tmp_path)
    prof = tmp_path / "storage" / "profiles"
    prof.mkdir(parents=True)
    boards = [
        {"name": "G", "source": "greenhouse", "token": "g"},
        {"name": "L", "source": "lever", "token": "l"},
        {"name": "A", "source": "ashby", "token": "a"},
        {"name": "S", "source": "smartrecruiters", "token": "s"},
        {"name": "W", "source": "workday", "token": "w",
         "extra": {"wd_host": "wd3", "site": "External"}}]
    (prof / "companies.yaml").write_text(yaml.safe_dump({"companies": boards}))
    monkeypatch.setattr(cli, "_sources_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    r = runner.invoke(cli.app, ["sources", "check", "--config-dir", str(d)])
    assert r.exit_code == 0, r.output
    for label in ("G (greenhouse:g)", "L (lever:l)", "A (ashby:a)", "S (smartrecruiters:s)",
                  "W (workday:w)"):
        assert f"{label}: SOURCE_UNAVAILABLE" in r.output


def test_bench_exits_when_ollama_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.httpx, "get", _down)
    r = runner.invoke(cli.app, ["bench", "--route", "a", "--config-dir", str(_config(tmp_path))])
    assert r.exit_code == 1 and "Start Ollama" in r.output


def test_bench_default_tags_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobpilot.bench import BenchResult

    monkeypatch.setattr(cli.httpx, "get", _tags("m2:big"))
    seen: dict[str, Any] = {}

    async def fake(cfg: Any, tags: list[str], route: str, fixtures: Path) -> list[BenchResult]:
        seen["tags"] = tags
        return [BenchResult(t, 1.0, 0.5, 0.5, 10, 20) for t in tags]

    import jobpilot.bench as b
    monkeypatch.setattr(b, "run_bench", fake)
    r = runner.invoke(cli.app, ["bench", "--route", "c", "--config-dir", str(_config(tmp_path))])
    assert r.exit_code == 0, r.output
    assert seen["tags"] == ["m2:big", "m3:tiny"]
    assert "m3:tiny" in r.output and str(tmp_path / "storage" / "bench") in r.output


def test_schedule_refuses_off_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _config(tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    for cmd in ("install", "uninstall", "status"):
        r = runner.invoke(cli.app, ["schedule", cmd] + (
            [] if cmd == "uninstall" else ["--config-dir", str(d)]))
        assert r.exit_code == 1 and "only available on macOS" in r.output


def test_discover_run_now_runs_cycle_then_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _config(tmp_path)
    seen: list[str] = []

    async def fake_now(cfg: Any) -> int:
        seen.append("cycle")
        return 3

    monkeypatch.setattr(cli, "_discover_now", fake_now)
    monkeypatch.setattr(cli, "_run_maintenance_if_available", lambda cfg: seen.append("maint"))
    r = runner.invoke(cli.app, ["discover", "--run-now", "--config-dir", str(d)])
    assert r.exit_code == 0, r.output
    assert seen == ["cycle", "maint"] and "3 tasks" in r.output


def test_schedule_install_passes_unresolved_uv_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _config(tmp_path)
    real = tmp_path / "Cellar" / "uv" / "0.1" / "bin"
    real.mkdir(parents=True)
    (real / "uv").write_text("")
    link = tmp_path / "bin" / "uv"
    link.parent.mkdir()
    link.symlink_to(real / "uv")
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(link))
    seen: dict[str, Any] = {}

    def fake_install(cfg: Any, **kw: Any) -> list[Path]:
        seen.update(kw)
        return []

    monkeypatch.setattr("jobpilot.ops.schedule.install", fake_install)
    r = runner.invoke(cli.app, ["schedule", "install", "--config-dir", str(d)])
    assert r.exit_code == 0, r.output
    assert seen["uv_path"] == str(link) != str(link.resolve())
    assert seen["config_dir"] == d.resolve()
