"""Live, opt in with COUSIN_LIVE_SDK=1: the console's
interrupt ends a real SDK turn. A real SdkRunner holds the cousin's lock, as
cousin-runner does; the turn runs `sleep 60` in Bash; POST
/api/cousins/<slug>/interrupt on a real ConsoleServer must answer
`delivered` and the turn's result must say interrupted within seconds. A
thinking event, when the model thinks, carries its text. One small model
turn on the machine's lane; at most two runs per attempt, a failure is
reported, not retried."""
import json
import os
import threading
import time
import unittest
import urllib.request

from cousin_lib import delivery
from cousin_lib.delivery import Item
from cousin_lib.runner import main
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _evidence(r):
    return "kinds=%r" % [e["kind"] for e in r.events()]


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLiveInterrupt(HermeticCase):
    def test_the_console_interrupts_a_real_turn(self):
        from cousin_lib.console.app import ConsoleServer
        from cousin_lib.runner.sdk import SdkRunner
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(root)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[operator]\nname = "Priya"\n\n'
            '[agent]\nrunner = "sdk"\n')
        r = SdkRunner(home, model="claude-haiku-4-5-20251001", idle_timeout_s=120)
        held, release = threading.Event(), threading.Event()

        def hold():
            with main.hold_lock(home):
                r.stream.append("runner", {"kind": r.kind, "pid": os.getpid(), "unsupported": []})
                r.start()
                held.set()
                release.wait(300)
                r.stop(timeout=30)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(30))
        console = ConsoleServer(root)
        console.start()
        self.addCleanup(console.stop)
        delivery.deliver(home, Item("operator:priya", "chat",
                                    "Run exactly this Bash command and nothing else: sleep 60",
                                    sender="Priya"), wait=False)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not any(
                e["kind"] == "tool" and e["payload"].get("name") == "Bash" for e in r.events()):
            time.sleep(0.2)
        self.assertTrue(any(e["kind"] == "tool" for e in r.events()), _evidence(r))
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/cousins/wren/interrupt" % console.port, data=b"{}",
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        self.assertEqual(body, {"ok": True, "outcome": "delivered"}, _evidence(r))
        deadline = time.monotonic() + 10
        results = []
        while time.monotonic() < deadline and not results:
            results = [e["payload"] for e in r.events() if e["kind"] == "result"]
            time.sleep(0.2)
        self.assertTrue(results and results[-1].get("interrupted"), _evidence(r))
        for e in r.events():
            if e["kind"] == "thinking":
                self.assertIn("text", e["payload"], _evidence(r))


if __name__ == "__main__":
    unittest.main()
