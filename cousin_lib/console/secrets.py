"""Write-only secrets for the console (a pasted key, a bot token, an
account credential): the one writer and the one "what may be shown"
reader a route uses. The value is never echoed, logged or returned; the
answer is `{"set": bool, "last4": str | None}` and nothing more. The
browser side is ui.jsx's SecretField, which posts the value once and
clears its box before the request goes out.

Kept out of _common.py on purpose (phase 11 edits that file)."""
from __future__ import annotations

import os
from pathlib import Path

LAST4_MIN_LEN = 16    # below this, four characters say too much of the value


def _contained(path, within):
    base = os.path.normpath(os.path.abspath(within))
    full = os.path.normpath(os.path.abspath(path))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("the secret file must be under %s" % base)


def write_secret_file(path, value, *, within=None):
    """Write `value` to `path` atomically, 0600 from the first byte (the
    tmp is opened through os.open with O_EXCL and O_NOFOLLOW, the mode
    set on the descriptor whatever the umask), in a parent created 0700.
    A stale tmp is removed first, so neither a leftover readable tmp nor
    a symlink planted at the tmp path can carry the value anywhere; a
    failed write leaves the old file and no tmp. `within`: refuse a path
    outside that directory. Returns secret_state(path)."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("the secret must be a non-empty string")
    path = Path(path)
    if within is not None:
        _contained(path, within)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(".%s.tmp" % path.name)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fd = None
            fh.write(value)
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return secret_state(path)


def secret_state(path):
    """What may be shown about a secret file: whether it is set, and its
    last four characters when the value is long enough for that to
    reveal nothing useful. Never the value."""
    try:
        value = Path(path).read_text(errors="replace").strip()
    except OSError:
        return {"set": False, "last4": None}
    if not value:
        return {"set": False, "last4": None}
    return {"set": True, "last4": value[-4:] if len(value) >= LAST4_MIN_LEN else None}
