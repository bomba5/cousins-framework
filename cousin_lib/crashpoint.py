"""Named points where a test kills the process, to prove what a crash leaves (#286).

`crashpoint("name")` does nothing unless the environment names it:
`COUSIN_CRASH_AT=name` SIGKILLs the process at the point's first hit,
`name:n` at its nth (a point inside a loop). The variable is read once,
at import, so a point costs one comparison when no test asks.

A test runs a real process with the variable set, lets it die at the
point, then starts again and checks the recovery by reading the stores,
never log text. A kill proves atomicity between two steps; it says
nothing about a power loss (the page cache survives a SIGKILL).

Every point is registered in POINTS with what it sits between; with
the variable set, a call with a name not in POINTS raises, and
tests/test_crashpoint.py checks every call is registered and every
registered point is exercised by a test."""
import os
import signal

ENV = "COUSIN_CRASH_AT"

POINTS = {
    "job.registered": "cousin-job start: the row is registered, the command not yet forked",
    "job.exited": "a job's runner: the command exited, its row not yet closed",
    "job.artifacts_recorded": "a job's runner: the artifacts are recorded, the row not yet closed",
    "rollover.handed_off": "a rollover: the handoff is written, the old session not yet ended",
    "rollover.connected": "a rollover: the new session exists, the generation not yet moved",
    "rollover.bumped": "a rollover: the generation moved, the digest not yet queued",
    "rollover.digest_put": "a rollover: the digest row is put, the journal not yet told",
    "rollover.digest_queued": "a rollover: the digest is queued, the flip row not yet closed",
    "sdk.written": "the sdk lane: a row is written to the CLI, its echo not yet read",
    "runner.result_recorded": "a runner: the result naming a turn's rows is in the stream,"
                              " the rows not yet closed",
    "tmux.handled": "the tmux lane: a transcript line is handled, the cursor not yet saved",
    "peer.seen": "a peer message's id is recorded as seen, the message not yet delivered",
    "outbox.sent": "the outbox: a retry reached the peer, its row not yet finished",
    "schedule.delivered": "a due schedule is delivered, not yet marked fired",
}


def _target(value):
    name, _, count = str(value or "").partition(":")
    if not name:
        return None
    try:
        return name, max(1, int(count or 1))
    except ValueError:
        return name, 1


_TARGET = _target(os.environ.get(ENV))
_hits = {}


def _sigkill():
    os.kill(os.getpid(), signal.SIGKILL)


_kill = _sigkill


def arm(value, kill):
    """In-process, for a test whose process must outlive the point (the
    tmux lane: its pane outlives the runner, so the "process" that dies is
    the runner's thread): `value` as the variable would name it, `kill`
    called in place of the SIGKILL (raise SystemExit there, which no
    `except Exception` catches and a thread dies of silently; return
    False for "not this hit", and the next hit asks again). Returns the
    undo."""
    global _TARGET, _kill
    before = (_TARGET, _kill, dict(_hits))
    _TARGET, _kill = _target(value), kill
    _hits.clear()

    def undo():
        global _TARGET, _kill
        _TARGET, _kill = before[0], before[1]
        _hits.clear()
        _hits.update(before[2])
    return undo


def crashpoint(name):
    """SIGKILL this process here when the environment names this point. A
    pure no-op otherwise: a name is checked against POINTS only when a
    test asked for a point (tests/test_crashpoint.py checks every call),
    so a typo never costs a production path."""
    if _TARGET is None:
        return
    if name not in POINTS:
        raise ValueError("unregistered crash point %r (crashpoint.POINTS)" % name)
    if _TARGET[0] != name:
        return
    _hits[name] = _hits.get(name, 0) + 1
    if _hits[name] == _TARGET[1] and _kill() is False:
        _hits[name] -= 1
