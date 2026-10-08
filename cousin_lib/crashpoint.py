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
    if _hits[name] == _TARGET[1]:
        os.kill(os.getpid(), signal.SIGKILL)
