"""Write-only secrets for the console (a pasted key, a bot token, an
account credential): the one writer and the one "what may be shown"
reader a route uses. The value is never echoed, logged or returned; the
answer is `{"set": bool, "last4": str | None, "error": str | None}` and
nothing more. The browser side is ui.jsx's SecretField, which posts the
value once and clears its box before the request goes out.

One writer, one reader: the file is written by the accounts' private
writer (accounts._write_private_text: 0600 from the first byte, O_EXCL and
O_NOFOLLOW on the tmp, atomic) and only in the shape the runner's reader
takes (agent_auth.read_private_file, accounts._read_secret: one line of
printable non-space characters, at most agent_auth.KEY_MAX_CHARS, in a 0700
directory of ours). What that reader would refuse at the next start is
refused here, before anything is written.

Kept out of _common.py on purpose (phase 11 edits that file)."""
from __future__ import annotations

import os
from pathlib import Path

from cousin_lib import accounts, agent_auth

LAST4_MIN_LEN = 16    # below this, four characters say too much of the value


def _under(path, base):
    return path == base or path.startswith(base + os.sep)


def _check_shape(value):
    if not isinstance(value, str):
        raise ValueError("the secret must be a string")
    text = value.strip()
    if not text or "\n" in text or "\r" in text:
        raise ValueError("the secret must be one non-empty line")
    if len(text) > agent_auth.KEY_MAX_CHARS:
        raise ValueError("the secret is longer than %d characters" % agent_auth.KEY_MAX_CHARS)
    if not agent_auth._KEY_RE.match(text):
        raise ValueError("the secret must be printable characters with no spaces")
    return text


def write_secret_file(path, value, *, within):
    """Write `value` (stripped, one line) to `path`, which must stay under
    `within` once every symlink is resolved: a symlinked directory inside
    `within` that points elsewhere is refused, before and after the parent
    is created. The parent is created 0700 and tightened to 0700; the
    written file is read back through the runner's own reader, and removed
    again if that refuses it. Returns secret_state(path)."""
    text = _check_shape(value)
    base = os.path.realpath(within)
    full = os.path.abspath(path)
    parent = os.path.dirname(full)
    if not _under(os.path.realpath(parent), base):
        raise ValueError("the secret file must be under %s" % base)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if not _under(os.path.realpath(parent), base):      # a link swapped in meanwhile
        raise ValueError("the secret file must be under %s" % base)
    os.chmod(parent, 0o700)
    accounts._write_private_text(Path(full), text + "\n")
    try:
        agent_auth.read_private_file(full, what="secret file")
    except agent_auth.AuthError as err:
        try:
            os.unlink(full)
        except OSError:
            pass
        raise ValueError(str(err))
    return secret_state(full)


def secret_state(path):
    """What may be shown about a secret file, read as the runner reads it
    (no symlink followed, a directory and file of ours with no group or
    other bits): set or not, the last four characters when the value is
    long enough for that to reveal nothing useful, and why it is unusable.
    Never the value."""
    try:
        raw = agent_auth.read_private_file(path, what="secret file")
    except agent_auth.MissingFile:
        return {"set": False, "last4": None, "error": None}
    except agent_auth.AuthError as err:
        return {"set": False, "last4": None, "error": str(err)}
    value = raw.decode("utf-8", "replace").strip()
    if not value:
        return {"set": False, "last4": None, "error": None}
    return {"set": True, "last4": value[-4:] if len(value) >= LAST4_MIN_LEN else None,
            "error": None}
