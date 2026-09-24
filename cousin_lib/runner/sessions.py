"""Side sessions (spec, "Threads"; master plan phase 8).

The runner ships primary-only: every thread runs in the one session. A
cousin turns side sessions on in cousin.toml:

    [agent.sessions]
    peer = "own"        # peer chat gets a session of its own
    meeting = "primary" # the default for every kind not named

A kind mapped to "own" gets ONE side session for all its threads: the
same system prompt, tools and working directory as the primary (so the
cached prefix is shared, phase 4 R1), the same memory, its own context.
A side session merges with the primary through memory, never through
context.

`operator` and `system` are the primary's and cannot be "own": the
generation's work arrives on operator threads, and the system thread
carries the rollover, the digest and the memory proposals, which belong
to the generation.
"""
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib.delivery import THREAD_KINDS
from cousin_lib.runner.base import RunnerError

PRIMARY, OWN = "primary", "own"
ALWAYS_PRIMARY = ("operator", "system")
ACTIVITY_CHARS = 300


class SessionsError(RunnerError):
    """A bad [agent.sessions] table: cousin-runner exits 2 naming it."""


def load_map(home):
    """{kind: "primary" | "own"} for every thread kind, from cousin.toml
    [agent.sessions]; a kind not named is "primary"."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise SessionsError("cannot read %s/cousin.toml: %s" % (home, err))
    table = (data.get("agent") or {}).get("sessions") or {}
    if not isinstance(table, dict):
        raise SessionsError("[agent.sessions] must be a table of thread kinds")
    out = {kind: PRIMARY for kind in THREAD_KINDS}
    for kind, value in table.items():
        if kind not in THREAD_KINDS:
            raise SessionsError("[agent.sessions] %r is not a thread kind (one of %s)"
                                % (kind, ", ".join(THREAD_KINDS)))
        if value not in (PRIMARY, OWN):
            raise SessionsError("[agent.sessions] %s must be %r or %r, got %r"
                                % (kind, PRIMARY, OWN, value))
        if value == OWN and kind in ALWAYS_PRIMARY:
            raise SessionsError("[agent.sessions] %s cannot be %r: %s threads belong to the"
                                " primary session, which holds the generation"
                                % (kind, OWN, kind))
        out[kind] = value
    return out


def side_kinds(home):
    """The kinds with a session of their own, in THREAD_KINDS order."""
    mapping = load_map(home)
    return tuple(kind for kind in THREAD_KINDS if mapping[kind] == OWN)


def _clock(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M UTC")


def describe_primary(activity, now=None):
    """One line for the primary's `activity()`: its state, and when a turn
    is live, the kinds of threads it answers and since when. Never a key,
    a sender or a body: the dict carries none."""
    if not activity:
        return "not reported"
    state = activity.get("state") or "unknown"
    kinds, since = activity.get("thread_kinds") or [], activity.get("since")
    if not kinds:
        return state
    line = "%s, in a turn on %s threads" % (state, ", ".join(kinds))
    if since:
        now = time.time() if now is None else now
        line += " since %s (%d min)" % (_clock(since), max(0, int((now - since) // 60)))
    return line


def last_activity(home):
    """The cousin's own last activity note (the memory tool's `activity`,
    data/last-activity.txt), first line, bounded; or "none recorded"."""
    try:
        text = (Path(home) / "data" / "last-activity.txt").read_text(errors="replace")
    except OSError:
        return "none recorded"
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line[:ACTIVITY_CHARS] or "none recorded"


def side_digest(home, *, root, slug, kind, primary=None, now=None):
    """The context a side session's first turn carries: which session it
    is, what the primary is doing (describe_primary), the cousin's last
    activity note, then the generation's state digest. Never raises: a
    digest that cannot be built is said in its place."""
    from cousin_lib.runner import prompt
    head = [
        "SIDE SESSION: %s threads of %s" % (kind, slug),
        "You are one of %s's sessions; this one answers %s threads only. The primary"
        " session holds the generation (the operator's work, STATUS.md, the handoff)."
        " You share its memory, not its context: what it should know, record with the"
        " memory tool (remember, decide). Do not call handoff." % (slug, kind),
        "Primary session: %s" % describe_primary(primary, now),
        "Last recorded activity: %s" % last_activity(home),
    ]
    try:
        digest = prompt.state_digest(home, root=root, slug=slug)["text"]
    except Exception as exc:  # noqa: BLE001 - the side session starts all the same
        digest = "STATE DIGEST unavailable: %s: %s\n" % (type(exc).__name__, exc)
    return "\n".join(head) + "\n\n" + digest
