"""The daily cost cap ([agent] daily_cost_cap_usd): usage.spent_today, the
turn-start decision (runner/cost_cap.admit), the runners that call it, the
setting, the Telegram notice. Invented cast only."""
import json
import sqlite3
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from cousin_lib import health, usage
from cousin_lib.delivery import Item
from cousin_lib.runner import cost_cap
from cousin_lib.runner.fake import FakeRunner
from cousin_lib.runner.inbox import Inbox
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _spend(home, usd):
    """One usage row costing `usd` today: a client of its own, so the row
    is the whole figure (record diffs per client)."""
    usage.record(home, client_id=uuid.uuid4().hex, session_id="s",
                 result={"usage": {"input_tokens": 10, "output_tokens": 5},
                         "total_cost_usd": usd}, lane="login")


def _set_cap(home, value):
    text = (home / "cousin.toml").read_text()
    lines = [ln for ln in text.splitlines() if not ln.startswith(cost_cap.KEY)]
    text = "\n".join(lines) + "\n"
    if value is not None:
        text = text.replace("[agent]\n", "[agent]\n%s = %s\n" % (cost_cap.KEY, value), 1)
    (home / "cousin.toml").write_text(text)


class _Stream:
    def __init__(self):
        self.events = []

    def append(self, kind, payload):
        self.events.append((kind, payload))

    def kinds(self, kind):
        return [p for k, p in self.events if k == kind]


class TestSpentToday(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def test_no_usage_db_is_zero(self):
        self.assertEqual(usage.spent_today(self.home), 0.0)
        self.assertFalse((self.home / "data" / "usage.db").exists())   # nothing created

    def test_it_sums_todays_rows_only(self):
        _spend(self.home, 0.75)
        _spend(self.home, 1.25)
        conn = sqlite3.connect(self.home / "data" / "usage.db")
        try:
            with conn:
                conn.execute("UPDATE usage SET day = '2026-01-01' WHERE id = 1")
        finally:
            conn.close()
        self.assertAlmostEqual(usage.spent_today(self.home), 1.25)

    def test_now_picks_the_utc_day(self):
        _spend(self.home, 2.0)
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).timestamp()
        self.assertAlmostEqual(usage.spent_today(self.home), 2.0)
        self.assertEqual(usage.spent_today(self.home, now=tomorrow), 0.0)


class TestAdmit(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.root = self.home.parent.parent
        self.inbox = Inbox(self.home)
        self.stream = _Stream()

    def _claimed(self, thread, source="chat", sender="Sam", body="hello"):
        self.inbox.put(Item(thread, source, body, sender=sender))
        return self.inbox.claim(limit=1, claimant="t")[0]

    def _admit(self, row):
        return cost_cap.admit(self.home, row, inbox=self.inbox, stream=self.stream,
                              root=self.root)

    def _health(self):
        return health.read(self.root).get("cap:wren")

    def test_off_by_default_changes_nothing(self):
        _spend(self.home, 50.0)
        row = self._claimed("loop:heartbeat", source="loop")
        self.assertEqual(self._admit(row), row)
        self.assertEqual(self.stream.events, [])
        self.assertIsNone(self._health())

    def test_zero_is_off(self):
        _set_cap(self.home, 0)
        _spend(self.home, 50.0)
        row = self._claimed("peer:kestrel")
        self.assertEqual(self._admit(row), row)
        self.assertEqual(self.stream.events, [])

    def test_under_the_cap_the_turn_runs_and_health_is_ok(self):
        _set_cap(self.home, 5)
        _spend(self.home, 4.99)
        row = self._claimed("loop:heartbeat", source="loop")
        self.assertEqual(self._admit(row), row)
        self.assertEqual(self.stream.events, [])
        self.assertEqual(self._health()["state"], "ok")

    def test_a_loop_a_peer_and_a_schedule_are_refused_over_the_cap(self):
        _set_cap(self.home, 2.5)
        _spend(self.home, 3.0)
        for thread, source in (("loop:heartbeat", "loop"), ("peer:kestrel", "chat"),
                               ("schedule", "schedule"), ("meeting:7", "meeting"),
                               ("system", "propose")):
            row = self._claimed(thread, source=source)
            self.assertIsNone(self._admit(row), thread)
            done = self.inbox.get(row["id"])
            self.assertEqual((done["state"], done["outcome"]), ("done", "failed"), thread)
            self.assertEqual(done["detail"],
                             "daily cost cap reached: spent $3.00 of $2.50 today (UTC)")
        refused = self.stream.kinds("cap")
        self.assertEqual(len(refused), 5)
        self.assertTrue(all(e["refused"] and e["over"] for e in refused))
        self.assertEqual((refused[0]["limit"], refused[0]["spent"]), (2.5, 3.0))
        self.assertEqual(refused[0]["thread_id"], "loop:heartbeat")
        entry = self._health()
        self.assertEqual(entry["state"], "failing")
        self.assertIn("daily cost cap reached: spent $3.00 of $2.50 today (UTC)", entry["error"])

    def test_the_operator_is_told_once_a_day(self):
        _set_cap(self.home, 1)
        _spend(self.home, 1.0)
        for _ in range(3):
            self.assertIsNone(self._admit(self._claimed("loop:heartbeat", source="loop")))
        self.assertEqual([e["notice"] for e in self.stream.kinds("cap")], [True, False, False])
        notice = cost_cap.read_notice(self.home)
        self.assertEqual((notice["day"], notice["slug"], notice["limit"]),
                         (usage.utc_day(), "wren", 1.0))
        # yesterday's file does not stop today's notice
        notice["day"] = "2026-01-01"
        (self.home / cost_cap.NOTICE_FILE).write_text(json.dumps(notice))
        self.assertIsNone(self._admit(self._claimed("schedule", source="schedule")))
        self.assertTrue(self.stream.kinds("cap")[-1]["notice"])

    def test_an_operator_and_a_person_run_over_the_cap_with_the_note(self):
        _set_cap(self.home, 2)
        _spend(self.home, 4.1)
        for thread, who in (("operator:ana", "ana"), ("person:priya", "Priya")):
            row = self._claimed(thread, sender=who)
            out = self._admit(row)
            self.assertIsNotNone(out)
            self.assertEqual(out["body"], "hello")              # the sender's words untouched
            self.assertTrue(out["context"].startswith(
                "[runner] daily cost cap reached: $4.10 of $2.00 today (UTC); this turn runs"
                " because a person sent it."))
            self.assertEqual(self.inbox.get(row["id"])["state"], "claimed")
        allowed = self.stream.kinds("cap")
        self.assertEqual([e["allowed"] for e in allowed], ["person", "person"])
        self.assertTrue(all(e["over"] and "refused" not in e for e in allowed))
        self.assertIsNone(cost_cap.read_notice(self.home))     # nothing refused: no notice

    def test_the_note_goes_ahead_of_an_existing_context(self):
        _set_cap(self.home, 1)
        _spend(self.home, 1.5)
        self.inbox.put(Item("operator:ana", "chat", "hi", sender="ana", context="the side digest"))
        out = self._admit(self.inbox.claim(limit=1, claimant="t")[0])
        self.assertTrue(out["context"].startswith("[runner] daily cost cap reached"))
        self.assertTrue(out["context"].endswith("\n\nthe side digest"))

    def test_a_flip_the_boot_digest_and_an_interrupt_are_never_refused(self):
        _set_cap(self.home, 1)
        _spend(self.home, 9.0)
        for source in ("flip", "boot", "interrupt"):
            row = self._claimed("system", source=source, sender="runner")
            self.assertEqual(self._admit(row), row)
        self.assertEqual(self.stream.events, [])

    def test_a_value_that_is_not_a_number_reads_as_off_and_says_so(self):
        _set_cap(self.home, '"ten"')
        _spend(self.home, 50.0)
        row = self._claimed("loop:heartbeat", source="loop")
        self.assertEqual(self._admit(row), row)
        self.assertEqual(self._health()["state"], "failing")
        self.assertIn("read as off", self._health()["error"])

    def test_a_cap_turned_off_clears_its_failing_row(self):
        _set_cap(self.home, 1)
        _spend(self.home, 2.0)
        self._admit(self._claimed("loop:heartbeat", source="loop"))
        self.assertEqual(self._health()["state"], "failing")
        _set_cap(self.home, None)
        self._admit(self._claimed("loop:heartbeat", source="loop"))
        self.assertEqual(self._health()["state"], "ok")

    def test_a_broken_store_never_fails_the_turn(self):
        _set_cap(self.home, 1)
        row = self._claimed("loop:heartbeat", source="loop")
        with mock.patch.object(usage, "spent_today", side_effect=RuntimeError("disk")):
            self.assertEqual(self._admit(row), row)
        self.assertIn("daily cost cap: RuntimeError: disk", self.stream.kinds("error")[0]["error"])


class TestFakeRunner(HermeticCase):
    """The shared path end to end, on the reference runner."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="fake")
        self.root = self.home.parent.parent
        self.r = FakeRunner(self.home)
        self.addCleanup(lambda: self.r.stop(timeout=5))

    def _closed(self, inbox_id):
        return _wait(lambda: self.r.inbox.get(inbox_id)["state"] == "done")

    def _turns(self):
        return [e["payload"]["inbox_ids"][0] for e in self.r.events() if e["kind"] == "turn_start"]

    def test_a_loop_is_refused_and_the_operator_runs_over_the_cap(self):
        _set_cap(self.home, 1)
        _spend(self.home, 1.0)
        self.r.start()
        loop = self.r.enqueue(Item("loop:heartbeat", "loop", "tick", sender="loop")).inbox_id
        self.assertTrue(self._closed(loop))
        self.assertEqual(self.r.inbox.get(loop)["outcome"], "failed")
        self.assertEqual(self.r.inbox.get(loop)["detail"],
                         "daily cost cap reached: spent $1.00 of $1.00 today (UTC)")
        op = self.r.enqueue(Item("operator:ana", "chat", "status?", sender="ana")).inbox_id
        self.assertTrue(self._closed(op))
        self.assertEqual(self.r.inbox.get(op)["outcome"], "delivered")
        self.assertEqual(self._turns(), [op])                  # the loop never started a turn
        caps = [e["payload"] for e in self.r.events() if e["kind"] == "cap"]
        self.assertEqual([("refused" in c, c.get("allowed")) for c in caps],
                         [(True, None), (False, "person")])

    def test_the_setting_applies_without_a_restart(self):
        _spend(self.home, 3.0)
        self.r.start()
        first = self.r.enqueue(Item("peer:kestrel", "chat", "one", sender="kestrel")).inbox_id
        self.assertTrue(self._closed(first))
        self.assertEqual(self.r.inbox.get(first)["outcome"], "delivered")   # off: it runs
        _set_cap(self.home, 2)                                  # the same runner, no restart
        second = self.r.enqueue(Item("peer:kestrel", "chat", "two", sender="kestrel")).inbox_id
        self.assertTrue(self._closed(second))
        self.assertEqual(self.r.inbox.get(second)["outcome"], "failed")
        _set_cap(self.home, 10)                                 # raised: it runs again
        third = self.r.enqueue(Item("peer:kestrel", "chat", "three", sender="kestrel")).inbox_id
        self.assertTrue(self._closed(third))
        self.assertEqual(self.r.inbox.get(third)["outcome"], "delivered")
        self.assertEqual(self._turns(), [first, third])
        self.assertEqual(health.read(self.root)["cap:wren"]["state"], "ok")

    def test_a_row_folded_into_a_running_turn_is_never_refused(self):
        r = FakeRunner(self.home, turn_seconds=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        _set_cap(self.home, 1)
        r.start()
        op = r.enqueue(Item("operator:ana", "chat", "long one", sender="ana")).inbox_id
        self.assertTrue(_wait(lambda: r.state() == "running"))
        _spend(self.home, 5.0)                                  # the cap is reached mid-turn
        peer = r.enqueue(Item("peer:kestrel", "chat", "folded", sender="kestrel")).inbox_id
        self.assertTrue(_wait(lambda: r.inbox.get(peer)["state"] == "done"))
        self.assertEqual(r.inbox.get(peer)["outcome"], "delivered")
        self.assertTrue(_wait(lambda: r.inbox.get(op)["state"] == "done"))


class TestSetting(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.root = self.home.parent.parent

    def test_the_key_is_live_and_read_by_the_lanes_that_measure(self):
        from cousin_lib import agent_settings
        spec = agent_settings.SCHEMA[cost_cap.KEY]
        self.assertIs(spec["restart"], False)
        self.assertEqual(spec["default"], 0)
        self.assertIn(cost_cap.KEY, agent_settings.lane_keys("sdk"))
        self.assertIn(cost_cap.KEY, agent_settings.lane_keys("opencode"))
        self.assertNotIn(cost_cap.KEY, agent_settings.lane_keys("tmux"))
        self.assertFalse(agent_settings.needs_restart([cost_cap.KEY]))

    def test_validate_takes_a_number_and_refuses_the_rest(self):
        from cousin_lib import agent_settings
        self.assertEqual(agent_settings.validate(self.home, self.root, {cost_cap.KEY: 5}),
                         {cost_cap.KEY: 5.0})
        self.assertEqual(agent_settings.validate(self.home, self.root, {cost_cap.KEY: 0}),
                         {cost_cap.KEY: 0.0})
        for bad in (-1, "5", True, [5]):
            with self.assertRaises(agent_settings.SettingsError) as cm:
                agent_settings.validate(self.home, self.root, {cost_cap.KEY: bad})
            self.assertIn(cost_cap.KEY, cm.exception.errors)

    def test_describe_shows_it_off_by_default(self):
        from cousin_lib import agent_settings
        row = agent_settings.describe(self.home, self.root)["settings"][cost_cap.KEY]
        self.assertEqual((row["value"], row["type"], row["restart"]), (0, "usd", False))


class TestTelegramNotice(HermeticCase):
    def test_the_notice_is_relayed_once_on_its_day(self):
        from cousin_lib import telegram
        home = temp_home(self)
        cfg = mock.Mock(home=home, operator_ids={4242}, slug="wren")
        state, sent = {"tg_offset": 0, "threads": {}}, []
        send = lambda **kw: sent.append(kw)
        self.assertFalse(telegram.relay_cap_notice(cfg, state, tg_send_text=send))   # no file
        cost_cap._notice_once(home, slug="wren", limit=2.0, spent=2.5, now=time.time())
        self.assertTrue(telegram.relay_cap_notice(cfg, state, tg_send_text=send))
        self.assertFalse(telegram.relay_cap_notice(cfg, state, tg_send_text=send))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], 4242)
        self.assertIn("wren: daily cost cap reached, $2.50 of $2.00 today (UTC)", sent[0]["text"])
        telegram.save_cursors(home, state)
        self.assertEqual(telegram.load_cursors(home)["cap_notified_day"], usage.utc_day())

    def test_a_past_days_file_is_not_relayed(self):
        from cousin_lib import telegram
        home = temp_home(self)
        cfg = mock.Mock(home=home, operator_ids={4242}, slug="wren")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
        cost_cap._notice_once(home, slug="wren", limit=2.0, spent=2.5, now=yesterday)
        sent = []
        self.assertFalse(telegram.relay_cap_notice(cfg, {}, tg_send_text=lambda **kw: sent.append(kw)))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
