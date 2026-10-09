"""One memory writer at a time per cousin home.

Two sessions of one cousin (a primary and its side sessions,
threads of one runner process) and the cousin's own `cousin-memory`
commands (another process) write the same files: the raw day file, the
decisions log and its rotation, the recall counts, the distilled views,
the extraction cursors. Most of those writes are read-modify-write, and
an unserialized pair loses one of them.

`write_lock(home)` is an flock on `<home>/data/.memory-write.lock`,
opened afresh by every holder: flock excludes two open file
descriptions even inside one process, so it serializes threads and
processes alike, and the kernel drops it when a holder dies. It lives
under data/, not memory/, because a transplant copies memory/.

It is reentrant within one thread (a decision's raw bridge appends raw
under the decision's own lock); a second thread blocks. Holders keep it
for file work only, never across a model call or a network wait.
"""
import fcntl
import os
import threading
from contextlib import contextmanager
from pathlib import Path

LOCK_PARTS = ("data", ".memory-write.lock")

_held = threading.local()


def lock_path(home):
    return Path(home).joinpath(*LOCK_PARTS)


@contextmanager
def write_lock(home):
    """Hold the home's memory write lock for the body of the `with`."""
    key = os.path.realpath(str(home))
    depth = getattr(_held, "depth", None)
    if depth is None:
        depth = _held.depth = {}
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    path = lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # read-only and 0644: flock needs no write access, so a lock file first
    # created by another uid (the operator's shell, a container user) never
    # shuts the cousin out of its own memory
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        depth[key] = 1
        try:
            yield
        finally:
            depth.pop(key, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def try_write_lock(home):
    """write_lock without waiting: yields True holding it (reentrant, as
    write_lock), False when another holder has it, for a reader that
    would rather leave something alone than wait on a writer."""
    key = os.path.realpath(str(home))
    depth = getattr(_held, "depth", None)
    if depth is None:
        depth = _held.depth = {}
    if depth.get(key):
        with write_lock(home):
            yield True
        return
    path = lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        depth[key] = 1
        try:
            yield True
        finally:
            depth.pop(key, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
