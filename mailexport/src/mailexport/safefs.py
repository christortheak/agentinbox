"""Symlink-safe file operations inside the agent-writable output folder.

The exporter and the request worker run with Full Disk Access, and the agent
can write anything in <output>/ - including replacing a folder with a symlink
to somewhere else, or a file with a FIFO. So every write, read, listing and
delete under <output>/ goes through here:

- Each path component is opened with O_NOFOLLOW | O_DIRECTORY relative to its
  parent's descriptor, so a symlink anywhere in the chain is never followed,
  and there is no check-then-use race.
- A symlink (or file) where a folder belongs is removed - the link itself,
  never its target - and replaced with a real folder. Each repair is recorded
  in REPAIRS so the manifest can report it.
- Files are created O_EXCL | O_NOFOLLOW and renamed into place within the same
  directory descriptor. Reads accept regular files only and never block on a
  FIFO.
"""
from __future__ import annotations

import errno
import logging
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("safefs")

O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ROOTS: set[Path] = set()
REPAIRS: list[str] = []


class UnsafePath(Exception):
    pass


def register_root(root: Path) -> None:
    """Mark a folder as agent-writable: atomic_write() under it goes through
    the safe path automatically."""
    _ROOTS.add(Path(os.path.abspath(os.path.expanduser(str(root)))))


def root_for(path: Path) -> tuple[Path, Path] | None:
    p = Path(os.path.abspath(str(path)))
    for r in sorted(_ROOTS, key=lambda x: len(x.parts), reverse=True):
        try:
            return r, p.relative_to(r)
        except ValueError:
            continue
    return None


def _note(msg: str) -> None:
    if msg not in REPAIRS:
        REPAIRS.append(msg)
    log.warning("security: %s", msg)


def _check_name(name: str) -> None:
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        raise UnsafePath(f"bad path component {name!r}")


def _open_root(root: Path) -> int:
    try:
        return os.open(str(root), O_DIR)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(f"{root} is not a real folder") from None
        raise


def _subdir(parent_fd: int, name: str, *, create: bool, where: str) -> int | None:
    _check_name(name)
    try:
        return os.open(name, O_DIR, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
    except OSError as e:
        if e.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        if not create:
            return None          # treat a planted link/file as "not there"
        os.unlink(name, dir_fd=parent_fd)   # removes the link itself only
        _note(f"replaced a symlink or file at {where}/ with a real folder")
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    return os.open(name, O_DIR, dir_fd=parent_fd)


@contextmanager
def open_dir(root: Path, rel: Path | str = "", *, create: bool = True):
    """Yield an fd for root/rel (every component opened without following
    symlinks), or None if create=False and it doesn't exist."""
    fds: list[int] = []
    try:
        fd = _open_root(root)
        fds.append(fd)
        walked: list[str] = []
        for part in Path(rel).parts:
            if part in ("", "."):
                continue
            walked.append(part)
            nfd = _subdir(fd, part, create=create, where="/".join(walked))
            if nfd is None:
                fd = None
                break
            fds.append(nfd)
            fd = nfd
        yield fd
    finally:
        for f in fds:
            os.close(f)


def ensure_dir(root: Path, rel: Path | str, mode: int = 0o700) -> None:
    with open_dir(root, rel, create=True) as fd:
        os.fchmod(fd, mode)


def write_file(root: Path, rel: Path | str, data: bytes, mode: int = 0o600) -> None:
    rel = Path(rel)
    _check_name(rel.name)
    with open_dir(root, rel.parent, create=True) as dfd:
        tmp = f".tmp-{os.getpid()}-{secrets.token_hex(6)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=dfd)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # rename replaces the destination entry itself, even if it's a link
            os.rename(tmp, rel.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise


def _read_fd(fd: int, max_bytes: int) -> bytes | None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
        return None
    chunks, total = [], 0
    while True:
        b = os.read(fd, 1 << 16)
        if not b:
            break
        total += len(b)
        if total > max_bytes:
            return None
        chunks.append(b)
    return b"".join(chunks)


def read_at(dir_fd: int, name: str, max_bytes: int = 50_000_000) -> bytes | None:
    """Regular files only; a symlink, FIFO, folder or oversized file reads as None."""
    _check_name(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            return None
        raise
    try:
        return _read_fd(fd, max_bytes)
    finally:
        os.close(fd)


def read_file(root: Path, rel: Path | str, max_bytes: int = 50_000_000) -> bytes | None:
    rel = Path(rel)
    try:
        with open_dir(root, rel.parent, create=False) as dfd:
            if dfd is None:
                return None
            return read_at(dfd, rel.name, max_bytes)
    except (FileNotFoundError, UnsafePath):
        return None


def unlink_at(dir_fd: int, name: str) -> bool:
    """Remove a file or link entry (never a folder, never a link's target)."""
    _check_name(name)
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode):
        return False
    os.unlink(name, dir_fd=dir_fd)
    return True


def remove_file(root: Path, rel: Path | str) -> bool:
    rel = Path(rel)
    with open_dir(root, rel.parent, create=False) as dfd:
        if dfd is None:
            return False
        return unlink_at(dfd, rel.name)


def entries(dir_fd: int) -> list[os.DirEntry]:
    with os.scandir(dir_fd) as it:
        return list(it)
