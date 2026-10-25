"""Local backups (DB + profiles + artifacts) and verified, reversible restore.

Archives hold personal data: they live under storage/backups with mode 0600 (dirs 0700).
Restore needs the typed confirmation ``RESTORE``, refuses while the server runs, validates
every member and hash before touching current data, and rolls back if the result fails
``PRAGMA integrity_check``.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

from jobpilot.config import AppConfig
from jobpilot.data.db import upgrade_to_head, utcnow

log = logging.getLogger(__name__)

CONFIRM_WORD = "RESTORE"
FORMAT = 1
ARCHIVE_RE = re.compile(r"jobpilot-\d{8}-\d{6}\.tar\.gz")
TREES = ("profiles", "artifacts")
TOP_FILES = ("db.sqlite", "manifest.json")


class RestoreError(RuntimeError):
    pass


def _ts(now: Callable[[], datetime]) -> str:
    return now().strftime("%Y%m%d-%H%M%S")


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backups_dir(cfg: AppConfig) -> Path:
    return cfg.storage_path / "backups"


def pid_file(cfg: AppConfig) -> Path:
    return cfg.storage_path / "run" / "server.pid"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def server_running(cfg: AppConfig, *,
                   port_open: Callable[[str, int], bool] = _port_open) -> bool:
    """True when `jobpilot start`'s pid file names a live process, or the port answers."""
    pf = pid_file(cfg)
    try:
        if pf.exists() and _pid_alive(int(pf.read_text().strip())):
            return True
    except (ValueError, OverflowError):
        pass
    return port_open(cfg.bind_host, cfg.port)


def write_pid_file(cfg: AppConfig) -> Path:
    pf = pid_file(cfg)
    _private_dir(pf.parent)
    pf.write_text(str(os.getpid()))
    return pf


def remove_pid_file(cfg: AppConfig) -> None:
    pid_file(cfg).unlink(missing_ok=True)


def _alembic_revision(db: Path) -> str:
    with sqlite3.connect(db) as c:
        try:
            row = c.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return ""
    return row[0] if row else ""


def _copy_tree(src: Path, dest: Path) -> None:
    """Copy regular files and dirs only; symlinks are skipped with a warning."""
    _private_dir(dest)
    for entry in sorted(src.iterdir()):
        if entry.is_symlink():
            log.warning("backup: skipping symlink %s", entry)
        elif entry.is_dir():
            _copy_tree(entry, dest / entry.name)
        elif entry.is_file():
            shutil.copyfile(entry, dest / entry.name)
            (dest / entry.name).chmod(0o600)


def _rotate(bdir: Path, keep: int) -> None:
    archives = sorted(p for p in bdir.iterdir() if ARCHIVE_RE.fullmatch(p.name) and p.is_file())
    for old in archives[:-keep] if keep > 0 else archives:
        old.unlink()


def _tar_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mode = 0o700 if ti.isdir() else 0o600
    return ti


def backup(cfg: AppConfig, *, now: Callable[[], datetime] = utcnow,
           keep: int | None = None) -> Path:
    """Write storage/backups/jobpilot-<ts>.tar.gz atomically, then rotate."""
    keep = cfg.backup_keep if keep is None else keep
    bdir = _private_dir(backups_dir(cfg))
    tmp_root = _private_dir(bdir / ".tmp")
    ts = _ts(now)
    work = Path(tempfile.mkdtemp(prefix=f"{ts}-", dir=tmp_root))
    try:
        stage = _private_dir(work / "stage")
        db_copy = stage / "db.sqlite"
        src = sqlite3.connect(cfg.db_path)
        dst = sqlite3.connect(db_copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        db_copy.chmod(0o600)
        for tree in TREES:
            if (cfg.storage_path / tree).is_dir():
                _copy_tree(cfg.storage_path / tree, stage / tree)
        files = {p.relative_to(stage).as_posix(): _sha256(p)
                 for p in sorted(stage.rglob("*")) if p.is_file()}
        manifest = {"format": FORMAT, "created_utc": now().isoformat(),
                    "alembic_revision": _alembic_revision(db_copy), "files": files}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        (stage / "manifest.json").chmod(0o600)
        tmp_archive = work / "archive.tar.gz"
        with tarfile.open(tmp_archive, "w:gz") as tar:
            for name in sorted(os.listdir(stage)):
                tar.add(stage / name, arcname=name, filter=_tar_filter)
        tmp_archive.chmod(0o600)
        final = bdir / f"jobpilot-{ts}.tar.gz"
        os.replace(tmp_archive, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _rotate(bdir, keep)
    return final


def _safe_member(m: tarfile.TarInfo) -> bool:
    if not (m.isfile() or m.isdir()):
        return False
    p = PurePosixPath(m.name)
    if p.is_absolute() or ".." in p.parts or "\\" in m.name or not p.parts:
        return False
    if p.parts[0] in TREES:
        return True
    return len(p.parts) == 1 and m.isfile() and p.parts[0] in TOP_FILES


def _extract_verified(archive: Path, dest: Path) -> None:
    try:
        tar = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as e:
        raise RestoreError(f"cannot open archive: {e}") from e
    with tar:
        members = tar.getmembers()
        bad = [m.name for m in members if not _safe_member(m)]
        if bad:
            raise RestoreError(f"archive contains unsafe members: {bad[:5]}")
        tar.extractall(dest, members=members, filter="data")
    mpath = dest / "manifest.json"
    if not mpath.is_file() or not (dest / "db.sqlite").is_file():
        raise RestoreError("archive is missing manifest.json or db.sqlite")
    manifest = json.loads(mpath.read_text())
    if manifest.get("format") != FORMAT:
        raise RestoreError(f"unsupported backup format {manifest.get('format')!r}")
    listed: dict[str, str] = manifest.get("files", {})
    present = {p.relative_to(dest).as_posix() for p in dest.rglob("*")
               if p.is_file() or p.is_symlink()} - {"manifest.json"}
    if present != set(listed):
        raise RestoreError("archive files do not match the manifest")
    for rel, digest in listed.items():
        if _sha256(dest / rel) != digest:
            raise RestoreError(f"hash mismatch for {rel}")


def _integrity(db: Path) -> str:
    with sqlite3.connect(db) as c:
        return str(c.execute("PRAGMA integrity_check").fetchone()[0])


def restore(cfg: AppConfig, archive: Path, *, confirm: str,
            server_running: Callable[[AppConfig], bool] = server_running,
            now: Callable[[], datetime] = utcnow,
            integrity_check: Callable[[Path], str] = _integrity) -> Path:
    """Replace DB/profiles/artifacts with the archive's; returns the pre-restore dir."""
    if confirm != CONFIRM_WORD:
        raise RestoreError(f"type {CONFIRM_WORD} exactly to confirm a restore")
    if server_running(cfg):
        raise RestoreError("jobpilot start is running; stop it before restoring")
    tmp_root = _private_dir(backups_dir(cfg) / ".tmp")
    work = Path(tempfile.mkdtemp(prefix="restore-", dir=tmp_root))
    try:
        _extract_verified(archive, work)
        storage = cfg.storage_path
        pre = storage / f".pre-restore-{_ts(now)}"
        if pre.exists():
            raise RestoreError(f"{pre} already exists")
        _private_dir(pre)
        targets = [(cfg.db_path, work / "db.sqlite", pre / "jobpilot.db")]
        targets += [(storage / t, work / t, pre / t) for t in TREES]
        sidecars = [cfg.db_path.with_name(cfg.db_path.name + s) for s in ("-wal", "-shm")]
        moved: list[tuple[Path, Path]] = []
        placed: list[Path] = []
        try:
            for side in sidecars:
                if side.exists():
                    os.replace(side, pre / side.name)
                    moved.append((pre / side.name, side))
            for current, _new, saved in targets:
                if current.exists():
                    os.replace(current, saved)
                    moved.append((saved, current))
            for current, new, _saved in targets:
                if new.exists():
                    os.replace(new, current)
                    placed.append(current)
            upgrade_to_head(cfg.db_path)
            result = integrity_check(cfg.db_path)
            if result != "ok":
                raise RestoreError(f"restored database failed integrity check: {result}")
        except BaseException:
            for p in placed + sidecars:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
            for saved, current in reversed(moved):
                os.replace(saved, current)
            shutil.rmtree(pre, ignore_errors=True)
            raise
        return pre
    finally:
        shutil.rmtree(work, ignore_errors=True)
