"""The loops daemon: docs/reference/loops.md as executable contract.

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

from tests._hermetic import HermeticCase

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
        # The daily flip is off for the same reason: every cousin now
        # takes the install default, so a fixture that says nothing
        # would fire one and colour every unrelated report. Flip tests
        # opt in through _flip_cousin.
        if extra is None:
            extra = ('[heartbeat]\ncontext_beat_seconds = 0\n'
                     '[lifecycle]\nflip_at = "never"\n')
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
        (home / "STATUS.md").write_text(
            "# Wren - STATUS\n_gen 8 header, superseded_\n\n## Open loops\n\n- [ ] ship the descaler\n\n"
            "## Archived loops\n\n- [x] SUPERSEDED old item\n")
        return home

    def test_beat_fires_with_changed_files_inlined(self):
        home = self._beat_cousin()
        self._tick()
        self.assertEqual(len(self.delivered), 1)
        _, text = self.delivered[0]
        self.assertIn("Context heartbeat", text)
        self.assertIn("its open loops now", text)
        self.assertIn("- [ ] ship the descaler", text)

    def test_the_beat_carries_only_status_open_loops_and_pointers(self):
        # #248: no header, no archive, no identity or memory bodies; and
        # nothing the beat says calls itself authoritative
        home = self._beat_cousin()
        (home / "MEMORY.md").write_text("# Memory\nan old line\n")
        self._tick()
        _, text = self.delivered[0]
        self.assertNotIn("superseded", text)
        self.assertNotIn("SUPERSEDED old item", text)
        self.assertNotIn("identity", text)
        self.assertNotIn("an old line", text)
        self.assertIn("CLAUDE.md changed since the last heartbeat", text)
        self.assertIn("MEMORY.md changed since the last heartbeat", text)
        self.assertNotIn("AUTHORITATIVE", text)

    def test_long_open_loops_are_cut_with_a_pointer(self):
        from cousin_lib import loops
        home = self._beat_cousin()
        (home / "STATUS.md").write_text("## Open loops\n\n" + "- [ ] x\n" * 2000)
        self._tick()
        _, text = self.delivered[0]
        self.assertIn("cut at %d characters" % loops._BEAT_INLINE_CAP, text)

    def test_an_unchanged_beat_is_not_sent_and_a_later_change_is(self):
        # meeting 11 C: a beat saying "nothing changed" was most of a quiet
        # cousin's spend; the next tick looks again, so a change still lands
        home = self._beat_cousin()
        base = time.time()
        self._tick(now=base)
        self._tick(now=base + 10)
        self.assertEqual(len(self.delivered), 1)
        # the look that found nothing is on record for the console (#305)
        from cousin_lib import loops
        state = loops._load_state()
        self.assertEqual(state["beat_checked"][home.name], base + 10)
        self.assertLess(state["last_beat"][home.name], base + 10)
        (home / "STATUS.md").write_text("## Open loops\n\n- [ ] a new loop\n")
        os.utime(home / "STATUS.md", (base + 15, base + 15))
        self._tick(now=base + 20)
        self.assertEqual(len(self.delivered), 2)
        self.assertIn("a new loop", self.delivered[1][1])

    def test_a_manual_fire_with_nothing_changed_still_says_so(self):
        from cousin_lib import loops
        home = self._beat_cousin()
        self._tick()
        text, _commit = loops._compose_beat(home, time.time())
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
        self.assertIn("its open loops now", text)

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

    def _flip_cousin(self, slug, at="04:00", started="yesterday"):
        # A daily flip ends a session older than the day's flip point, so
        # the fixture's generation started yesterday unless a test says
        # otherwise; None is a cousin that has never started one.
        home = self._cousin(slug, extra=(
            "[heartbeat]\ncontext_beat_seconds = 0\n"
            '[lifecycle]\nflip_at = "%s"\n' % at))
        if started == "yesterday":
            started = self._at(0, 0) - 86400
        if started is not None:
            from cousin_lib import boot
            boot.mark_generation_start(home, now=started)
        return home

    @staticmethod
    def _at(hour, minute=0):
        from datetime import datetime
        return datetime.now().replace(hour=hour, minute=minute, second=0,
                                      microsecond=0).timestamp()

    def _flipped_on(self, slug):
        from cousin_lib import loops
        return loops._load_state().get("last_flips", {}).get(slug)

    def test_a_session_started_after_the_flip_time_is_not_flipped_that_day(self):
        # A cousin spawned or started after today's flip time: its session
        # is younger than the flip point, so the late-once catch-up must
        # not end it seconds after its first turn. Its day is done; the
        # next day's flip time is the first that applies to it.
        from datetime import date, datetime, timedelta
        self._flip_cousin("wren", started=self._at(14, 28))
        self._tick_f(now=self._at(14, 29))
        self._tick_f(now=self._at(23))
        self.assertEqual(self.flips, [])
        self.assertEqual(self._flipped_on("wren"), str(date.today()))
        tomorrow = (datetime.now() + timedelta(days=1)).replace(
            hour=23, minute=0, second=0, microsecond=0)
        self._tick_f(now=tomorrow.timestamp())
        self.assertEqual(self.flips, ["wren"])
        self.assertEqual(self._flipped_on("wren"), str(tomorrow.date()))

    def test_a_session_started_before_the_flip_time_is_flipped_late(self):
        # The daemon was down at 04:00: a session that started at 03:00
        # is older than today's flip point, so it flips late, as today's.
        from datetime import date
        self._flip_cousin("wren", started=self._at(3))
        self._tick_f(now=self._at(23))
        self.assertEqual(self.flips, ["wren"])
        self.assertEqual(self._flipped_on("wren"), str(date.today()))

    def test_a_cousin_that_never_started_is_not_flipped(self):
        from datetime import date
        self._flip_cousin("wren", started=None)
        self._tick_f(now=self._at(23))
        self.assertEqual(self.flips, [])
        self.assertEqual(self._flipped_on("wren"), str(date.today()))

    def test_one_flip_per_tick_skips_the_young_session_without_spending_the_slot(self):
        # The young session is marked done and the walk goes on: the
        # tick's one flip goes to the cousin that is due.
        self._flip_cousin("testa", started=self._at(22))
        self._flip_cousin("wren")
        self._flip_cousin("toki")
        self._tick_f(now=self._at(23))
        self.assertEqual(len(self.flips), 1)
        self.assertNotIn("testa", self.flips)
        self._tick_f(now=self._at(23) + 30)
        self.assertEqual(sorted(self.flips), ["toki", "wren"])
        self._tick_f(now=self._at(23) + 60)
        self.assertEqual(sorted(self.flips), ["toki", "wren"])

    def _seed_inbox(self, home, source, body, at):
        import sqlite3
        from cousin_lib.delivery import Item
        from cousin_lib.runner.inbox import Inbox
        inbox = Inbox(home)
        rec = inbox.put(Item("loop:daemon" if source == "loop" else "operator:priya",
                             source, body, sender="x"))
        conn = sqlite3.connect(inbox.path)
        conn.execute("UPDATE inbox SET created_at=? WHERE id=?", (at, rec))
        conn.commit()
        conn.close()

    def test_an_idle_generation_keeps_its_session(self):
        # #280: nothing but heartbeats since it started; the flip would buy
        # a handoff and a boot and carry nothing. Its day is done.
        from datetime import date
        home = self._flip_cousin("wren")
        self._seed_inbox(home, "loop", "Context heartbeat. nothing new", self._at(1))
        report = self._tick_f(now=self._at(23))
        self.assertEqual(self.flips, [])
        self.assertEqual(report["idle_flips"], ["wren"])
        self.assertEqual(self._flipped_on("wren"), str(date.today()))

    def test_an_idle_generation_whose_snapshot_moved_is_flipped(self):
        # a new rule since the session started: the flip is how it reaches it
        import json as _json
        home = self._flip_cousin("wren")
        self._seed_inbox(home, "loop", "Context heartbeat. nothing new", self._at(1))
        (home / "data" / "runner-session.json").write_text(
            _json.dumps({"session_id": "s-1", "snapshot": "0123456789abcdef"}))
        report = self._tick_f(now=self._at(23))
        self.assertEqual(self.flips, ["wren"])
        self.assertEqual(report["idle_flips"], [])

    def test_an_idle_generation_whose_snapshot_holds_keeps_its_session(self):
        import json as _json
        from cousin_lib.runner import snapshot
        home = self._flip_cousin("wren")
        self._seed_inbox(home, "loop", "Context heartbeat. nothing new", self._at(1))
        current = snapshot.fingerprint(home, home.parent.parent)
        self.assertIsNotNone(current)
        (home / "data" / "runner-session.json").write_text(
            _json.dumps({"session_id": "s-1", "snapshot": current}))
        report = self._tick_f(now=self._at(23))
        self.assertEqual(self.flips, [])
        self.assertEqual(report["idle_flips"], ["wren"])

    def test_the_fingerprint_does_not_depend_on_the_callers_environment(self):
        # a .mcp.json server whose ${VAR} one process lacks: the runner and
        # the daemon still hash the same thing
        import json as _json
        import os as _os
        from unittest import mock as _mock
        from cousin_lib.runner import snapshot
        home = self._flip_cousin("wren")
        (home / ".mcp.json").write_text(_json.dumps({"mcpServers": {"x": {
            "command": "srv", "env": {"TOKEN": "${ONLY_IN_THE_RUNNER}"}}}}))
        with _mock.patch.dict(_os.environ, {"ONLY_IN_THE_RUNNER": "1"}):
            runner_side = snapshot.fingerprint(home, home.parent.parent)
        _os.environ.pop("ONLY_IN_THE_RUNNER", None)
        self.assertEqual(snapshot.fingerprint(home, home.parent.parent), runner_side)

    def test_a_generation_that_worked_is_flipped(self):
        home = self._flip_cousin("wren")
        self._seed_inbox(home, "loop", "Context heartbeat. nothing new", self._at(1))
        self._seed_inbox(home, "chat", "can you check the NAS", self._at(2))
        report = self._tick_f(now=self._at(23))
        self.assertEqual(self.flips, ["wren"])
        self.assertEqual(report["idle_flips"], [])

    def test_an_idle_generation_does_not_spend_the_ticks_flip(self):
        idle = self._flip_cousin("aaidle")
        self._seed_inbox(idle, "loop", "Context heartbeat. nothing new", self._at(1))
        self._flip_cousin("wren")
        self._tick_f(now=self._at(23))
        self.assertEqual(self.flips, ["wren"])

    def test_daily_flip_fires_late_once(self):
        self._flip_cousin("wren")
        from datetime import datetime
        evening = datetime.now().replace(hour=23, minute=0,
                                         second=0).timestamp()
        self._tick_f(now=evening)
        self.assertEqual(self.flips, ["wren"])
        self._tick_f(now=evening + 60)
        self.assertEqual(self.flips, ["wren"])

    def test_daily_flip_skips_a_stopped_cousin(self):
        # A flip starts the agent: flipping a stopped cousin would undo
        # the operator's stop. Skipped, and the day counts as done, so
        # a start later that day is not followed by a flip.
        self._flip_cousin("wren")
        from datetime import datetime
        evening = datetime.now().replace(hour=23, minute=0,
                                         second=0).timestamp()
        self._tick_f(now=evening, is_alive=lambda slug: False)
        self.assertEqual(self.flips, [])
        self._tick_f(now=evening + 60)
        self.assertEqual(self.flips, [])

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

    def test_a_cousin_with_no_lifecycle_block_still_flips(self):
        # The defect this closes: flip_at was per cousin and nothing
        # wrote one, so a cousin spawned or migrated later never
        # flipped and nothing said so. No [lifecycle] block at all,
        # which is the shape a migrated cousin actually had.
        home = self._cousin("wren",
                            extra="[heartbeat]\ncontext_beat_seconds = 0\n")
        from cousin_lib import boot
        boot.mark_generation_start(home, now=self._at(0) - 86400)
        from datetime import datetime
        evening = datetime.now().replace(hour=23, minute=0,
                                         second=0).timestamp()
        self._tick_f(now=evening)
        self.assertEqual(self.flips, ["wren"])

    def test_never_opts_a_cousin_out(self):
        self._flip_cousin("wren", at="never")
        from datetime import datetime
        evening = datetime.now().replace(hour=23, minute=0,
                                         second=0).timestamp()
        self._tick_f(now=evening)
        self.assertEqual(self.flips, [])

    def test_timed_flip_warns_then_fires_through_the_request_store(self):
        self._flip_cousin("wren", at="never")   # isolate the request path
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


class TestOneShotsFireFromTheTick(LoopsCase):
    """docs/reference/loops.md tick step 4: the daemon fires due one-shots
    from the scheduler store. Canary: without it nothing but a
    hand-run `cousin-schedule tick` ever fires them."""

    def _add_job(self, slug, prompt, target_ts):
        from cousin_lib import schedule
        conn = schedule._db()
        try:
            cur = conn.execute(
                "INSERT INTO scheduled_jobs (cousin, target_ts, prompt,"
                " created_at) VALUES (?, ?, ?, ?)",
                (slug, int(target_ts), prompt, int(target_ts) - 60))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _status(self, job_id):
        from cousin_lib import schedule
        conn = schedule._db()
        try:
            return conn.execute(
                "SELECT status FROM scheduled_jobs WHERE id=?",
                (job_id,)).fetchone()[0]
        finally:
            conn.close()

    def test_due_one_shot_fires_from_a_loops_tick_exactly_once(self):
        self._cousin("wren")
        now = time.time()
        job = self._add_job("wren", "check the kettle", now - 5)
        self._tick(now=now)
        texts = [t for s, t in self.delivered if s == "wren"]
        self.assertEqual(len(texts), 1, self.delivered)
        # Provenance prefix is contract (schedule.tick docstring).
        self.assertTrue(texts[0].startswith("[cousin-schedule] "))
        self.assertIn("check the kettle", texts[0])
        self.assertEqual(self._status(job), "fired")
        self._tick(now=now + 30)
        self.assertEqual(len(self.delivered), 1)

    def test_future_one_shot_waits(self):
        self._cousin("wren")
        now = time.time()
        job = self._add_job("wren", "later", now + 3600)
        self._tick(now=now)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self._status(job), "pending")

    def test_failed_delivery_keeps_it_pending_and_others_still_fire(self):
        self._cousin("wren")
        self._cousin("testa")
        now = time.time()
        bad = self._add_job("wren", "first", now - 10)
        good = self._add_job("testa", "second", now - 5)

        def deliver(slug, text):
            if slug == "wren":
                raise RuntimeError("pane gone")
            self.delivered.append((slug, text))
            return True

        report = self._tick(now=now, deliver=deliver)
        self.assertEqual(self._status(bad), "pending")
        self.assertEqual(self._status(good), "fired")
        self.assertTrue(any("#%d" % bad in e for e in report["errors"]),
                        report["errors"])
        self.assertIsNotNone(report.get("scheduled"))

    def test_false_delivery_is_a_failure_not_a_fire(self):
        self._cousin("wren")
        now = time.time()
        job = self._add_job("wren", "x", now - 5)
        self._tick(now=now, deliver=lambda slug, text: False)
        self.assertEqual(self._status(job), "pending")

    def test_dead_cousin_holds_its_one_shot_until_it_returns(self):
        self._cousin("wren")
        now = time.time()
        job = self._add_job("wren", "x", now - 5)
        self._tick(now=now, is_alive=lambda slug: False)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self._status(job), "pending")
        self._tick(now=now + 30)
        self.assertEqual(self._status(job), "fired")

    def test_a_broken_scheduler_store_never_costs_the_tick(self):
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "report"\ninterval_seconds = 60\n'
            'prompt = "write the report"\n'))
        with mock.patch("cousin_lib.schedule._db",
                        side_effect=RuntimeError("disk on fire")):
            report = self._tick()
        self.assertTrue(any("write the report" in t
                            for _, t in self.delivered))
        self.assertTrue(any("disk on fire" in e for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()


class TestDefaultDeliverWait(unittest.TestCase):
    def test_a_runner_cousin_is_not_waited_on_and_a_tmux_cousin_is(self):
        import os
        import pathlib
        import tempfile
        from unittest import mock
        from cousin_lib import delivery, loops
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name); (root / "config").mkdir()
        for slug, extra in (("wren", ""), ("finch", '\n[agent]\nrunner = "fake"\n')):
            (root / "cousins" / slug).mkdir(parents=True)
            (root / "cousins" / slug / "cousin.toml").write_text(
                '[cousin]\nslug = "%s"\n[chat]\nport = 8100\n%s' % (slug, extra))
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            with mock.patch.object(delivery, "deliver", return_value=delivery.QUEUED) as deliver:
                self.assertTrue(loops._default_deliver("finch", "beat"))
            self.assertIs(deliver.call_args.kwargs["wait"], False)
            with mock.patch.object(delivery, "deliver",
                                   return_value=delivery.DELIVERED) as deliver:
                self.assertTrue(loops._default_deliver("wren", "beat"))
            self.assertIs(deliver.call_args.kwargs["wait"], True)


class TestIndexRefresh(unittest.TestCase):
    """Every cousin's index is refreshed by the daemon, by default: each
    home at most once per window, one worker, results on the report."""

    def setUp(self):
        from cousin_lib import loops
        self.loops = loops
        loops._index_worker.update(thread=None, queue=[], last={}, done=[])
        self.seen = []

    def refresh(self, home):
        self.seen.append(home)
        return {"files": 1} if home == "/h/wren" else None

    def run_tick(self, now):
        report = {}
        self.loops.schedule_index_refresh(
            now, report, homes=[("wren", "/h/wren"), ("toki", "/h/toki")],
            refresh=self.refresh, every=300, background=False)
        return report

    def test_each_home_once_per_window_and_only_changes_reported(self):
        report = self.run_tick(1000)
        self.assertEqual(self.seen, ["/h/wren", "/h/toki"])
        self.assertEqual(report["indexed"], [("wren", {"files": 1})])
        self.run_tick(1100)                  # inside the window
        self.assertEqual(len(self.seen), 2)
        self.run_tick(1301)                  # window passed
        self.assertEqual(len(self.seen), 4)

    def test_a_failing_home_does_not_stop_the_others(self):
        def refresh(home):
            if home == "/h/wren":
                raise RuntimeError("service down")
            self.seen.append(home)
        report = {}
        self.loops.schedule_index_refresh(
            0 + 10 ** 6, report,
            homes=[("wren", "/h/wren"), ("toki", "/h/toki")],
            refresh=refresh, background=False)
        self.assertEqual(self.seen, ["/h/toki"])
        self.assertEqual(report["indexed"][0][0], "wren")
        self.assertIn("error", report["indexed"][0][1])


class TestDefaultIsAliveForARunnerCousin(unittest.TestCase):
    def test_default_is_alive_for_a_runner_cousin_reads_the_lock(self):
        import pathlib
        import tempfile

        from cousin_lib import loops
        from cousin_lib.runner import main as runner_main

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home = root / "cousins" / "wren"
            home.mkdir(parents=True)
            (home / "cousin.toml").write_text(
                '[cousin]\nslug = "wren"\nname = "Wren"\n[chat]\nport = 8100\n'
                '\n[agent]\nrunner = "fake"\n')
            with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
                with mock.patch.object(runner_main, "is_running", return_value=True):
                    self.assertTrue(loops._default_is_alive("wren"))
                with mock.patch.object(runner_main, "is_running", return_value=False):
                    self.assertFalse(loops._default_is_alive("wren"))


class TestMaxAgeOnTheRunnerLane(HermeticCase):
    """max_age fires at the configured cadence
    with the stagger intact, for runner cousins."""
    def test_two_due_runner_cousins_roll_over_one_per_tick_with_reason_max_age(self):
        import contextlib
        import datetime as dt
        import pathlib
        import tempfile
        from cousin_lib import boot, loops
        from cousin_lib.runner.fake import FakeRunner
        from cousin_lib.runner.main import hold_lock
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name); (root / "config").mkdir()
        homes = []
        for slug in ("wren", "testa"):     # one root, so FrameworkConfig lists both
            home = root / "cousins" / slug
            for sub in ("data", "run", "memory"):
                (home / sub).mkdir(parents=True)
            (home / "cousin.toml").write_text(
                '[cousin]\nslug = "%s"\nname = "%s"\n\n[agent]\nrunner = "fake"\n\n'
                '[lifecycle]\nflip_at = "11:00"\n' % (slug, slug.capitalize()))
            # sessions from yesterday: older than today's 11:00, so due
            yesterday = dt.datetime.now().replace(hour=0, minute=0, second=0,
                                                  microsecond=0).timestamp() - 86400
            boot.mark_generation_start(home, now=yesterday)
            # the generation worked (#280: an idle one keeps its session)
            import sqlite3
            from cousin_lib.delivery import Item
            from cousin_lib.runner.inbox import Inbox
            inbox = Inbox(home)
            done = inbox.put(Item("operator:priya", "chat", "check the NAS", sender="Priya"))
            conn = sqlite3.connect(inbox.path)
            conn.execute("UPDATE inbox SET state='done', outcome='delivered' WHERE id=?", (done,))
            conn.commit()
            conn.close()
            homes.append(home)
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}); p.start(); self.addCleanup(p.stop)
        with contextlib.ExitStack() as stack:
            runners = []
            for home in homes:
                stack.enter_context(hold_lock(home))
                r = FakeRunner(home); r.start(); stack.callback(r.stop, timeout=5)
                runners.append(r)
            # midday, whole minute: +60 s and +120 s stay on the same day (a 23:59 tick
            # would cross midnight and flip the first cousin twice)
            state = {}
            now = dt.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
            daily = loops.daily_flip(loops._default_do_flip)
            first = {"flips": [], "errors": []}
            loops._fire_daily_flips(state, daily, lambda slug: True, now, first)
            second = {"flips": [], "errors": []}
            loops._fire_daily_flips(state, daily, lambda slug: True, now + 60, second)
            third = {"flips": [], "errors": []}
            loops._fire_daily_flips(state, daily, lambda slug: True, now + 120, third)
        self.assertEqual(len(first["flips"]), 1)                 # the stagger: one per tick
        self.assertEqual(len(second["flips"]), 1)
        self.assertEqual(third["flips"], [])                     # each once a day
        self.assertEqual(sorted(first["flips"] + second["flips"]), ["testa", "wren"])
        self.assertEqual([boot.read_generation(h) for h in homes], [1, 1])
        for r in runners:
            reasons = [e["payload"]["reason"] for e in r.events() if e["kind"] == "rollover"]
            self.assertEqual(reasons, ["max_age"])

    def test_an_injected_do_flip_is_passed_through_unchanged(self):
        from cousin_lib import loops
        calls = []
        loops.daily_flip(lambda slug: calls.append(slug) or {"ok": True})("wren")
        self.assertEqual(calls, ["wren"])


class TestLeftoverChatHostIsInert(LoopsCase):
    def test_a_leftover_chat_host_keeps_the_cousins_index_fresh(self):
        # [chat] host is a key 2.0.0 removed: it no longer makes a cousin
        # "remote" and skipped by the daemon's index refresh
        from cousin_lib import loops
        self._cousin("wren")
        self._cousin("far", loops_toml='host = "far-host"\n')
        seen = []
        with mock.patch.object(loops, "schedule_index_refresh",
                               side_effect=lambda now, report, homes: seen.extend(homes)):
            self._tick(index_refresh=True)
        self.assertEqual(sorted(slug for slug, _home in seen), ["far", "wren"])


class TestCronIsCheckedAndIsolated(LoopsCase):
    """A cron field the parser cannot read is refused when the loops are
    saved, and a bad one written by hand costs only its own loop: the
    cousin's other loops and its heartbeat still fire, and the error
    names the loop."""

    BAD = ("0 9 * * mon", "61 * * * *", "* 24 * * *", "*/0 * * * *", "* * * *",
           "1-x * * * *", "* * 0 * *", "* * * 13 *", "* * * * 8")

    def test_save_refuses_each_bad_cron_naming_the_entry(self):
        from cousin_lib.loops import save_cousin_loops, validate_loops
        home = self._cousin()
        before = (home / "cousin.toml").read_text()
        for expr in self.BAD:
            entries = [{"name": "ok", "interval_seconds": 60, "prompt": "x"},
                       {"name": "weekly", "cron": expr, "prompt": "x"}]
            errors = validate_loops(entries)
            self.assertEqual(len(errors), 1, (expr, errors))
            self.assertIn("loops[1]", errors[0])
            self.assertIn("cron", errors[0])
            with self.assertRaises(ValueError):
                save_cousin_loops(home, entries)
            self.assertEqual((home / "cousin.toml").read_text(), before)
        for expr in ("0 9 * * 1", "*/15 8-18 * * 1-5", "0 0 1,15 * 0", "30 6 * * 7"):
            self.assertEqual(validate_loops([{"name": "w", "cron": expr, "prompt": "x"}]), [],
                             expr)

    def test_a_bad_cron_written_by_hand_stops_only_its_own_loop(self):
        home = self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "weekly"\ncron = "* * * * mon"\nprompt = "never"\n\n'
            '[[loops]]\nname = "often"\ninterval_seconds = 60\nprompt = "every minute"\n\n'),
            extra='[heartbeat]\ncontext_beat_seconds = 60\n[lifecycle]\nflip_at = "never"\n')
        (home / "STATUS.md").write_text("# Wren - STATUS\n")
        report = self._tick()
        self.assertEqual(report["fired"], ["wren|often"])
        self.assertEqual(len(self.delivered), 1)
        text = self.delivered[0][1]
        self.assertIn("Context heartbeat", text)
        self.assertIn("every minute", text)
        self.assertTrue(any("weekly" in e and "cron" in e for e in report["errors"]),
                        report["errors"])


class TestNeverFiresLate(LoopsCase):
    """docs/reference/loops.md: a request still pending after its TTL is
    expired and never fires late. The first tick after downtime expires
    first, then fires what is left."""

    def test_an_over_age_timed_flip_expires_instead_of_firing(self):
        self._cousin("wren")
        flips = []
        base = time.time()
        submit_request("flip", cousin="wren", payload={"fire_at": base + 60},
                       ttl_seconds=660)
        # the daemon was down for an hour: the flip's window is long gone
        self._tick(now=base + 3600, do_flip=lambda slug: flips.append(slug) or {"ok": True})
        self.assertEqual(flips, [])
        row = list_requests()[0]
        self.assertEqual(row["status"], "expired")

    def test_an_over_age_manual_fire_expires_instead_of_delivering(self):
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "digest"\ninterval_seconds = 86400\nprompt = "digest now"\n\n'))
        base = time.time()
        submit_request("fire", cousin="wren", payload={"loop": "digest"})
        self._tick(now=base + 3600)
        self.assertFalse([t for _, t in self.delivered if "manual fire" in t], self.delivered)
        self.assertEqual(list_requests()[0]["status"], "expired")


class TestUpkeepAlarm(LoopsCase):
    """#288: hourly, a cousin with an alarm gets an `upkeep:<slug>` health row."""

    def test_the_alarm_is_a_health_row_checked_hourly(self):
        from unittest import mock as _mock
        from cousin_lib import loops, upkeep
        self._cousin("wren", extra="[agent]\nupkeep_alarm_percent = 50\n")
        calls = []
        def fake(home, now=None):
            calls.append(now)
            return False, "upkeep and its own schedules took 63%"
        with _mock.patch.object(upkeep, "alarm", side_effect=fake):
            report = self._tick(now=10_000.0)
            self.assertIn(("upkeep:wren", False, "upkeep and its own schedules took 63%"),
                          [tuple(h) for h in report["health"]])
            self._tick(now=10_060.0)                       # within the hour: no second check
            self._tick(now=10_000.0 + loops.UPKEEP_CHECK_SECONDS)
        self.assertEqual(len(calls), 2)

    def test_an_alarm_switched_off_clears_its_failing_row(self):
        from unittest import mock as _mock
        from cousin_lib import health, loops, upkeep
        home = self._cousin("wren", extra="[agent]\nupkeep_alarm_percent = 50\n")
        with _mock.patch.object(upkeep, "alarm", return_value=(False, "over")):
            report = self._tick(now=20_000.0)
        health.record(loops.FrameworkConfig.from_env().root, report["health"])
        (home / "cousin.toml").write_text((home / "cousin.toml").read_text().replace(
            "upkeep_alarm_percent = 50", "upkeep_alarm_percent = 0"))
        report = self._tick(now=20_000.0 + loops.UPKEEP_CHECK_SECONDS)
        self.assertIn(("upkeep:wren", True, None), [tuple(h) for h in report["health"]])
