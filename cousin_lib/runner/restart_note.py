"""The runner restarted: say so to the resumed session (#98).

A runner stop interrupts the turn in flight, and the agent CLI records that
interruption in the session's transcript as the user's ("[Request
interrupted by user]", "stop what you are doing and wait for the user"). A
cousin whose session is resumed after a restart read that as its operator's
stop and parked. So a runner that stopped with a turn in flight (or one
that died holding a claimed row, found by the next start's sweep) leaves a
mark in the home, and the next runner that RESUMES the session takes it and
puts one line first on the system thread: the runner restarted, that was
not the operator, continue where you were. A fresh session has nothing
interrupted in it (the digest carries the state), so a fresh start only
drops the mark."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

FILE = ("data", "runner-restart.json")
SOURCE = "boot"             # the framework's start-up line (thread `system`, sender `runner`)
MARKER = "[runner] the runner restarted"


def _path(home):
    return Path(home).joinpath(*FILE)


def mark(home, why):
    """Record that the turn in flight was cut by a stop or a death (`why`)."""
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "why": str(why)}))
    os.replace(tmp, path)


def take(home):
    """The mark ({"at", "why"}), removed; None when there is none. A mark
    that cannot be read still counts (it only says a turn was cut)."""
    path = _path(home)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        text = ""
    path.unlink(missing_ok=True)
    try:
        note = json.loads(text)
    except ValueError:
        note = None
    return note if isinstance(note, dict) else {"at": "unknown", "why": "unreadable mark"}


def body(note):
    """The line the resumed session gets."""
    return (MARKER + " (at %s: %s). A restart interrupts the turn in"
            " flight, and your agent CLI records that as \"[Request interrupted by user]\" or"
            " \"stop what you are doing and wait for the user\": that was the restart, not the"
            " operator, and nobody asked you to stop. Nothing was lost: continue where you were."
            % (note.get("at", "unknown"), note.get("why", "a restart")))
