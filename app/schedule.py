"""Date window and single-run lock for the scheduled job."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from .exceptions import Locked


def report_window(
    today: dt.date,
    days: int = 3,
    last_synced_end: dt.date | None = None,
    max_days: int = 31,
) -> tuple[dt.date, dt.date]:
    """The last ``days`` full days (today is excluded: its numbers are still moving).

    If the newest day that reached Power BI is older than that window (a missed or
    failed run), start the day after it instead, so no day is skipped; never reach
    back more than ``max_days``.
    """
    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    if last_synced_end is not None and last_synced_end + dt.timedelta(days=1) < start:
        start = max(last_synced_end + dt.timedelta(days=1), end - dt.timedelta(days=max_days - 1))
    return start, end


class AlreadyRunning(Locked):
    """Another scheduled run holds the lock file."""


class RunLock:
    """An OS-level exclusive lock on a file; released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(f"another run holds {self.path}") from None
        self._handle = handle
        return self

    def __exit__(self, *exc) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
