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
import asyncio
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import boot
from cousin_lib.delivery import THREAD_KINDS, DeliveryError, thread_id
from cousin_lib.runner import wake
from cousin_lib.runner.base import RunnerError
from cousin_lib.runner.sdk import SdkRunner

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


# ---------------------------------------------------------- a side session

SIDE_HEAD = "side_session"   # the first event of every side session's stream


def _thread_pattern(kind):
    """The threads a side session answers: the bare kind, or `<kind>:*`."""
    try:
        return thread_id(kind)
    except DeliveryError:
        return kind + ":*"


def side_streams(home):
    """{kind: [stream paths, newest first]}: every file under data/stream/
    whose first event is `side_session`, grouped by the kind it names. A
    file with any other head (a primary's `runner`, a pre-1.14 stream) is
    never listed."""
    import json
    out = {}
    for path in (Path(home) / "data" / "stream").glob("*.jsonl"):
        try:
            with open(path, encoding="utf-8") as fh:
                head = json.loads(fh.readline() or "null")
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if isinstance(head, dict) and head.get("kind") == SIDE_HEAD:
            kind = (head.get("payload") or {}).get("kind") or "?"
            out.setdefault(kind, []).append((mtime, path))
    return {kind: [p for _, p in sorted(rows, reverse=True)] for kind, rows in out.items()}

class SideSession(SdkRunner):
    """One thread kind's session of its own. An SdkRunner that claims only
    its kind; polls the inbox instead of binding the home's wake socket
    (the primary holds it, and the loop claims every `poll_s` anyway);
    carries the side digest as its first turn's context instead of a row
    of its own (a `system` row would be the primary's to claim); never
    proposes (proposals ride the system thread); and never rolls over: at
    context pressure, or when the primary's generation moves, it RESETS
    (a final mine, a new session, the side digest again), with no handoff,
    no [session] hooks and no generation of its own."""

    # An interrupt row rides the `system` thread, the primary's (R2, R12): a
    # side session that took it would interrupt its own turn and close the
    # row delivered while the primary's task runs on (round 2 review N1).
    # SdkRunner._take_interrupts reads this flag (phase 5, ruling P5-2).
    takes_interrupts = False

    def __init__(self, home, *, kind, primary_activity=None, **kw):
        if kind not in THREAD_KINDS or kind in ALWAYS_PRIMARY:
            raise SessionsError("no side session for %r: one of %s" % (
                kind, ", ".join(k for k in THREAD_KINDS if k not in ALWAYS_PRIMARY)))
        super().__init__(home, session=kind, claim_kinds=(kind,), **kw)
        self.kind = kind
        self.primary_activity = primary_activity or (lambda: None)
        self._digest_due = False     # the next turn carries the side digest
        self._reset_due = None       # why the next boundary resets, or None
        self._generation = None      # the primary's generation this session belongs to
        # The stream's head (the P8-2 rule): a side session's stream starts
        # with `side_session`, never with the `runner` event that marks the
        # primary's, so a reader of "the runner's stream" never takes it.
        self.stream.append(SIDE_HEAD, {"kind": kind, "id": self.session_id,
                                       "thread": _thread_pattern(kind)})

    def _doorbell(self):
        return wake.Poller()

    async def _start_fresh(self, *, with_digest):
        self._generation = await asyncio.to_thread(boot.read_generation, self.home)
        self._digest_due = True
        self.stream.append("system", {"subtype": "fresh", "digest": "side",
                                      "session": self.kind})

    async def _propose(self, sid):
        return None


    def _request_rollover(self, why):
        """Pressure or PreCompact: a reset at the next boundary, never a
        `flip` row (that is the primary's generation)."""
        if self._reset_due is None:
            self._reset_due = why
        self.stream.append("rollover", {"phase": "requested", "reason": why,
                                        "session": self.kind})

    def rollover(self, reason):
        self._request_rollover(reason)
        return {"outcome": "queued", "session": self.kind, "reason": reason}

    def _side_digest(self):
        return side_digest(self.home, root=self.root, slug=self.tool_context.slug,
                           kind=self.kind, primary=self.primary_activity())

    def _session_generation(self):
        """The generation this session belongs to, not the home's current one
        (review M3): a reset that fell back to the old session keeps saying so,
        and the next start resets it."""
        if self._generation is not None:
            return self._generation
        return boot.read_generation(self.home)

    async def _reset_reason(self):
        now_gen = await asyncio.to_thread(boot.read_generation, self.home)
        if self._generation is None:     # a resumed session: its file says which
            saved = (await asyncio.to_thread(self._read_session_file)).get("generation")
            self._generation = saved if isinstance(saved, int) else now_gen
        if self._reset_due is not None:
            return self._reset_due
        if now_gen != self._generation:
            return "the primary session moved to generation %d" % now_gen
        return None

    async def _boundary(self, row):
        """Reset first when one is due. The claimed `row` is this session's
        to give back: whatever happens here (a stop, an unreadable
        generation file, a reset that raised, no session afterwards), it
        goes back to the queue and the loop runs no turn (review I1)."""
        try:
            why = await self._reset_reason()
            if why is None:
                return True
            if self._stop.is_set() or not await self._reset(why) or self._client is None:
                self.inbox.requeue(row["id"])
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - a claimed row is never stranded
            self.inbox.requeue(row["id"])
            self.stream.append("error", {"error": "side session %s, before its turn: %s: %s"
                                         % (self.kind, type(exc).__name__, exc),
                                         "session": self.kind, "requeued": [row["id"]]})
            return False

    async def _reset(self, why):
        """False when it did not run (the machine was not idle: a stop won
        the race). `_reset_due` is cleared only once the reset has run; a
        raise puts the machine back to idle and propagates to _boundary."""
        old = self._resume_id
        with self._lock:
            if self.machine.state != "idle":
                return False
            self.machine.to("rolling_over", why.splitlines()[0][:120])
        try:
            self.stream.append("rollover", {"phase": "start", "reason": why,
                                            "session": self.kind, "session_id": old})
            await self._disconnect()
            self._client = None
            await self._mine(old, final=True)   # nothing of the old session arrives after this
            self._resume_id = self._expect_session = None
            self._resume_lost, self._pending_save = False, None
            try:
                await asyncio.to_thread(self._save_session, None)
            except Exception as exc:  # noqa: BLE001 - the reset goes on; the file is a record
                self.stream.append("error", {"error": "runner-session file: %s: %s"
                                             % (type(exc).__name__, exc)})
            self.hysteresis.rolled_over()
        finally:
            with self._lock:
                if self.machine.state == "rolling_over":
                    self.machine.to("idle", "side session reset")
        self._reset_due = None
        if self._stop.is_set():
            return False                      # no new CLI during a shutdown
        if await self._connect(resume=None, why="side reset", fatal=False):
            await self._start_fresh(with_digest=True)
            self.stream.append("rollover", {"phase": "done", "reason": why,
                                            "session": self.kind, "old_session": old})
            return True
        if self._login_blocked:
            self._fresh_pending = True        # the login retry starts it fresh
            return True
        # the new session did not start: back on the old one, else give up
        # (Sessions restarts a side session that gave up, review I3)
        if old and await self._connect(resume=old, why="side reset failed", fatal=False):
            self._resume_id = self._pending_save = old
            await self._flush_session()       # with the generation it belongs to (M3)
            self.stream.append("rollover", {"phase": "failed", "reason": why,
                                            "session": self.kind, "old_session": old,
                                            "error": self._last_connect_error})
            return True
        self._fail_connect("side session %s has no session after a reset: %s"
                           % (self.kind, self._last_connect_error))
        return True

    async def _turn(self, first):
        if self._digest_due:
            digest = await asyncio.to_thread(self._side_digest)
            context = first.get("context") or ""
            first = dict(first, context=digest + ("\n\n" + context if context else ""))
        ok = await super()._turn(first)
        if ok:
            self._digest_due = False
        return ok
