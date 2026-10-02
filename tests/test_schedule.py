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
        self.assertEqual([s for s, _ in fired], ["wren"])
        self.assertTrue(fired[0][1].endswith("\n\ndue job"), fired)
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


class TestTickReadsTheReturnValue(ScheduleCase):
    """An explicit False from the deliverer is not a delivery: the job
    stays pending and on_error hears why, as for a raise. True or None
    keeps the old meaning."""

    def _tick(self, returned):
        from cousin_lib import schedule
        self._main(["add", "in 1s", "due job"])
        past = int(datetime.now().timestamp()) + 5
        errors = []
        n = tick(now_ts=past, deliver=lambda slug, prompt: returned,
                 on_error=lambda job_id, err: errors.append((job_id, err)))
        return n, schedule.list_entries(include_fired=True)[0]["status"], errors

    def test_a_false_return_keeps_the_job_pending_and_reports_it(self):
        n, status, errors = self._tick(False)
        self.assertEqual((n, status), (0, "pending"))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0][1], RuntimeError)
        self.assertEqual(str(errors[0][1]), "delivery not accepted")

    def test_a_true_return_marks_the_job_fired(self):
        self.assertEqual(self._tick(True), (1, "fired", []))

    def test_a_none_return_still_marks_the_job_fired(self):
        self.assertEqual(self._tick(None), (1, "fired", []))


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


class TestEnvelope(ScheduleCase):
    """A due job says what it is and how late it fired, and the store
    caps how many a cousin may hold pending."""

    def test_on_time_header_then_the_prompt_verbatim(self):
        from cousin_lib.schedule import annotate
        text = annotate(7, "check the kettle\nsecond line", created_ts=1000,
                        target_ts=4600, now_ts=4630)
        header, _, body = text.partition("\n\n")
        self.assertEqual(body, "check the kettle\nsecond line")
        self.assertTrue(header.startswith("#7, set "), header)
        self.assertIn("(on time)", header)
        self.assertIn("nobody is waiting on this turn", header)
        self.assertNotIn("may already be settled", header)

    def test_late_is_said_and_stale_warns(self):
        from cousin_lib.schedule import annotate
        late = annotate(1, "p", created_ts=0, target_ts=1000, now_ts=1000 + 5 * 60)
        self.assertIn("(5 min late)", late)
        self.assertNotIn("may already be settled", late)
        stale = annotate(1, "p", created_ts=0, target_ts=1000, now_ts=1000 + 3 * 3600)
        self.assertIn("(180 min late)", stale)
        self.assertIn("may already be settled", stale)

    def test_a_late_job_is_still_delivered(self):
        # at-least-once: lateness is reported, never a silent drop
        fired = []
        self._main(["add", "in 1s", "late job"])
        n = tick(now_ts=int(datetime.now().timestamp()) + 4 * 3600,
                 deliver=lambda s, p: fired.append(p))
        self.assertEqual(n, 1)
        self.assertIn("may already be settled", fired[0])
        self.assertTrue(fired[0].endswith("\n\nlate job"))

    def test_pending_jobs_are_capped_per_cousin(self):
        from cousin_lib import schedule
        for i in range(schedule.MAX_PENDING):
            schedule.add("wren", "in 2h", "job %d" % i)
        with self.assertRaisesRegex(ValueError, "cap"):
            schedule.add("wren", "in 2h", "one too many")
        schedule.add("kite", "in 2h", "another cousin is not capped by wren's")
        first = schedule.list_entries("wren")[0]["id"]
        schedule.cancel(first, slug="wren")
        schedule.add("wren", "in 2h", "room again after a cancel")
