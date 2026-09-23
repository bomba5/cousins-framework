"""Proofs against the real SDK. Opt in: COUSIN_LIVE_SDK=1.
Costs a few small model calls on whatever lane the machine has."""
import os
import sys
import time
import unittest

from cousin_lib import delivery
from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

MODEL = "claude-haiku-4-5-20251001"


def _wait(pred, timeout, step=0.01):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


def _op(body):
    return Item("operator:priya", "chat", body, sender="Priya")


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLiveContinuity(HermeticCase):
    def _runner(self):
        home = temp_home(self, runner="sdk")
        r = SdkRunner(home, model=MODEL, idle_timeout_s=120)
        self.addCleanup(lambda: r.stop(timeout=30))
        r.start()
        return home, r

    def _one_session(self, r):
        inits = [e["payload"] for e in r.events() if e["kind"] == "session_init"]
        self.assertTrue(inits)
        self.assertIn(inits[0]["apiKeySource"], ("none", "ANTHROPIC_API_KEY"))
        self.assertEqual({i["session_id"] for i in inits}, {inits[0]["session_id"]},
                         "every init names the one session")

    def test_the_second_turn_remembers_the_first(self):
        home, r = self._runner()
        self.assertEqual(delivery.deliver(home, _op(
            "Remember this code word and reply only OK: zebracorn"),
            wait=True, timeout=120), delivery.DELIVERED)
        first_result_seq = max(e["seq"] for e in r.events() if e["kind"] == "result")
        self.assertEqual(delivery.deliver(home, _op(
            "What was the code word? Reply with the word only."),
            wait=True, timeout=120), delivery.DELIVERED)
        first_texts = [e for e in r.events()
                       if e["kind"] == "text" and e["seq"] <= first_result_seq]
        self.assertTrue(first_texts)
        texts = " ".join(e["payload"]["text"] for e in r.events()
                         if e["kind"] == "text" and e["seq"] > first_result_seq)
        self.assertIn("zebracorn", texts.lower())
        self._one_session(r)

    def test_a_message_sent_during_the_final_answer_is_not_lost(self):
        home, r = self._runner()
        inbox = r.inbox
        # The Bash step holds turn 1 open, so the second row lands while the
        # CLI is still in that turn (without it the answer and the result
        # arrive back to back and the row simply gets a turn of its own).
        self.assertEqual(delivery.deliver(home, _op(
            "First run the shell command `sleep 6` with the Bash tool. Then count"
            " from 1 to 80, one number per line, nothing else"), wait=False),
            delivery.QUEUED)
        self.assertTrue(_wait(lambda: any(e["kind"] in ("tool", "text")
                                          for e in r.events()), 120))
        self.assertEqual(delivery.deliver(home, _op("Reply with the single word: pomegranate"),
                                          wait=False), delivery.QUEUED)
        first, second = 1, 2
        self.assertTrue(_wait(lambda: inbox.get(second)["state"] == "done", 180, step=0.1))
        self.assertEqual(delivery.deliver(home, _op(
            "What single word did I ask you to reply with? Reply with the word only."),
            wait=True, timeout=180), delivery.DELIVERED)
        third = 3

        events = list(r.events())
        seq = [(e["kind"], e["payload"].get("echo_of") or e["payload"].get("inbox_ids")
                or e["payload"].get("name") or e["payload"].get("subtype")
                or e["payload"].get("type") or e["payload"].get("text", "")[:40])
               for e in events if e["kind"] not in ("state",)]
        print("\nSTREAM:", seq, file=sys.stderr)

        for i in (first, second, third):
            row = inbox.get(i)
            self.assertEqual((row["state"], row["outcome"]), ("done", "delivered"), i)
        echo = {e["payload"]["echo_of"]: e["seq"] for e in events
                if e["kind"] == "user" and e["payload"].get("echo_of")}
        self.assertEqual(sorted(echo), [first, second, third], "every row was echoed")
        results = [e for e in events if e["kind"] == "result"
                   and not e["payload"].get("drained")]
        closed = [i for e in results for i in e["payload"]["inbox_ids"]]
        self.assertEqual(sorted(closed), [first, second, third], "each row closed once")
        # one result per CLI turn: every result closes the rows echoed before it
        self.assertTrue(all(e["payload"]["inbox_ids"] for e in results), "no empty result")
        for e in results:
            for i in e["payload"]["inbox_ids"]:
                self.assertLess(echo[i], e["seq"], "row %d closed before its echo" % i)
        case = "A" if echo[second] < results[0]["seq"] else "B"
        print("CASE %s: %d results" % (case, len(results)), file=sys.stderr)
        after_second = " ".join(e["payload"]["text"] for e in events if e["kind"] == "text"
                                and e["seq"] > min(echo[second], results[0]["seq"]))
        self.assertIn("pomegranate", after_second.lower())
        third_answer = " ".join(e["payload"]["text"] for e in events
                                if e["kind"] == "text" and e["seq"] > echo[third])
        self.assertIn("pomegranate", third_answer.lower())
        self._one_session(r)


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLiveCache(HermeticCase):
    """Cache-bust guard: the second turn of one session must read the
    prompt cache, not rebuild it, and read back at least what the first
    turn read or wrote (less 512 tokens of slack). If it doesn't,
    something per-turn is landing in the cached prefix (test_wiring
    guards the two places that could: system_prompt and the tool
    definitions)."""

    def _runner(self):
        home = temp_home(self, runner="sdk")
        r = SdkRunner(home, model=MODEL, idle_timeout_s=120)
        self.addCleanup(lambda: r.stop(timeout=30))
        r.start()
        return home, r

    def _last_usage(self, r):
        results = [e["payload"] for e in r.events()
                  if e["kind"] == "result" and not e["payload"].get("drained")]
        self.assertTrue(results)
        return results[-1]["usage"]

    def test_the_second_turn_reads_the_prompt_cache(self):
        home, r = self._runner()
        self.assertEqual(delivery.deliver(home, _op("Reply only with the word OK"),
                                          wait=True, timeout=120), delivery.DELIVERED)
        first_usage = self._last_usage(r)
        self.assertEqual(delivery.deliver(home, _op("Reply only with the word OK again"),
                                          wait=True, timeout=120), delivery.DELIVERED)
        second_usage = self._last_usage(r)
        print("\nUSAGE turn 1:", first_usage, file=sys.stderr)
        print("USAGE turn 2:", second_usage, file=sys.stderr)
        evidence = "turn 1 usage: %r; turn 2 usage: %r" % (first_usage, second_usage)
        cache_read = (second_usage or {}).get("cache_read_input_tokens") or 0
        self.assertGreater(cache_read, 0, evidence)
        # Turn 2 reads back at least everything turn 1 read or wrote: a
        # partial bust (a system prompt or tool list that changed between
        # the turns) rebuilds part of the prefix and falls short.
        first_read = (first_usage or {}).get("cache_read_input_tokens") or 0
        first_created = (first_usage or {}).get("cache_creation_input_tokens") or 0
        self.assertGreaterEqual(cache_read, first_read + first_created - 512, evidence)


if __name__ == "__main__":
    unittest.main()
