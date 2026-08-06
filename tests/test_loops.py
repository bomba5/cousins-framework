"""The loops daemon: docs/loops-spec.md as executable contract.

The first test is the one the SOURCE architecture cannot pass: a
request written by a different process, consumed by the daemon, its
status visible end to end. The source's bug was a write nobody reads
- state living in two processes - and if that test can pass with
split state, it is not testing the thing that is broken today.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.loops import (
    daemon_status,
    expire_stale_requests,
    list_requests,
    load_cousin_loops,
    submit_request,
    tick,
)


class LoopsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.delivered = []

    def _cousin(self, slug="wren", loops_toml="", extra=None):
        # Beats are disabled in the plain fixture (interval 0) so loop
        # tests count only their own deliveries; beat tests opt in.
        if extra is None:
            extra = "[heartbeat]\ncontext_beat_seconds = 0\n"
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n'
            '[chat]\nport = 8100\n%s%s'
            % (slug, slug.capitalize(), loops_toml, extra))
        return home

    def _deliver(self, slug, text):
        self.delivered.append((slug, text))
        return True

    def _tick(self, **kw):
        kw.setdefault("deliver", self._deliver)
        kw.setdefault("is_alive", lambda slug: True)
        return tick(**kw)


class TestCrossProcessRequests(LoopsCase):
    def test_request_written_by_another_process_is_consumed_here(self):
        # The write happens in a REAL separate interpreter - the shape
        # the source could not serve, because its request target was a
        # module dict in whichever process handled the HTTP call.
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "report"\ninterval_seconds = 99999\n'
            'prompt = "write the report"\n'))
        writer = subprocess.run(
            [sys.executable, "-c",
             "from cousin_lib.loops import submit_request;"
             "print(submit_request('fire', cousin='wren',"
             " payload={'loop': 'report'}))"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(
                pathlib.Path(__file__).resolve().parents[1])},
        )
        self.assertEqual(writer.returncode, 0, writer.stderr)
        request_id = int(writer.stdout.strip())
        pending = list_requests(status="pending")
        self.assertEqual([r["id"] for r in pending], [request_id])
        self._tick()
        self.assertTrue(any("write the report" in text
                            for _, text in self.delivered))
        row = list_requests()[0]
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["consumed_at"])
        # Exactly once: at-least-once with no upper bound is a
        # different promise from at-least-once with idempotent
        # consumption. A second tick must not re-deliver a done row.
        delivered_before = len(self.delivered)
        self._tick()
        self.assertEqual(len(self.delivered), delivered_before)

    def test_unknown_kind_is_touched_by_no_consumer(self):
        # The general form of the kind-eating bug: the consumer's rule
        # must be "mine only if proven", not "mine unless proven
        # otherwise". A kind nobody has taught a consumer about stays
        # pending - the acceptable, silent kind of absence - until its
        # TTL expires it loudly.
        self._cousin("wren")
        submit_request("teleport", cousin="wren",
                       payload={}, ttl_seconds=3600)
        self._tick()
        row = list_requests()[0]
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["consumed_at"])

    def test_unconsumed_request_expires_loudly_never_drops(self):
        self._cousin("wren")
        rid = submit_request("fire", cousin="wren",
                             payload={"loop": "x"}, ttl_seconds=0)
        time.sleep(0.02)
        expired = expire_stale_requests()
        self.assertEqual(expired, 1)
        row = list_requests()[0]
        self.assertEqual(row["status"], "expired")
        self.assertIn("missed ticks", row["reason"])
        self.assertEqual(rid, row["id"])


class TestDaemonAbsenceIsLoud(LoopsCase):
    def test_never_run_is_reported(self):
        status = daemon_status()
        self.assertFalse(status["ok"])
        self.assertIn("never run", status["message"])

    def test_stale_tick_is_reported_with_age(self):
        self._cousin("wren")
        self._tick(now=time.time() - 500)
        status = daemon_status(tick_interval=30)
        self.assertFalse(status["ok"])
        self.assertIn("down", status["message"])
        self.assertIn("s ago", status["message"])

    def test_fresh_tick_is_ok(self):
        self._cousin("wren")
        self._tick()
        self.assertTrue(daemon_status(tick_interval=30)["ok"])


class TestLoopConfig(LoopsCase):
    def test_valid_loops_parse(self):
        home = self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "daily-report"\ndaily_at = "06:00"\n'
            'days = ["mon", "fri"]\nprompt = "report"\n'))
        loops, errors = load_cousin_loops(home)
        self.assertEqual(errors, [])
        self.assertEqual(loops[0]["name"], "daily-report")

    def test_enabled_zero_disables_truthiness_not_identity(self):
        home = self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "off"\ninterval_seconds = 60\n'
            'prompt = "x"\nenabled = 0\n'))
        loops, _ = load_cousin_loops(home)
        self.assertEqual(loops, [])

    def test_two_schedule_forms_is_an_error_not_silently_dead(self):
        home = self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "confused"\ninterval_seconds = 60\n'
            'daily_at = "06:00"\nprompt = "x"\n'))
        loops, errors = load_cousin_loops(home)
        self.assertEqual(loops, [])
        self.assertIn("exactly one schedule form", errors[0])

    def test_malformed_toml_names_the_cousin_never_silent(self):
        home = self._cousin("wren")
        (home / "cousin.toml").write_text("[cousin\nbroken")
        loops, errors = load_cousin_loops(home)
        self.assertEqual(loops, [])
        self.assertTrue(errors)
        self.assertIn(str(home), errors[0])


class TestScheduleEvaluation(LoopsCase):
    def test_daily_fires_late_once_never_twice_a_day(self):
        # Late is better than skipped - and once means once.
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "daily"\ndaily_at = "06:00"\n'
            'prompt = "morning report"\n'))
        from datetime import datetime
        late_evening = datetime.now().replace(
            hour=23, minute=0, second=0).timestamp()
        self._tick(now=late_evening)
        self.assertEqual(len(self.delivered), 1)
        self._tick(now=late_evening + 60)
        self.assertEqual(len(self.delivered), 1)

    def test_interval_downtime_yields_one_catch_up_not_n(self):
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "often"\ninterval_seconds = 60\n'
            'prompt = "check"\n'))
        base = time.time()
        self._tick(now=base)                 # first fire
        self._tick(now=base + 600)           # 10 intervals later
        self.assertEqual(len(self.delivered), 2)
        self._tick(now=base + 601)           # immediately after
        self.assertEqual(len(self.delivered), 2)

    def test_two_due_loops_coalesce_into_one_delivery(self):
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "one"\ninterval_seconds = 30\n'
            'prompt = "first thing"\n'
            '[[loops]]\nname = "two"\ninterval_seconds = 30\n'
            'prompt = "second thing"\n'))
        self._tick()
        self.assertEqual(len(self.delivered), 1)
        _, text = self.delivered[0]
        self.assertIn("2 loops due this tick", text)
        self.assertIn("### one", text)
        self.assertIn("### two", text)

    def test_failed_delivery_leaves_loops_due(self):
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "x"\ninterval_seconds = 30\n'
            'prompt = "p"\n'))
        report = self._tick(deliver=lambda slug, text: False)
        self.assertEqual(report["fired"], [])
        self.assertTrue(any("stay due" in e for e in report["errors"]))
        self._tick()  # delivery works now
        self.assertEqual(len(self.delivered), 1)

    def test_cron_dom_dow_or_when_both_restricted(self):
        from datetime import datetime

        from cousin_lib.loops import cron_matches
        # 15th of the month OR Mondays. 2026-08-15 is a Saturday:
        # dom matches, dow does not - real cron fires, AND would not.
        when = datetime(2026, 8, 15, 6, 0)
        self.assertTrue(cron_matches("0 6 15 * 1", when))
        # A Monday that is not the 15th also fires.
        when = datetime(2026, 8, 17, 6, 0)
        self.assertTrue(cron_matches("0 6 15 * 1", when))
        # A Tuesday the 16th does not.
        when = datetime(2026, 8, 18, 6, 0)
        self.assertFalse(cron_matches("0 6 15 * 1", when))

    def test_cron_minute_dedupe_two_ticks_one_fire(self):
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "cronny"\ncron = "* * * * *"\n'
            'prompt = "tick"\n'))
        # Pin to a minute start so base+15 is provably the same
        # minute whatever the wall clock says.
        base = (int(time.time()) // 60) * 60 + 1
        self._tick(now=base)
        self._tick(now=base + 15)  # same minute, second shot
        self.assertEqual(len(self.delivered), 1)


class TestHeartbeat(LoopsCase):
    def _beat_cousin(self, seconds=1):
        home = self._cousin("wren", extra=(
            "[heartbeat]\ncontext_beat_seconds = %d\n" % seconds))
        (home / "CLAUDE.md").write_text("# Wren\nidentity\n")
        (home / "STATUS.md").write_text("# Wren - STATUS\n")
        return home

    def test_beat_fires_with_changed_files_inlined(self):
        home = self._beat_cousin()
        self._tick()
        self.assertEqual(len(self.delivered), 1)
        _, text = self.delivered[0]
        self.assertIn("Context heartbeat", text)
        self.assertIn("STATUS.md CHANGED", text)
        self.assertIn("# Wren - STATUS", text)

    def test_unchanged_files_yield_the_no_changes_note(self):
        home = self._beat_cousin()
        base = time.time()
        self._tick(now=base)
        self._tick(now=base + 10)
        _, text = self.delivered[1]
        self.assertNotIn("CHANGED", text)
        self.assertIn("No identity files changed", text)

    def test_delta_state_commits_after_delivery_not_before(self):
        # The source wrote the mtime state BEFORE injecting; a failed
        # inject lost the delta and the next beat lied "no changes".
        # The fix its own ready-watcher had and its backend never got.
        home = self._beat_cousin()
        base = time.time()
        self._tick(now=base, deliver=lambda s, t: False)
        self.assertEqual(self.delivered, [])
        self._tick(now=base + 10)  # delivery works now
        _, text = self.delivered[0]
        self.assertIn("STATUS.md CHANGED", text)

    def test_beat_coalesces_with_due_loops(self):
        self._beat_cousin()
        home = self.root / "cousins" / "wren"
        toml = (home / "cousin.toml").read_text()
        (home / "cousin.toml").write_text(toml + (
            '[[loops]]\nname = "extra"\ninterval_seconds = 30\n'
            'prompt = "also this"\n'))
        self._tick()
        self.assertEqual(len(self.delivered), 1)
        _, text = self.delivered[0]
        self.assertIn("Context heartbeat", text)
        self.assertIn("### extra", text)


class TestWorkers(LoopsCase):
    def _worker(self, cmd, loops_toml=None):
        (self.root / "config" / "worker-cmd").write_text(cmd + "\n")
        home = self._cousin("grinder", loops_toml=loops_toml or (
            '[[loops]]\nname = "churn"\ninterval_seconds = 30\n'
            'prompt = "process the queue"\n'),
            extra='type = "worker"\n'
                  '[heartbeat]\ncontext_beat_seconds = 0\n')
        # type belongs to [cousin]; rewrite with it in place.
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "grinder"\nname = "Grinder"\n'
            'type = "worker"\n[chat]\nport = 8101\n'
            '[heartbeat]\ncontext_beat_seconds = 0\n'
            + (loops_toml or (
                '[[loops]]\nname = "churn"\ninterval_seconds = 30\n'
                'prompt = "process the queue"\n')))
        os.environ["COUSIN_HOME"] = str(home)
        self.addCleanup(os.environ.pop, "COUSIN_HOME", None)
        return home

    def _wait_job(self, wanted, timeout=10):
        from cousin_lib.jobs import list_jobs
        deadline = time.time() + timeout
        while time.time() < deadline:
            jobs = list_jobs(spawned_by="grinder")
            if jobs and jobs[0]["status"] in wanted:
                return jobs[0]
            time.sleep(0.05)
        self.fail("no job reached %s (have: %r)"
                  % (wanted, list_jobs(spawned_by="grinder")))

    def test_worker_firing_is_a_tracked_job_with_inspected_rc(self):
        marker = self.root / "ran.txt"
        self._worker("sh -c 'echo {prompt} > %s'" % marker)
        self._tick()
        job = self._wait_job(("done",))
        self.assertEqual(job["exit_code"], 0)
        self.assertIn("process the queue", marker.read_text())
        # No tmux delivery for workers.
        self.assertEqual(self.delivered, [])

    def test_failing_worker_looks_failed(self):
        # The source marked worker fires successful before the
        # subprocess ran; a worker failing every firing looked
        # perfectly healthy.
        self._worker("sh -c 'exit 3'")
        self._tick()
        job = self._wait_job(("failed",))
        self.assertEqual(job["exit_code"], 3)

    def test_no_worker_cmd_is_an_error_and_the_loop_stays_due(self):
        self._worker("x")
        (self.root / "config" / "worker-cmd").unlink()
        report = self._tick()
        self.assertTrue(any("worker-cmd" in e for e in report["errors"]))
        # Configured later: the loop fires.
        (self.root / "config" / "worker-cmd").write_text("sh -c true\n")
        report = self._tick()
        self.assertIn("grinder|churn", report["fired"])
        # Wait for the detached runner before teardown: tempdir
        # cleanup racing a runner still writing its log and rc is a
        # flaky ERROR, and the other worker tests already wait.
        self._wait_job(("done", "failed"))


class TestFlipDrivers(LoopsCase):
    def setUp(self):
        super().setUp()
        self.flips = []

    def _tick_f(self, **kw):
        kw.setdefault("do_flip", lambda slug: self.flips.append(slug)
                      or {"ok": True})
        return self._tick(**kw)

    def _flip_cousin(self, slug, at="04:00"):
        self._cousin(slug, extra=(
            "[heartbeat]\ncontext_beat_seconds = 0\n"
            '[lifecycle]\nflip_at = "%s"\n' % at))

    def test_daily_flip_fires_late_once(self):
        self._flip_cousin("wren")
        from datetime import datetime
        evening = datetime.now().replace(hour=23, minute=0,
                                         second=0).timestamp()
        self._tick_f(now=evening)
        self.assertEqual(self.flips, ["wren"])
        self._tick_f(now=evening + 60)
        self.assertEqual(self.flips, ["wren"])

    def test_at_most_one_daily_flip_per_tick_stagger(self):
        # Boot packets must never assemble simultaneously; the tick
        # cadence is the stagger.
        self._flip_cousin("wren")
        self._flip_cousin("toki")
        from datetime import datetime
        evening = datetime.now().replace(hour=23, minute=0,
                                         second=0).timestamp()
        self._tick_f(now=evening)
        self.assertEqual(len(self.flips), 1)
        self._tick_f(now=evening + 30)
        self.assertEqual(len(self.flips), 2)

    def test_timed_flip_warns_then_fires_through_the_request_store(self):
        self._cousin("wren")
        base = time.time()
        submit_request("flip", cousin="wren",
                       payload={"fire_at": base + 300},
                       ttl_seconds=600)
        self._tick_f(now=base + 10)     # inside T-5m: warning
        self.assertTrue(any("wrap up" in t for _, t in self.delivered))
        self.assertEqual(self.flips, [])
        self._tick_f(now=base + 301)    # T-0
        self.assertEqual(self.flips, ["wren"])
        row = list_requests()[0]
        self.assertEqual(row["status"], "done")

    def test_failed_flip_marks_the_request_failed_with_reason(self):
        self._cousin("wren")
        base = time.time()
        submit_request("flip", cousin="wren",
                       payload={"fire_at": base - 1}, ttl_seconds=600)
        self._tick_f(do_flip=lambda slug: {"ok": False,
                                           "error": "preflight failed"})
        row = list_requests()[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("preflight", row["reason"])


class TestCliAndRun(LoopsCase):
    def _main(self, argv):
        import contextlib
        import io

        from cousin_lib.loops import loops_main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = loops_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_status_reports_never_run_then_ok(self):
        rc, out, _ = self._main(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("never run", out)
        self._cousin("wren")
        rc, out, _ = self._main(["run", "--ticks", "2",
                                 "--interval", "0"])
        self.assertEqual(rc, 0)
        rc, out, _ = self._main(["status"])
        self.assertEqual(rc, 0)

    def test_fire_subcommand_writes_a_visible_request(self):
        self._cousin("wren")
        rc, out, _ = self._main(["fire", "wren", "report"])
        self.assertEqual(rc, 0)
        rc, out, _ = self._main(["requests"])
        self.assertIn("pending", out)
        self.assertIn("report", out)


if __name__ == "__main__":
    unittest.main()
