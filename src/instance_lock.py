"""
Single-instance lock
====================
An exclusive OS file lock held for the life of the trading process. Unlike a pid
file (check, then write) it has no race: two engines started in the same second
cannot both acquire it, and the OS releases it if the process dies.

On 2026-10-29 the pid-file guard let two or three engines run at once on the live
account; each saw the same signal and sent its own order about a second apart.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO, Optional


def acquire_instance_lock(path: Path) -> Optional[IO]:
    """Return an open handle that holds the lock, or None if another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def is_instance_locked(path: Path) -> bool:
    """True when some process currently holds the lock (used by the dashboard)."""
    handle = acquire_instance_lock(path)
    if handle is None:
        return True
    release_instance_lock(handle)
    return False


def release_instance_lock(handle: Optional[IO]) -> None:
    if handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()
