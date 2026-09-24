"""Phase 5 exit criteria (master plan, "Phase 5: Views"), through a real
ConsoleServer over HTTP: a runner cousin's thinking, tool calls and results
are visible live in the console and in cousin-watch, and the interrupt ends
a running turn. The runner is the reference FakeRunner holding the cousin's
lock, as cousin-runner does; the operator's message arrives through the chat
page's own send route (no chat server runs)."""
import io
import json
import threading
import time
import unittest
import urllib.request

from cousin_lib import watch
from cousin_lib.runner import main
from cousin_lib.runner.fake import FakeRunner
from tests.console._harness import ConsoleCase


class TestViewsExit(ConsoleCase):
    def _runner(self, home):
        runner = FakeRunner(home, turn_seconds=4.0)
        held, release = threading.Event(), threading.Event()

        def hold():
            with main.hold_lock(home):
                runner.stream.append("runner", {"kind": runner.kind, "pid": 1, "unsupported": []})
                runner.start()
                held.set()
                release.wait(20)
                runner.stop(timeout=5)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        return runner

    def _read_until(self, resp, want, timeout=8.0):
        """runner-event payloads off the live SSE socket until `want(event)`."""
        seen, event_type = [], None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = resp.readline().decode()
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: ") and event_type == "runner-event":
                event = json.loads(line[6:])
                seen.append(event)
                if want(event):
                    return seen
        self.fail("never saw the wanted event; saw %s" % [e["kind"] for e in seen])

    def test_a_turn_is_visible_live_and_the_interrupt_ends_it(self):
        home = self.cousin("wren", operator="Priya", extra='\n[agent]\nrunner = "fake"\n')
        self.serve()
        runner = self._runner(home)
        stream = urllib.request.urlopen(
            "http://127.0.0.1:%d/api/cousins/wren/stream" % self.server.port, timeout=10)
        self.addCleanup(stream.close)
        status, sent = self.post("/api/chat/send", {"cousin": "wren", "user": "Priya",
                                                    "message": "reconcile the ledger"})
        self.assertEqual(status, 200, sent)
        seen = self._read_until(stream, lambda e: e["kind"] == "tool")
        self.assertIn("turn_start", [e["kind"] for e in seen])
        runner.stream.append("thinking", {"length": 18, "text": "the ledger first.."})
        self._read_until(stream, lambda e: e["kind"] == "thinking")
        self.assertEqual(self.post("/api/cousins/wren/interrupt"),
                         (200, {"ok": True, "outcome": "delivered"}))
        done = self._read_until(stream, lambda e: e["kind"] == "result")
        self.assertTrue(done[-1]["payload"]["interrupted"])
        out = io.StringIO()
        watch.run(home, out=out)
        text = out.getvalue()
        for piece in ("reconcile the ledger", "think", "the ledger first..", "tool", "Bash",
                      "rows [", "interrupted"):
            self.assertIn(piece, text)
        self.assertEqual(self.get("/api/cousins")[1]["cousins"][0]["runner"]["state"], "idle")


if __name__ == "__main__":
    unittest.main()
