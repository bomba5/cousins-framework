"""FakeRunner: the reference runner, no model, no terminal.

It exists so the contract suite has a known-good implementation and so
every producer, the console and the supervisor can be tested against
a runner that behaves exactly as the spec says, deterministically.
"""
import threading
import time
import uuid

from cousin_lib.delivery import DELIVERED, QUEUED, Item
from cousin_lib.runner import wake
from cousin_lib.runner.base import Receipt
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream

FOLDED_KINDS = ("operator", "person")


class FakeRunner:
    def __init__(self, home, *, turn_seconds=0.0, script=None):
        self.home = home
        self.turn_seconds = float(turn_seconds)
        self.script = script or ["tool"]
        self.session_id = "fake-" + uuid.uuid4().hex[:8]
        self.inbox = Inbox(home)
        self.stream = EventStream(home, self.session_id)
        self.machine = StateMachine(on_change=self._on_state)
        self._stop = threading.Event()
        self._interrupt = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

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
        self._interrupt.set()
        wake.poke(self.home)
        if self._thread is not None:
            self._thread.join(timeout)
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
        if self.machine.state != "running":
            return False
        self._interrupt.set()
        return True

    def rollover(self, reason):
        return {"ok": False, "reason": "rollover arrives in phase 4"}

    def events(self, after=None):
        return self.stream.tail(after=after)

    def unsupported(self):
        return []

    # -- the loop ---------------------------------------------------------
    def _loop(self):
        with wake.Listener(self.home) as listener:
            while not self._stop.is_set():
                rows = self.inbox.claim(limit=1, claimant=self.session_id)
                if not rows:
                    listener.wait(timeout=0.2)
                    continue
                self._turn(rows[0])

    def _fold_midturn(self, consumed):
        """Claim operator/person chat rows that arrived during the turn
        (finding 1: they are folded into it and closed by its result)."""
        for row in self.inbox.claim(limit=10, claimant=self.session_id):
            kind = row["thread_id"].partition(":")[0]
            if row["source"] == "chat" and kind in FOLDED_KINDS:
                consumed.append(row)
            else:
                self.inbox.requeue(row["id"])

    def _turn(self, first):
        consumed = [first]
        self._interrupt.clear()
        with self._lock:
            self.machine.to("running", "turn")
        self.stream.append("turn_start", {"inbox_ids": [first["id"]],
                                          "bodies": [first["body"]],
                                          "thread_id": first["thread_id"]})
        deadline = time.monotonic() + self.turn_seconds
        interrupted = False
        for step in self.script:
            self.stream.append("tool", {"name": "Bash" if step == "tool" else step,
                                        "input": {"command": "true"}})
            while time.monotonic() < deadline:
                if self._interrupt.is_set():
                    interrupted = True
                    break
                self._fold_midturn(consumed)
                time.sleep(0.02)
            if interrupted:
                break
        self._fold_midturn(consumed)
        outcome = DELIVERED
        for row in consumed:
            self.inbox.done(row["id"], outcome, "turn %s" % self.session_id)
        self.stream.append("result", {"inbox_ids": [r["id"] for r in consumed],
                                      "interrupted": interrupted,
                                      "is_error": False})
        with self._lock:
            if self.machine.state == "running":
                self.machine.to("idle", "turn done")
