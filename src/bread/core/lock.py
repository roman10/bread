"""Per-database advisory file lock — prevents two `bread run` processes from
sharing the same SQLite file (and thus submitting duplicate orders to Alpaca).

The lock file lives next to the DB (e.g. `data/bread-paper.db.lock`). We hold
an exclusive `fcntl.flock` for the process lifetime; the kernel releases it
automatically if the process dies, so stale-lock cleanup is not needed.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class DatabaseLockedError(RuntimeError):
    """Raised when another process already holds the DB lock."""


class DatabaseLock:
    """Exclusive flock on a sidecar lock file. Held until the process exits."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock_path = db_path.with_name(db_path.name + ".lock")
        self._fd: int | None = None

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            try:
                holder = self._lock_path.read_text().strip() or "unknown"
            except OSError:
                holder = "unknown"
            raise DatabaseLockedError(
                f"another bread process is using {self._db_path}"
                f" (pid {holder}); refusing to start a duplicate"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        logger.debug("Acquired DB lock at %s", self._lock_path)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass
