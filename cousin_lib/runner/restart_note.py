"""The runner restarted: say so to the resumed session.

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
drops the mark, unless the cut turn had run tools (tool_ledger): then
it gets `fresh_body` and the list, since its message comes again and a
push or a send must not run twice.

A stop the operator asked for (console, cousin-supervisor stop) writes
run/held BEFORE it signals the runner, so a stop that finds the hold marks
it as held, and the line then says who stopped the turn. Only an unheld stop
or a death is told "not the operator". A restart that was asked for holds
under a name ending in "restart" (the console's is REQUESTED_RESTART_BY):
its line says a requested restart cut the turn, and to continue it."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

FILE = ("data", "runner-restart.json")
SOURCE = "boot"             # the framework's start-up line (thread `system`, sender `runner`)
MARKER = "[runner] the runner restarted"
HELD_MARKER = "[runner] a requested stop cut your last turn"
RESTART_HELD_MARKER = "[runner] a requested restart cut your last turn"
REQUESTED_RESTART_BY = "console restart"   # the hold the console's restart writes


def _path(home):
    return Path(home).joinpath(*FILE)


def held_by(home):
    """The hold a requested stop wrote (`<ISO time> <who>`), or None. A hold
    that cannot be read still counts: a stop was asked for."""
    from cousin_lib import supervisor     # lazy: supervisor imports runner.main
    path = supervisor.held_path(home)
    if not path.exists():
        return None
    try:
        return path.read_text().strip() or "a stop request"
    except OSError:
        return "a stop request"


def mark(home, why, *, held=None, requeued=False):
    """Record that the turn in flight was cut by a stop or a death (`why`);
    `held` is the hold of a requested stop (held_by); `requeued`: the
    start's sweep put the cut turn's claimed rows back in the queue (a
    death), so its message is delivered again."""
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    note = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "why": str(why)}
    if held:
        note["held"] = str(held)
    if requeued:
        note["requeued"] = True
    tmp.write_text(json.dumps(note))
    os.replace(tmp, path)


def read(home):
    """The mark ({"at", "why"[, "held"]}), left in place; None when there is
    none. A mark that cannot be parsed still counts (it only says a turn was
    cut). The caller puts its line, then clear()s: a crash between the two
    repeats the line, it never loses it."""
    path = _path(home)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        note = json.loads(text)
    except ValueError:
        note = None
    return note if isinstance(note, dict) else {"at": "unknown", "why": "unreadable mark"}


def clear(home):
    _path(home).unlink(missing_ok=True)


def requested_restart(held):
    """The hold (`<ISO time> <who>`) is a restart's: its who ends in
    "restart". An unreadable hold is a stop's."""
    who = str(held).strip().split(" ", 1)[-1]
    return who.endswith("restart")


FRESH_MARKER = "[runner] a restart cut your last turn before this session began"


def fresh_body(note):
    """The line a FRESH session gets when the cut turn had run tools
    (tool_ledger): it has nothing of that turn in its transcript."""
    return (FRESH_MARKER + " (at %s: %s). This is a new session, so you cannot see that"
            " turn." % (note.get("at", "unknown"), note.get("held") or note.get("why", "a restart")))


def body(note):
    """The line the resumed session gets."""
    if note.get("held") and requested_restart(note["held"]):
        return (RESTART_HELD_MARKER + " (at %s: %s). A restart interrupts the turn in flight,"
                " and your agent CLI records that as \"[Request interrupted by user]\": that was"
                " the restart that was asked for, not a request to stop. Nothing was lost:"
                " continue where you were." % (note.get("at", "unknown"), note["held"]))
    if note.get("held"):
        return (HELD_MARKER + " (at %s: %s). Your agent CLI records that stop as"
                " \"[Request interrupted by user]\"; it was the stop that was asked for."
                % (note.get("at", "unknown"), note["held"]))
    return (MARKER + " (at %s: %s). A restart interrupts the turn in"
            " flight, and your agent CLI records that as \"[Request interrupted by user]\" or"
            " \"stop what you are doing and wait for the user\": that was the restart, not the"
            " operator, and nobody asked you to stop. Nothing was lost: continue where you were."
            % (note.get("at", "unknown"), note.get("why", "a restart")))
