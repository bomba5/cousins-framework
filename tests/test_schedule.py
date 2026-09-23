"""One-shot scheduler: parse, store, cancel, tick.

Real SQLite in temporary roots; the tick's delivery is an injected
seam because firing means typing into a terminal, and that mechanism
has its own module and tests.
"""
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from cousin_lib.schedule import parse_when, schedule_main, tick


class ScheduleCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        home = self.root / "cousins" / "wren"
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
        )
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "COUSIN_HOME": str(home),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = schedule_main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestParseWhen(unittest.TestCase):
    def test_in_duration_forms(self):
        now = datetime.now().astimezone()
        for text, delta in (("in 30m", timedelta(minutes=30)),
                            ("in 2h", timedelta(hours=2)),
                            ("in 7d", timedelta(days=7)),
                            ("in 45", timedelta(minutes=45))):
            ts = parse_when(text, now=now)
            self.assertEqual(ts, int((now + delta).timestamp()), text)

    def test_tomorrow_hh_mm(self):
        now = datetime.now().astimezone()
        ts = parse_when("tomorrow 06:30", now=now)
        target = datetime.fromtimestamp(ts).astimezone()
        self.assertEqual((target.hour, target.minute), (6, 30))
        self.assertEqual(target.date(), (now + timedelta(days=1)).date())

    def test_iso_forms(self):
        ts = parse_when("2030-01-02T03:04")
        dt = datetime.fromtimestamp(ts)
        self.assertEqual((dt.year, dt.hour, dt.minute), (2030, 3, 4))

    def test_garbage_raises_with_examples_in_the_message(self):
        with self.assertRaises(ValueError) as ctx:
            parse_when("whenever")
        self.assertIn("in 30m", str(ctx.exception))


class TestCli(ScheduleCase):
    def test_add_list_cancel_roundtrip(self):
        rc, out, _ = self._main(["add", "in 30m", "check the build"])
        self.assertEqual(rc, 0)
        rc, out, _ = self._main(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("check the build", out)
        self.assertIn("pending", out)
        job_id = out.split("#")[1].split()[0]
        rc, out, _ = self._main(["cancel", job_id])
        self.assertEqual(rc, 0)
        rc, out, _ = self._main(["list"])
        self.assertIn("no jobs", out)

    def test_past_target_is_rejected(self):
        rc, _, err = self._main(["add", "2001-01-01T00:00", "too late"])
        self.assertEqual(rc, 2)
        self.assertIn("past", err)

    def test_empty_prompt_is_rejected(self):
        rc, _, _ = self._main(["add", "in 5m", "   "])
        self.assertEqual(rc, 2)

    def test_cancel_missing_job_exits_one(self):
        rc, _, _ = self._main(["cancel", "999"])
        self.assertEqual(rc, 1)

    def test_jobs_are_scoped_per_cousin(self):
        self._main(["add", "in 30m", "wren job"])
        other = self.root / "cousins" / "toki"
        other.mkdir(parents=True)
        (other / "cousin.toml").write_text(
            '[cousin]\nslug = "toki"\n[chat]\nport = 8101\n'
        )
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(other)}):
            rc, out, _ = self._main(["list"])
        self.assertIn("no jobs", out)


class TestTick(ScheduleCase):
    def test_fires_due_jobs_once_and_marks_them(self):
        fired = []
        self._main(["add", "in 1s", "due job"])
        self._main(["add", "in 2h", "future job"])
        past = int(datetime.now().timestamp()) + 5
        n = tick(now_ts=past, deliver=lambda slug, prompt: fired.append(
            (slug, prompt)))
        self.assertEqual(n, 1)
        self.assertEqual(fired, [("wren", "due job")])
        # A second tick must not re-fire.
        self.assertEqual(
            tick(now_ts=past, deliver=lambda s, p: fired.append((s, p))), 0
        )
        self.assertEqual(len(fired), 1)

    def test_delivery_failure_keeps_the_job_pending(self):
        # A tick that cannot deliver must not eat the job: pending means
        # it will be retried on the next tick, and losing a scheduled
        # prompt silently is the scheduler's one unforgivable failure.
        self._main(["add", "in 1s", "fragile job"])
        past = int(datetime.now().timestamp()) + 5

        def boom(slug, prompt):
            raise RuntimeError("tmux down")

        n = tick(now_ts=past, deliver=boom)
        self.assertEqual(n, 0)
        rc, out, _ = self._main(["list"])
        self.assertIn("fragile job", out)

    def test_a_queued_delivery_marks_the_job_fired_for_a_runner_cousin(self):
        # A runner cousin's inbox put is durable: `_default_deliver` must
        # treat `queued` as fired, not keep retrying a row already kept.
        from cousin_lib import delivery, schedule
        home = self.root / "cousins" / "wren"
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '\n[agent]\nrunner = "fake"\n')
        self._main(["add", "in 1s", "due job"])
        past = int(datetime.now().timestamp()) + 5
        calls = []

        def wrapped_deliver(slug, prompt):
            accepted = schedule._default_deliver(slug, prompt)
            calls.append(accepted)
            return accepted

        with mock.patch.object(delivery, "deliver", return_value=delivery.QUEUED):
            n = tick(now_ts=past, deliver=wrapped_deliver)
        self.assertEqual(n, 1)
        self.assertEqual(calls, [True])
        self.assertEqual(schedule.list_entries(include_fired=True)[0]["status"], "fired")


class TestDefaultDeliver(ScheduleCase):
    """A runner cousin is handed the item without waiting; a tmux
    cousin's line is waited on as before."""

    def _runner_cousin(self):
        (self.root / "cousins" / "wren" / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n\n[agent]\nrunner = "fake"\n')

    def _tick(self, outcome):
        from cousin_lib import delivery, schedule
        self._main(["add", "in 1s", "due job"])
        past = int(datetime.now().timestamp()) + 5
        with mock.patch.object(delivery, "deliver", return_value=outcome) as deliver:
            n = tick(now_ts=past, deliver=schedule._default_deliver)
        status = schedule.list_entries(include_fired=True)[0]["status"]
        return n, status, deliver

    def test_an_accepted_queued_marks_the_job_fired(self):
        from cousin_lib import delivery
        self._runner_cousin()
        n, status, _ = self._tick(delivery.QUEUED)
        self.assertEqual((n, status), (1, "fired"))

    def test_a_runner_cousin_is_not_waited_on_and_a_tmux_cousin_is(self):
        from cousin_lib import delivery
        _, _, deliver = self._tick(delivery.DELIVERED)
        self.assertIs(deliver.call_args.kwargs["wait"], True)
        self._runner_cousin()
        _, _, deliver = self._tick(delivery.QUEUED)
        self.assertIs(deliver.call_args.kwargs["wait"], False)

if __name__ == "__main__":
    unittest.main()
