"""The continuity proof, against the real SDK. Opt in: COUSIN_LIVE_SDK=1.
Costs one small model call pair on whatever lane the machine has."""
import os
import time
import unittest

from cousin_lib import delivery
from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLiveContinuity(HermeticCase):
    def test_the_second_turn_remembers_the_first(self):
        home = temp_home(self, runner="sdk")
        r = SdkRunner(home, model="claude-haiku-4-5-20251001", turn_timeout_s=120)
        self.addCleanup(lambda: r.stop(timeout=30))
        r.start()
        self.assertEqual(delivery.deliver(home, Item(
            "operator:priya", "chat", "Remember this code word and reply only OK: zebracorn",
            sender="Priya"), wait=True, timeout=120), delivery.DELIVERED)
        self.assertEqual(delivery.deliver(home, Item(
            "operator:priya", "chat", "What was the code word? Reply with the word only.",
            sender="Priya"), wait=True, timeout=120), delivery.DELIVERED)
        texts = " ".join(e["payload"]["text"] for e in r.events() if e["kind"] == "text")
        self.assertIn("zebracorn", texts.lower())
        inits = [e for e in r.events() if e["kind"] == "session_init"]
        self.assertTrue(inits and inits[0]["payload"]["apiKeySource"] in ("none", "ANTHROPIC_API_KEY"))


if __name__ == "__main__":
    unittest.main()
