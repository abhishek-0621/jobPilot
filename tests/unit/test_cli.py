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
