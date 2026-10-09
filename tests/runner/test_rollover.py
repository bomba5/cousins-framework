"""The pure parts of the rollover."""
import asyncio
import json
import threading
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner import rollover
from cousin_lib.runner.inbox import Inbox
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class TestPressure(HermeticCase):
    def test_at_or_over_the_percent_is_due(self):
        self.assertTrue(rollover.pressure_due({"percentage": 80.0}, 80.0))
        self.assertFalse(rollover.pressure_due({"percentage": 79.9}, 80.0))

    def test_the_clis_autocompact_threshold_pulls_the_trigger_earlier_in_tokens(self):
        near = {"percentage": 60.0, "totalTokens": 146_000, "autoCompactThreshold": 155_000}
        far = {"percentage": 60.0, "totalTokens": 140_000, "autoCompactThreshold": 155_000}
        self.assertTrue(rollover.pressure_due(near, 80.0))         # within 10,000 tokens
        self.assertFalse(rollover.pressure_due(far, 80.0))

    def test_an_unreadable_usage_is_never_due(self):
        for bad in (None, {}, {"percentage": "x"}):
            self.assertFalse(rollover.pressure_due(bad, 80.0))


class TestBequest(HermeticCase):
    BEQUEST = "You are about to be reincarnated.\nCall handoff with your bequest."

    def test_what_a_bequest_is(self):
        self.assertTrue(rollover.is_bequest(self.BEQUEST))
        self.assertTrue(rollover.is_bequest("x" * 121))
        self.assertFalse(rollover.is_bequest("context pressure 91%"))

    def test_a_bequest_replaces_a_queued_plain_row(self):
        home = temp_home(self); inbox = Inbox(home)
        plain, _ = rollover.put_once(inbox, home, "max_age")
        rid, joined = rollover.put_once(inbox, home, self.BEQUEST)
        self.assertEqual((rid, joined), (plain, True))
        self.assertEqual(inbox.get(plain)["body"], self.BEQUEST)
        self.assertEqual(len(inbox.open_rows("flip")), 1)

    def test_a_bequest_gets_its_own_row_when_a_rollover_is_running(self):
        home = temp_home(self); inbox = Inbox(home)
        plain, _ = rollover.put_once(inbox, home, "max_age")
        inbox.claim_id(plain, claimant="runner")
        rid, joined = rollover.put_once(inbox, home, self.BEQUEST)
        self.assertNotEqual(rid, plain); self.assertFalse(joined)
        self.assertEqual(inbox.get(plain)["body"], "max_age")

    def test_a_plain_request_joins_a_queued_bequest_without_touching_it(self):
        home = temp_home(self); inbox = Inbox(home)
        rid, _ = rollover.put_once(inbox, home, self.BEQUEST)
        again, joined = rollover.put_once(inbox, home, "context pressure 90%")
        self.assertEqual((again, joined), (rid, True))
        self.assertEqual(inbox.get(rid)["body"], self.BEQUEST)

    def test_a_second_bequest_is_never_merged_into_the_first(self):
        home = temp_home(self); inbox = Inbox(home)
        a, _ = rollover.put_once(inbox, home, self.BEQUEST)
        b, joined = rollover.put_once(inbox, home, self.BEQUEST + "\nThe second one.")
        self.assertNotEqual(a, b); self.assertFalse(joined)


class TestHysteresis(HermeticCase):
    def test_armed_until_a_rollover(self):
        self.assertTrue(rollover.Hysteresis().allow({"percentage": 95.0}, 80.0))

    def test_five_turns_rearm_it(self):
        h = rollover.Hysteresis(); h.rolled_over()
        seen = [h.allow({"percentage": 91.0}, 80.0) for _ in range(5)]
        self.assertEqual(seen, [False, False, False, False, True])

    def test_a_drop_below_threshold_minus_ten_rearms_it_at_once(self):
        h = rollover.Hysteresis(); h.rolled_over()
        self.assertFalse(h.allow({"percentage": 75.0}, 80.0))
        self.assertTrue(h.allow({"percentage": 69.0}, 80.0))


class TestHandoffBox(HermeticCase):
    def test_set_from_another_thread_wakes_the_waiter(self):
        box = rollover.HandoffBox()

        async def go():
            box.arm(asyncio.get_running_loop())
            threading.Timer(0.05, box.set, args=({"next_action": "x"},)).start()
            return await box.wait(2.0)
        self.assertEqual(asyncio.run(go()), {"next_action": "x"})

    def test_wait_times_out_to_none(self):
        box = rollover.HandoffBox()

        async def go():
            box.arm(asyncio.get_running_loop())
            return await box.wait(0.05)
        self.assertIsNone(asyncio.run(go()))

    def test_a_set_before_arm_is_kept_when_asked(self):
        box = rollover.HandoffBox(); box.set({"a": 1})

        async def go():
            box.arm(asyncio.get_running_loop(), keep=True)
            return await box.wait(0.05)
        self.assertEqual(asyncio.run(go()), {"a": 1})


class TestTexts(HermeticCase):
    def test_a_short_reason_is_inline(self):
        self.assertIn("(max_age)", rollover.handoff_request_text("max_age"))

    def test_a_bequest_is_quoted_whole(self):
        bequest = "You are about to be reincarnated.\nWrite a bequest to your successor."
        text = rollover.handoff_request_text(bequest)
        self.assertIn(bequest, text)
        self.assertIn("handoff", text)


class TestFiles(HermeticCase):
    def test_the_emergency_handoff_is_marked_degraded(self):
        home = temp_home(self)
        path = rollover.write_emergency_handoff(home, name="Wren", reason="handoff timeout (300s)",
                                                tail="last words")
        text = path.read_text()
        self.assertEqual(path, home / "data" / "handoff.md")
        self.assertIn("degraded_state: true", text)
        self.assertIn("handoff timeout (300s)", text)
        self.assertIn("last words", text)

    def test_a_degraded_digest_survives_a_handoff_that_is_not_utf8(self):
        home = temp_home(self)
        (home / "data" / "handoff.md").write_bytes(b"\xff\xfe not text")
        text = rollover.degraded_digest(home, slug="wren", generation=2, error="x")
        self.assertIn("DEGRADED", text)
        self.assertIn("UnicodeDecodeError", text)

    def test_archive_copies_status_and_the_handoff(self):
        # a stray active-threads.md (an older framework wrote it) is not
        # archived: STATUS.md is the one list (meeting 11 A)
        home = temp_home(self)
        (home / "STATUS.md").write_text("s"); (home / "data" / "handoff.md").write_text("h")
        (home / "data" / "active-threads.md").write_text("t")
        arch = rollover.archive_generation(home, 7)
        self.assertEqual(arch, home / "data" / "generations" / "gen-0007")
        self.assertEqual(sorted(p.name for p in arch.iterdir()), ["STATUS.md", "handoff.md"])


class TestRequest(HermeticCase):
    def test_a_dead_runner_gets_a_durable_row_and_an_honest_answer(self):
        home = temp_home(self); inbox = Inbox(home)
        out = rollover.request(inbox, home, "max_age", alive=lambda: False, timeout=1)
        self.assertFalse(out["ok"])
        row = inbox.get(out["inbox_id"])
        self.assertEqual((row["source"], row["thread_id"], row["state"]), ("flip", "system", "queued"))

    def test_a_second_request_coalesces_into_the_first(self):
        home = temp_home(self); inbox = Inbox(home)
        first, c1 = rollover.put_once(inbox, home, "max_age")
        second, c2 = rollover.put_once(inbox, home, "context pressure 90%")
        self.assertEqual((first, c1, c2), (second, False, True))
        self.assertEqual(len(inbox.open_rows("flip")), 1)
        raw = "".join(p.read_text() for p in (home / "memory" / "raw").glob("*.jsonl"))
        self.assertIn("coalesced", raw)

    def test_wait_for_row_reads_the_outcome_and_detail(self):
        home = temp_home(self); inbox = Inbox(home)
        rid = inbox.put(Item("system", "flip", "r", sender="runner"))
        inbox.done(rid, "delivered", json.dumps({"generation": 4, "handoff": "clean"}))
        out = rollover.wait_for_row(inbox, rid, 1)
        self.assertTrue(out["ok"]); self.assertEqual(out["generation"], 4)

    def test_an_open_row_at_the_timeout_is_unknown_not_fine(self):
        home = temp_home(self); inbox = Inbox(home)
        rid = inbox.put(Item("system", "flip", "r", sender="runner"))
        out = rollover.wait_for_row(inbox, rid, 0.1)
        self.assertFalse(out["ok"]); self.assertIn("still running", out["reason"])

    def test_a_runner_that_dies_while_waited_on_ends_the_wait(self):
        home = temp_home(self); inbox = Inbox(home)
        rid = inbox.put(Item("system", "flip", "r", sender="runner"))
        out = rollover.wait_for_row(inbox, rid, 30, alive=lambda: False)
        self.assertFalse(out["ok"]); self.assertIn("not running", out["reason"])


if __name__ == "__main__":
    unittest.main()
