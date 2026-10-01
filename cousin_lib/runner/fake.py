"""FakeRunner: the reference runner, no model, no terminal.

It exists so the contract suite has a known-good implementation and so
every producer, the console and the supervisor can be tested against
a runner that behaves exactly as the spec says, deterministically.

`script` is the steps every turn records: "tool" (a Bash tool event),
"fail_once" (the first time any turn reaches it, the turn body raises;
afterwards it is a tool step), or any other name, recorded as that
tool. `turn_seconds` holds the first step open that long, folding
operator/person/peer chat in while it waits.
"""
import threading
import time
import uuid
from pathlib import Path

from cousin_lib.delivery import DELIVERED, FAILED, QUEUED, Item
from cousin_lib.runner import wake
from cousin_lib.runner.base import INTERRUPT, NO_TURN, Receipt, folds_into_turn
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from cousin_lib.runner.turn import Turn


class FakeRunner:
    kind = "fake"          # what runner/status.py reports (the `runner` event)
    # The contract items this runner DECLARES unsupported, and those a
    # plugin meets; read at class level by runner/contract_table.py
    UNSUPPORTED = ()
    PLUGIN_ITEMS = ()
    def __init__(self, home, *, turn_seconds=0.0, script=None, policy=None):
        self.home = home
        self.policy = policy     # held for symmetry with SdkRunner; no tools, no hooks
        self.turn_seconds = float(turn_seconds)
        self.script = script or ["tool"]
        self.session_id = "fake-" + uuid.uuid4().hex[:8]
        self.inbox = Inbox(home)
        self.stream = EventStream(home, self.session_id)
        self.machine = StateMachine(on_change=self._on_state)
        self.turn = Turn()
        self._stop = threading.Event()
        self._interrupt = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._failed_once = False
        # the runner's own root, never the environment's: nothing
        # exports FRAMEWORK_ROOT for a reference runner
        from cousin_lib.config import FrameworkConfig
        self.root = FrameworkConfig.root_from_home(Path(home)) or Path(home).parent.parent

    def _on_state(self, old, new, detail):
        self.stream.append("state", {"from": old, "to": new, "detail": detail})

    # -- Runner protocol -------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, *, timeout=30.0):
        if self.machine.state == "stopped":
            return
        self._stop.set()
        with self._lock:
            self._interrupt.set()
        wake.poke(self.home)
        if self._thread is not None:
            self._thread.join(timeout)
        self.turn.end()
        with self._lock:
            if self.machine.state != "stopped":
                self.machine.to("stopped")

    def state(self):
        return self.machine.state

    def enqueue(self, item):
        if not isinstance(item, Item):
            raise TypeError("enqueue() takes a delivery.Item")
        inbox_id = self.inbox.put(item)
        wake.poke(self.home)
        return Receipt(inbox_id=inbox_id, outcome=QUEUED)

    def interrupt(self):
        # Under the lock that `_fold_midturn` checks it under: a fold that
        # starts after this returns never claims a row.
        with self._lock:
            if self.machine.state != "running":
                return False
            self._interrupt.set()
        return True

    def rollover(self, reason):
        from cousin_lib.runner import rollover as _rollover
        return _rollover.request(self.inbox, self.home, reason, alive=self.worker_alive,
                                 timeout=10.0)

    def events(self, after=None):
        return self.stream.tail(after=after)

    def unsupported(self):
        return list(self.UNSUPPORTED)

    def plugin_items(self):
        """The contract items a plugin meets (none on this lane); optional in
        the Runner protocol, read by contract_table."""
        return list(self.PLUGIN_ITEMS)

    # -- phase-2 CLI conveniences, NOT in the Runner protocol --------------
    def worker_alive(self):
        """True while the worker thread runs (SdkRunner.worker_alive)."""
        return self._thread is not None and self._thread.is_alive()

    # -- the loop ---------------------------------------------------------
    def _wake_error(self, message):
        self.stream.append("error", {"error": message})

    def _loop(self):
        with wake.listen(self.home, self._wake_error) as listener:
            while not self._stop.is_set():
                try:
                    rows = self.inbox.claim(limit=1, claimant=self.session_id)
                except Exception as exc:  # noqa: BLE001 - a store failure is never silence
                    self._fail_turn([], exc)
                    time.sleep(0.2)  # a wedged store must not spin the loop
                    continue
                if not rows:
                    listener.wait(timeout=0.2)
                    continue
                if rows[0]["source"] == INTERRUPT:
                    # between turns: nothing to interrupt, and never a turn
                    self.inbox.done(rows[0]["id"], FAILED, NO_TURN)
                    continue
                try:
                    (self._rollover_row if rows[0]["source"] == "flip" else self._turn)(rows[0])
                except Exception as exc:  # noqa: BLE001 - the success tail can still raise
                    # after its rows are already closed; `[]` because nothing here is
                    # safe to re-close (see `_fail_turn`'s docstring)
                    self._fail_turn([], exc)

    def _fold_midturn(self, consumed):
        """Claim operator/person/peer chat rows that arrived during the turn
        (they are folded into it and closed by its result).
        The caller folds only while the turn is live: never once an
        interrupt is asked (`base.folds_into_turn`)."""
        with self._lock:
            if self._interrupt.is_set():
                return
            rows = self.inbox.claim(limit=10, claimant=self.session_id)
        for row in rows:
            if row["source"] != INTERRUPT and folds_into_turn(row["source"], row["thread_id"]) \
                    and not self._interrupt.is_set():
                consumed.append(row)
                self.turn.add(row)
            else:                  # an interrupt row is _take_interrupts'
                self.inbox.requeue(row["id"])

    def _take_interrupts(self):
        """Interrupt rows on their own path, every poll of the
        live turn, as SdkRunner takes them: the first interrupts, and every
        one closes `delivered`."""
        for row in self.inbox.open_rows(INTERRUPT):
            if row["state"] != "queued" or \
                    self.inbox.claim_id(row["id"], claimant=self.session_id) is None:
                continue
            with self._lock:
                already = self._interrupt.is_set()
                self._interrupt.set()
            self.inbox.done(row["id"], DELIVERED, "the live turn was already being interrupted"
                            if already else "interrupted the live turn")

    def _fail_turn(self, consumed, exc):
        """A turn's failure path: never silence (global constraint).
        `consumed` is only ever rows this call may safely close: `_turn`'s
        own try/except passes what the turn had folded in when its BODY
        raised (those rows are certainly still open); `_loop`'s outer
        guards pass `[]`, because a store failure claimed nothing and a
        `_turn()` call that raised past its own try may already have
        closed its rows in the success tail - `Inbox.done` has no
        re-close guard, so guessing wrong here would silently overwrite a
        correct outcome.

        The state transition to `errored` only fires from `idle` or
        `running`, never from `stopped`, which `stop()` may have forced
        onto the machine while this turn was still failing; `errored ->
        idle` only fires from `errored`, for the same reason. Both are
        illegal transitions out of `stopped` (`TRANSITIONS["stopped"]` is
        empty), and raising IllegalTransition from inside this handler
        would be exactly the silent death this exists to close."""
        message = "%s: %s" % (type(exc).__name__, exc)
        # The state first: whoever sees the `error` event also sees `errored`.
        with self._lock:
            if self.machine.state in ("idle", "running"):
                self.machine.to("errored", message)
        self.turn.end()
        self.stream.append("error", {"error": message})
        # the result first: whoever reads a row closed finds its result;
        # the rows close even when the append raises
        try:
            self.stream.append("result", {"inbox_ids": [r["id"] for r in consumed],
                                          "interrupted": False,
                                          "is_error": True})
        finally:
            for row in consumed:
                self.inbox.done(row["id"], FAILED, message)
        with self._lock:
            if self.machine.state == "errored":
                self.machine.to("idle", "recovered")

    def _turn(self, first):
        consumed = [first]
        self._interrupt.clear()
        try:
            with self._lock:
                self.machine.to("running", "turn")
            self.turn.begin(first)
            self.stream.append("turn_start", {"inbox_ids": [first["id"]],
                                              "bodies": [first["body"]],
                                              "thread_id": first["thread_id"]})
            deadline = time.monotonic() + self.turn_seconds
            interrupted = False
            for step in self.script:
                if step == "fail_once" and not self._failed_once:
                    self._failed_once = True
                    raise RuntimeError("scripted failure")
                name = "Bash" if step in ("tool", "fail_once") else step
                self.stream.append("tool", {"name": name, "input": {"command": "true"}})
                while time.monotonic() < deadline:
                    self._take_interrupts()
                    if self._interrupt.is_set():
                        interrupted = True
                        break
                    self._fold_midturn(consumed)
                    time.sleep(0.02)
                if interrupted:
                    break
            if not interrupted:
                self._fold_midturn(consumed)
        except Exception as exc:  # noqa: BLE001 - a raising turn body is recorded, not lost
            self._fail_turn(consumed, exc)
            return

        # Success tail: reached only when the body above did not raise, so
        # a failure here (it can still raise) must NOT route back
        # through `_fail_turn(consumed, ...)` - these rows may already be
        # closed by the loop just below. It propagates to `_loop`'s outer
        # guard instead, which calls `_fail_turn([], exc)`.
        outcome = DELIVERED
        # the result first, then the rows it names, closed even when
        # the append raises (the turn did its work)
        try:
            self.stream.append("result", {"inbox_ids": [r["id"] for r in consumed],
                                          "interrupted": interrupted,
                                          "is_error": False})
        finally:
            for row in consumed:
                self.inbox.done(row["id"], outcome, "turn %s" % self.session_id)
        self.turn.end()
        with self._lock:
            if self.machine.state == "running":
                self.machine.to("idle", "turn done")

    def _rollover_row(self, row):
        """The reference rollover: no model, so the handoff is simulated
        clean; everything else is the SDK runner's sequence, including the
        generation moving only once the new session exists, the degraded
        digest, the failure path before that point, the degrade-never-
        fail tail after it, and the bequest rule."""
        import json
        from cousin_lib import boot, session
        from cousin_lib.runner import prompt
        from cousin_lib.runner import rollover as _rollover
        with self._lock:
            if self.machine.state != "idle":     # stop() won the race: the row waits
                self.inbox.requeue(row["id"])
                return
            self.machine.to("rolling_over", row["body"].splitlines()[0][:120])
        try:
            session.run_phase(self.home, "end")
            _rollover.archive_generation(self.home, boot.read_generation(self.home))
            self.session_id = "fake-" + uuid.uuid4().hex[:8]       # the new session
        except Exception as exc:  # noqa: BLE001 - never a wedged machine
            message = "%s: %s" % (type(exc).__name__, exc)
            with self._lock:
                if self.machine.state == "rolling_over":
                    self.machine.to("errored", "rollover failed: " + message)
                    self.machine.to("idle", "recovered")
            self.inbox.done(row["id"], FAILED, json.dumps({
                "reason": row["body"], "error": message,
                "generation": boot.read_generation(self.home)}))
            self._close_plain_duplicates(row, FAILED, {"error": message})
            return
        # the point of no return: degrade, never fail (SdkRunner._rollover_row)
        problems, generation, digest_id = [], boot.read_generation(self.home), None
        try:
            generation = boot.bump_generation(self.home)
        except Exception as exc:  # noqa: BLE001 - named in the detail
            problems.append("generation not moved: %s: %s" % (type(exc).__name__, exc))
        try:
            session.run_phase(self.home, "start")
        except Exception as exc:  # noqa: BLE001 - named in the detail
            problems.append("start hooks: %s: %s" % (type(exc).__name__, exc))
        try:
            try:
                digest = prompt.state_digest(self.home, root=self.root, slug=Path(self.home).name,
                                             generation=generation)["text"]
            except Exception as exc:  # noqa: BLE001 - degraded, never none
                digest = _rollover.degraded_digest(self.home, slug=Path(self.home).name,
                                                   generation=generation, error=exc)
            digest_id = self.inbox.put(Item(thread_id="system", source="boot", body=digest,
                                            sender="runner"))
        except Exception as exc:  # noqa: BLE001 - the session runs on without one
            problems.append("digest: %s: %s" % (type(exc).__name__, exc))
        detail = {"reason": row["body"], "handoff": "simulated", "generation": generation}
        if problems:
            detail["problems"] = problems
        with self._lock:
            if self.machine.state == "rolling_over":
                self.machine.to("idle", "rolled over")
        self.inbox.done(row["id"], DELIVERED, json.dumps(detail))
        self._close_plain_duplicates(row, DELIVERED, detail)
        self.stream.append("rollover", dict(detail, phase="done"))
        if digest_id is None or self._stop.is_set():
            return
        first = self.inbox.claim_id(digest_id, claimant=self.session_id)
        if first is not None:
            self._turn(first)

    def _close_plain_duplicates(self, row, outcome, detail):
        """SdkRunner._close_duplicates' rule: plain duplicates only, never a
        bequest, and the close guarded on the body read here."""
        import json
        from cousin_lib.runner import rollover as _rollover
        for other in self.inbox.open_rows("flip"):
            if other["id"] != row["id"] and other["state"] == "queued" \
                    and not _rollover.is_bequest(other["body"]):
                self.inbox.done_if_queued(other["id"], outcome,
                                          json.dumps(dict(detail, coalesced_into=row["id"])),
                                          body=other["body"])
