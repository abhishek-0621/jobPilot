"""Advisory, non-blocking process lock for operations that must not overlap.

Restore, backup, ``discover --run-now`` and the maintenance hook take ``storage/run/ops.lock``
with ``fcntl.flock``. The lock is re-entrant within one process, so a hook that itself calls
``backup`` does not deadlock against its own caller.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from jobpilot.config import AppConfig

_held: dict[Path, int] = {}


class LockHeld(RuntimeError):
    pass


def lock_path(cfg: AppConfig) -> Path:
    return cfg.storage_path / "run" / "ops.lock"


@contextmanager
def ops_lock(cfg: AppConfig) -> Iterator[None]:
    """Hold the exclusive ops lock; raises ``LockHeld`` if another process has it."""
    path = lock_path(cfg).absolute()
    if _held.get(path, 0) > 0:
        _held[path] += 1
        try:
            yield
        finally:
            _held[path] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise LockHeld(
                "another JobPilot operation (restore, backup or discovery run) is in "
                f"progress; lock file: {path}. Try again when it finishes.") from e
        _held[path] = 1
        try:
            yield
        finally:
            del _held[path]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
