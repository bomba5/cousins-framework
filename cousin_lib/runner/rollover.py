"""Rollover (spec, "Continuous extraction and rollover").

A generation ends on context pressure, or at the operator's daily
cadence (`max_age`: the loops daemon's `flip_at` today, later the
supervisor's clock). Either way it is ONE inbox row with
source `flip`: priority 0, claimed only at a turn boundary, never folded
into a live turn, durable (a runner that dies mid-rollover finishes it,
the journal below keeping a written handoff from being asked twice
at its next start), and coalesced (one pending per home). The handoff
is one awaited, structured tool call (tools.handoff); past the deadline
the runner writes an emergency handoff from the session store's tail,
marked degraded, and the generation still ends."""
import asyncio
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import atomic
from cousin_lib.delivery import Item
from cousin_lib.runner import wake

HANDOFF_DEADLINE_S = 300.0      # flip.HANDOFF_DEADLINE_SECONDS
WAIT_SLACK_S = 60.0
ROLLOVER_AT_PERCENT = 80.0
COMPACT_MARGIN_TOKENS = 10_000  # roll over this far before the CLI would compact
HYSTERESIS_POINTS = 10.0
HYSTERESIS_TURNS = 5
BEQUEST_CHARS = 120             # a reason longer than this, or multi-line, is a bequest


def is_bequest(reason):
    """A bequest is never coalesced away: the same test
    handoff_request_text uses to quote a reason whole."""
    reason = str(reason or "")
    return "\n" in reason.strip() or len(reason.strip()) > BEQUEST_CHARS


def threshold(context_usage, percent=ROLLOVER_AT_PERCENT):
    """The PERCENTAGE at which pressure is due (and from which hysteresis
    counts back): `percent`."""
    return float(percent)


def pressure_due(context_usage, percent=ROLLOVER_AT_PERCENT):
    """Due at `percent`, or earlier when the CLI reports its own
    autocompact threshold: totalTokens compared with it directly, a
    margin of COMPACT_MARGIN_TOKENS before it."""
    u = context_usage or {}
    try:
        if float(u["percentage"]) >= threshold(u, percent):
            return True
    except (KeyError, TypeError, ValueError):
        pass
    try:
        auto, total = u.get("autoCompactThreshold"), u.get("totalTokens")
        return bool(auto) and total is not None and \
            float(total) >= float(auto) - COMPACT_MARGIN_TOKENS
    except (TypeError, ValueError):
        return False


class Hysteresis:
    """After a rollover, pressure may not fire again until the percentage
    drops below the threshold minus HYSTERESIS_POINTS, or HYSTERESIS_TURNS
    turns have passed, whichever first. Without it a session that starts
    at 91% (a digest can be large) would roll over after every turn. It
    lives in memory: a restarted runner starts armed, which costs at most
    one early rollover and is harmless."""

    def __init__(self):
        self.armed = True
        self.turns = 0

    def rolled_over(self):
        self.armed, self.turns = False, 0

    def allow(self, context_usage, percent=ROLLOVER_AT_PERCENT):
        """Call once per completed turn. True when pressure may fire."""
        if self.armed:
            return True
        self.turns += 1
        try:
            low = float((context_usage or {})["percentage"]) < \
                threshold(context_usage, percent) - HYSTERESIS_POINTS
        except (KeyError, TypeError, ValueError):
            low = False
        if low or self.turns >= HYSTERESIS_TURNS:
            self.armed = True
        return self.armed


def handoff_request_text(reason):
    reason = str(reason or "rollover").strip()
    ask = ("Call the `handoff` tool now, exactly once, with position, next_action and"
           " status, plus active_threads (a list of one-line strings) and learned (a list"
           " of objects, each with topic and fact) when you have them. The next"
           " generation starts from what you write; nothing else is needed from you"
           " in this turn.")
    if is_bequest(reason):                       # a bequest: quoted whole, answered in the tool
        return ("Your generation is ending. The framework's request, verbatim:\n\n%s\n\n"
                "Answer it through the handoff tool (what it asks you to write goes in"
                " `position`). %s" % (reason, ask))
    return "Your generation is ending (%s). %s" % (reason, ask)


class HandoffBox:
    """The handoff tool's summary, carried from the tool's worker thread
    to the runner's loop: `set` from any thread, `await wait()` on the
    loop. The runner awaits it; it never polls a file."""

    def __init__(self):
        self.summary = None
        self._loop = None
        self._event = None

    def arm(self, loop, *, keep=False):
        if not keep:
            self.summary = None
        self._loop, self._event = loop, asyncio.Event()
        if self.summary is not None:
            self._event.set()

    def set(self, summary):
        self.summary = summary
        loop, event = self._loop, self._event
        if loop is not None and event is not None:
            loop.call_soon_threadsafe(event.set)

    async def wait(self, timeout):
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return self.summary

    def disarm(self):
        self._loop = self._event = None


def write_emergency_handoff(home, *, name, reason, tail):
    """The framework's emergency handoff, marked degraded, from the session
    store's tail: a real signal loss, never the normal flow."""
    path = Path(home) / "data" / "handoff.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, "\n".join([
        "# EMERGENCY HANDOFF (framework-generated)",
        "degraded_state: true",
        "reason: %s" % reason,
        "generated_at: %s" % datetime.now(timezone.utc).isoformat(),
        "cousin: %s" % name,
        "",
        "## Observed activity (session store tail)",
        tail[-2000:] if tail else "(no transcript)",
    ]) + "\n")
    return path


# The rollover's own record across a kill (#286): written when the handoff
# is done, removed when the row closes. A runner that dies in between
# finds it at its next start, for the same flip row, and goes on from
# there: no second handoff on a session that already handed off, no fresh
# session and boot of its own before the rollover makes the real one.
JOURNAL = ("data", "rollover.json")


def journal_path(home):
    return Path(home).joinpath(*JOURNAL)


def read_journal(home):
    """The journal's dict, or None (missing, unreadable, not an object)."""
    try:
        data = json.loads(journal_path(home).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_journal(home, row_id, old_session, generation, handoff):
    path = journal_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"row": row_id, "phase": "handed_off",
                               "old_session": old_session, "generation": generation,
                               "handoff": handoff, "ts": time.time()}))
    tmp.replace(path)


def advance_journal(home, phase, **fields):
    """Move the journal to a later phase ("bumped", "digest_queued"),
    keeping what it holds. No journal, nothing written."""
    data = read_journal(home)
    if data is None:
        return
    data.update(fields, phase=phase, ts=time.time())
    path = journal_path(home)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def clear_journal(home):
    try:
        journal_path(home).unlink()
    except FileNotFoundError:
        pass


def archive_generation(home, generation):
    arch = Path(home) / "data" / "generations" / ("gen-%04d" % generation)
    arch.mkdir(parents=True, exist_ok=True)
    for src in (Path(home) / "STATUS.md", Path(home) / "data" / "handoff.md",
                Path(home) / "data" / "active-threads.md"):
        if src.exists():
            shutil.copyfile(src, arch / src.name)
    return arch


def put_once(inbox, home, reason):
    """(inbox_id, coalesced). A PLAIN reason joins any pending rollover
    (one per home). A BEQUEST is never coalesced away: it replaces
    the body of a QUEUED plain row, and gets its own row when the pending
    one is already running or is itself a bequest."""
    from cousin_lib import memory
    reason = str(reason or "rollover")
    pending = inbox.open_rows("flip")
    if pending and not is_bequest(reason):
        memory.record_event(home, "framework", "framework:rollover",
                            "rollover request coalesced into inbox row %d (%s)"
                            % (pending[0]["id"], reason[:80]), "rollover")
        return pending[0]["id"], True
    if pending and is_bequest(reason):
        for row in pending:
            if row["state"] == "queued" and not is_bequest(row["body"]) \
                    and inbox.replace_body(row["id"], reason):
                memory.record_event(home, "framework", "framework:rollover",
                                    "a bequest replaced the queued rollover in row %d" % row["id"],
                                    "rollover")
                return row["id"], True
    return inbox.put(Item(thread_id="system", source="flip", body=reason,
                          sender="runner")), False


def degraded_digest(home, *, slug, generation, error):
    """The digest when prompt.state_digest failed: the last handoff
    verbatim, marked degraded. Never "no digest": the new session must not
    start blind because a layer builder raised."""
    try:
        handoff = (Path(home) / "data" / "handoff.md").read_text()
    except Exception as exc:  # noqa: BLE001 - OSError, or bytes that are not UTF-8
        handoff = "(no readable handoff on disk: %s: %s)" % (type(exc).__name__, exc)
    return ("STATE DIGEST FOR COUSIN: %s\nGeneration: %d\nDEGRADED: the digest could not be"
            " built (%s); the last handoff follows verbatim.\n\n%s\n"
            % (slug, generation, error, handoff[:8000]))


def wait_for_row(inbox, inbox_id, timeout, *, alive=None):
    """The rollover's answer from its row. `alive()`, when given, ends the
    wait early if the runner is gone: the row stays queued (durable) and
    the next start finishes it."""
    deadline = time.monotonic() + float(timeout)
    while True:
        row = inbox.get(inbox_id)
        if row and row["state"] == "done":
            try:
                detail = json.loads(row["detail"] or "{}")
            except ValueError:
                detail = {"detail": row["detail"]}
            ok = row["outcome"] == "delivered"
            out = {"ok": ok, "inbox_id": inbox_id,
                   "reason": (None if ok else detail.get("error")) or detail.get("reason")
                   or row["detail"] or row["outcome"]}
            out.update({k: v for k, v in detail.items() if k not in out})
            return out
        if alive is not None and not alive():
            return {"ok": False, "inbox_id": inbox_id,
                    "reason": "runner not running; rollover queued as inbox row %d" % inbox_id}
        if time.monotonic() >= deadline:
            return {"ok": False, "inbox_id": inbox_id,
                    "reason": "rollover still running after %.0fs" % float(timeout)}
        time.sleep(0.1)


def request(inbox, home, reason, *, alive, timeout):
    """Put (or join) the pending `flip` row and wait for its answer.
    `alive` is a callable: the runner's worker in-process,
    `runner.main.is_running(home)` from another process (flip.py)."""
    inbox_id, coalesced = put_once(inbox, home, reason)
    wake.poke(home)
    out = wait_for_row(inbox, inbox_id, timeout, alive=alive)
    out["coalesced"] = coalesced
    return out
