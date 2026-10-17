"""Single-run lockfile so a scheduled run and `run --once` never interleave."""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class LockHeld(Exception):
    pass


@contextmanager
def run_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "run.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeld("another agentinbox run is in progress") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)


@contextmanager
def try_lock(path: Path):
    """Yield True if we got the lock (held until the block exits), else False."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except BlockingIOError:
            got = False
        yield got
    finally:
        os.close(fd)


def lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    with try_lock(path) as got:
        return not got
