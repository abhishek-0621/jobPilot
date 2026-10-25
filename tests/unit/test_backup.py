import io
import json
import sqlite3
import tarfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobpilot.config import AppConfig
from jobpilot.data.db import upgrade_to_head
from jobpilot.ops import backup as bk


def _cfg(tmp_path: Path) -> AppConfig:
    storage = tmp_path / "storage"
    storage.mkdir()
    return AppConfig(storage_path=storage, db_path=storage / "jobpilot.db", model_routes={})


def _seed(cfg: AppConfig) -> None:
    upgrade_to_head(cfg.db_path)
    with sqlite3.connect(cfg.db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS t_extra (id INTEGER PRIMARY KEY, v TEXT)")
    (cfg.storage_path / "profiles").mkdir()
    (cfg.storage_path / "profiles" / "profile.yaml").write_text("name: Test\n")
    (cfg.storage_path / "artifacts" / "a1").mkdir(parents=True)
    (cfg.storage_path / "artifacts" / "a1" / "shot.png").write_bytes(b"png")


def _not_running(_cfg: AppConfig) -> bool:
    return False


def _at(i: int) -> datetime:
    return datetime(2026, 10, 25, 8, 0, 0, tzinfo=UTC) + timedelta(seconds=i)


def test_backup_consistent_during_writes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg)
    stop = threading.Event()

    def writer() -> None:
        with sqlite3.connect(cfg.db_path, timeout=10) as c:
            i = 0
            while not stop.is_set():
                c.execute("INSERT INTO t_extra (v) VALUES (?)", (f"r{i}",))
                c.commit()
                i += 1

    th = threading.Thread(target=writer)
    th.start()
    try:
        archive = bk.backup(cfg, now=lambda: _at(0))
    finally:
        stop.set()
        th.join()
    bk.restore(cfg, archive, confirm="RESTORE", server_running=_not_running, now=lambda: _at(1))
    with sqlite3.connect(cfg.db_path) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (cfg.storage_path / "profiles" / "profile.yaml").read_text() == "name: Test\n"
    assert (cfg.storage_path / "artifacts" / "a1" / "shot.png").read_bytes() == b"png"
    assert (cfg.storage_path / ".pre-restore-20261025-080001" / "jobpilot.db").exists()


def _rewrite(archive: Path, out: Path, mutate) -> Path:  # type: ignore[no-untyped-def]
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None  # type: ignore[union-attr]
            m, data = mutate(m, data)
            dst.addfile(m, io.BytesIO(data) if data is not None else None)
    return out


def test_restore_refuses_bad_hash_and_running_server(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = bk.backup(cfg, now=lambda: _at(0))
    with pytest.raises(bk.RestoreError, match="RESTORE"):
        bk.restore(cfg, archive, confirm="restore", server_running=_not_running)
    with pytest.raises(bk.RestoreError, match="running"):
        bk.restore(cfg, archive, confirm="RESTORE", server_running=lambda _c: True)

    def tamper(m: tarfile.TarInfo, data: bytes | None) -> tuple[tarfile.TarInfo, bytes | None]:
        if m.name == "profiles/profile.yaml":
            data = b"name: Evil\n"
            m.size = len(data)
        return m, data

    bad = _rewrite(archive, tmp_path / "bad.tar.gz", tamper)
    with pytest.raises(bk.RestoreError, match="hash"):
        bk.restore(cfg, bad, confirm="RESTORE", server_running=_not_running)
    assert (cfg.storage_path / "profiles" / "profile.yaml").read_text() == "name: Test\n"
    assert not list(cfg.storage_path.glob(".pre-restore-*"))


@pytest.mark.parametrize("name", ["../evil.txt", "/abs/evil.txt", "profiles/../../evil.txt",
                                  "other/evil.txt"])
def test_restore_rejects_path_traversal(tmp_path: Path, name: str) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = bk.backup(cfg, now=lambda: _at(0))

    def add_evil(m: tarfile.TarInfo, data: bytes | None) -> tuple[tarfile.TarInfo, bytes | None]:
        if m.name == "profiles/profile.yaml":
            m.name = name
        return m, data

    bad = _rewrite(archive, tmp_path / "bad.tar.gz", add_evil)
    with pytest.raises(bk.RestoreError, match="unsafe"):
        bk.restore(cfg, bad, confirm="RESTORE", server_running=_not_running)
    assert not (tmp_path / "evil.txt").exists()


def test_restore_rejects_symlink_member(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = bk.backup(cfg, now=lambda: _at(0))

    def link(m: tarfile.TarInfo, data: bytes | None) -> tuple[tarfile.TarInfo, bytes | None]:
        if m.name == "profiles/profile.yaml":
            m.type, m.linkname, m.size = tarfile.SYMTYPE, "/etc/passwd", 0
            return m, None
        return m, data

    bad = _rewrite(archive, tmp_path / "bad.tar.gz", link)
    with pytest.raises(bk.RestoreError, match="unsafe"):
        bk.restore(cfg, bad, confirm="RESTORE", server_running=_not_running)


def test_rotation_keeps_n(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg)
    other = cfg.storage_path / "backups" / "notes.tar.gz"
    for i in range(5):
        bk.backup(cfg, now=lambda i=i: _at(i), keep=3)
        if i == 0:
            other.write_text("keep me")
    names = sorted(p.name for p in (cfg.storage_path / "backups").glob("jobpilot-*.tar.gz"))
    assert names == ["jobpilot-20261025-080002.tar.gz", "jobpilot-20261025-080003.tar.gz",
                     "jobpilot-20261025-080004.tar.gz"]
    assert other.exists()
    assert not list((cfg.storage_path / "backups" / ".tmp").iterdir())


def test_backup_file_modes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = bk.backup(cfg, now=lambda: _at(0))
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.parent.stat().st_mode & 0o777 == 0o700
    with tarfile.open(archive) as t:
        manifest = json.loads(t.extractfile("manifest.json").read())  # type: ignore[union-attr]
        for m in t.getmembers():
            assert m.mode & 0o077 == 0
    assert manifest["format"] == 1 and manifest["alembic_revision"]
    assert set(manifest["files"]) == {"db.sqlite", "profiles/profile.yaml",
                                      "artifacts/a1/shot.png"}


def test_backup_excludes_browser_state(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg)
    (cfg.browser_state_dir).mkdir()
    (cfg.browser_state_dir / "cookies.json").write_text("secret")
    (cfg.storage_path / "profiles" / "link.yaml").symlink_to(tmp_path / "outside.yaml")
    archive = bk.backup(cfg, now=lambda: _at(0))
    with tarfile.open(archive) as t:
        names = t.getnames()
    assert not any("browser_state" in n or "cookies" in n for n in names)
    assert "profiles/link.yaml" not in names
    assert "symlink" in caplog.text


def test_restore_rollback_on_integrity_failure(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = bk.backup(cfg, now=lambda: _at(0))
    (cfg.storage_path / "profiles" / "profile.yaml").write_text("name: Current\n")
    with pytest.raises(bk.RestoreError, match="integrity"):
        bk.restore(cfg, archive, confirm="RESTORE", server_running=_not_running,
                   integrity_check=lambda _p: "corrupt")
    assert (cfg.storage_path / "profiles" / "profile.yaml").read_text() == "name: Current\n"
    assert not list(cfg.storage_path.glob(".pre-restore-*"))


def test_server_running_pid_and_port(tmp_path: Path) -> None:
    import os
    cfg = _cfg(tmp_path)
    assert bk.server_running(cfg, port_open=lambda _h, _p: False) is False
    run = cfg.storage_path / "run"
    run.mkdir()
    (run / "server.pid").write_text(str(os.getpid()))
    assert bk.server_running(cfg, port_open=lambda _h, _p: False) is True
    (run / "server.pid").write_text("999999999")
    assert bk.server_running(cfg, port_open=lambda _h, _p: False) is False
    assert bk.server_running(cfg, port_open=lambda _h, _p: True) is True
