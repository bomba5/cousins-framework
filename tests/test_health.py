"""The health record (cousin_lib/health.py) and what feeds it: the loops
tick's report["health"], folded into data/health.json once per tick by
the daemon, read back by cousin-health. The first case is the incident it
exists for: a dreaming pass that fails on every tick, which used to be
one log line per tick and nothing else."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import health, loops
from tests._stub_supervisor import StubSupervisor


class _RootCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        patcher = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cousin(self, slug, extra=""):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n[chat]\nport = 8100\n'
            '[heartbeat]\ncontext_beat_seconds = 0\n[lifecycle]\nflip_at = "never"\n%s'
            % (slug, slug.capitalize(), extra))
        return home


class TestDreamingFailureStreak(_RootCase):
    """The done check: N failing passes on N ticks are one entry with
    fails == N and since == the first tick; one good pass resets it."""

    def setUp(self):
        super().setUp()
        self.cousin("wren")
        loops._dream_worker.update(thread=None, queue=[], inflight=set(), done=[])
        self.addCleanup(loops._dream_worker.update, thread=None, queue=[],
                        inflight=set(), done=[])
        self.verdict = {"result": "error", "error": "ImportError: no module named\n  'dream_x'"}
        real = loops.schedule_dreams

        def inline(now, report, *, homes, root, **_kw):
            return real(now, report, homes=homes, root=root,
                        run=lambda home, root, trigger: dict(self.verdict), background=False)
        for target, value in (("cousin_lib.loops.schedule_dreams", inline),
                              ("cousin_lib.dreaming.due", lambda home, when: "nightly")):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def tick(self, now):
        report = loops.tick(deliver=lambda slug, text: True, is_alive=lambda slug: True,
                            now=now, dreams=True)
        health.record(self.root, report["health"], now=now)
        return report

    def test_every_failing_tick_counts_and_one_done_resets(self):
        t0 = 1_790_000_000.0
        ticks = 5
        for i in range(ticks):
            self.tick(t0 + 30 * i)
        entry = health.read(self.root)["dreaming:wren"]
        self.assertEqual(entry["state"], "failing")
        self.assertEqual(entry["fails"], ticks)
        self.assertEqual(entry["since"], t0)
        self.assertEqual(entry["last_fail"], t0 + 30 * (ticks - 1))
        self.assertIsNone(entry["last_ok"])
        self.assertEqual(entry["error"], "ImportError: no module named 'dream_x'")
        body = health.summary(self.root, now=t0 + 30 * ticks, supervisor=None)
        self.assertEqual([r["key"] for r in body["failing"]], ["dreaming:wren"])
        self.assertIn("cousin:wren", body["ok"])
        self.assertIn("dream-due:wren", body["ok"])

        self.verdict = {"result": "done", "tokens": 900}
        self.tick(t0 + 30 * ticks)
        entry = health.read(self.root)["dreaming:wren"]
        self.assertEqual((entry["state"], entry["fails"], entry["since"]), ("ok", 0, None))
        self.assertEqual(entry["last_ok"], t0 + 30 * ticks)
        self.assertEqual(entry["last_fail"], t0 + 30 * (ticks - 1))

    def test_lost_fails_and_budget_is_a_result_not_a_fault(self):
        self.assertEqual(loops.dream_outcome({"result": "lost"}),
                         (False, "pass ended lost"))
        self.assertEqual(loops.dream_outcome({"result": "error", "error": "boom"}),
                         (False, "boom"))
        for result in ("done", "no_change", "budget"):
            self.assertEqual(loops.dream_outcome({"result": result}), (True, None))
        self.verdict = {"result": "budget", "tokens": 70000}
        self.tick(1_790_000_000.0)
        self.assertEqual(health.read(self.root)["dreaming:wren"]["state"], "ok")


class TestTickHealth(_RootCase):
    """The keys the tick reports, ok and failing, without touching the
    report's other keys."""

    def test_a_failing_delivery_and_a_bad_loop_file(self):
        self.cousin("wren", '[[loops]]\nname = "report"\ninterval_seconds = 60\n'
                            'prompt = "the daily report"\n')
        self.cousin("kestrel", '[[loops]]\nname = "x"\n')
        report = loops.tick(deliver=lambda slug, text: False,
                            is_alive=lambda slug: True, now=1_790_000_000.0)
        got = {key: (ok, error) for key, ok, error in report["health"]}
        self.assertEqual(got["delivery:wren"],
                         (False, "delivery failed for wren; loops stay due"))
        self.assertEqual(got["loop:wren|report"], (True, None))
        self.assertEqual(got["cousin:wren"], (True, None))
        self.assertFalse(got["cousin:kestrel"][0])
        self.assertIn("kestrel", got["cousin:kestrel"][1])
        for key in ("requests", "schedules", "meetings", "distill:wren"):
            self.assertEqual(got[key], (True, None), key)
        self.assertEqual(report["fired"], [])
        self.assertIn("delivery failed for wren; loops stay due", report["errors"])

    def test_a_walk_that_raises_fails_its_cousin(self):
        self.cousin("wren")
        with mock.patch("cousin_lib.loops.load_cousin_loops",
                        side_effect=RuntimeError("disk gone")):
            report = loops.tick(deliver=lambda slug, text: True,
                                is_alive=lambda slug: True, now=1_790_000_000.0)
        got = {key: (ok, error) for key, ok, error in report["health"]}
        self.assertEqual(got["cousin:wren"], (False, "RuntimeError: disk gone"))
        self.assertIn("wren: disk gone", report["errors"])


class TestDaemonWritesTheRecord(_RootCase):
    def run_main(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = loops.loops_main(argv)
        return rc, err.getvalue()

    def test_each_tick_is_recorded(self):
        self.cousin("wren")
        rc, _ = self.run_main(["run", "--ticks", "2", "--interval", "0"])
        self.assertEqual(rc, 0)
        data = health.read(self.root)
        self.assertEqual(data["tick"]["state"], "ok")
        self.assertEqual(data["cousin:wren"]["state"], "ok")

    def test_a_health_write_that_fails_never_breaks_the_tick(self):
        self.cousin("wren")
        with mock.patch("cousin_lib.health.record", side_effect=OSError("read-only")):
            rc, err = self.run_main(["run", "--ticks", "2", "--interval", "0"])
        self.assertEqual(rc, 0)
        self.assertIn("cousin-loops: health record: read-only", err)
        self.assertIsNotNone(loops._load_state().get("last_tick"))

    def test_a_tick_that_raises_is_on_record_before_the_daemon_exits(self):
        with mock.patch("cousin_lib.loops.tick", side_effect=RuntimeError("store locked")):
            with self.assertRaises(RuntimeError):
                self.run_main(["run", "--ticks", "1", "--interval", "0"])
        entry = health.read(self.root)["tick"]
        self.assertEqual((entry["state"], entry["error"]),
                         ("failing", "RuntimeError: store locked"))


class TestStore(_RootCase):
    def test_round_trip_and_reset(self):
        health.record(self.root, [("meetings", True, None), ("schedules", False, "x")], now=100)
        health.record(self.root, [("schedules", False, "y")], now=130)
        data = health.read(self.root)
        self.assertEqual(data["meetings"], {"state": "ok", "fails": 0, "since": None,
                                            "last_ok": 100, "last_fail": None, "error": "",
                                            "seen": 100})
        self.assertEqual(data["schedules"], {"state": "failing", "fails": 2, "since": 100,
                                             "last_ok": None, "last_fail": 130, "error": "y",
                                             "seen": 130})
        health.record(self.root, [("schedules", True, None)], now=160)
        entry = health.read(self.root)["schedules"]
        self.assertEqual((entry["state"], entry["fails"], entry["since"], entry["last_ok"]),
                         ("ok", 0, None, 160))
        self.assertEqual(entry["error"], "y")      # the last error stays readable

    def test_one_key_twice_in_one_round_counts_once_and_a_failure_wins(self):
        health.record(self.root, [("delivery:wren", True, None),
                                  ("delivery:wren", False, "first"),
                                  ("delivery:wren", False, "second")], now=100)
        entry = health.read(self.root)["delivery:wren"]
        self.assertEqual((entry["state"], entry["fails"], entry["error"]),
                         ("failing", 1, "first"))

    def test_the_error_is_one_line_and_capped(self):
        health.record(self.root, [("tick", False, "a\n\n  b\t" + "c" * 1000)], now=1)
        error = health.read(self.root)["tick"]["error"]
        self.assertTrue(error.startswith("a b ccc"))
        self.assertEqual(len(error), health.ERROR_CHARS)

    def test_a_missing_or_corrupt_file_starts_empty(self):
        self.assertEqual(health.read(self.root), {})
        path = self.root / "data" / "health.json"
        path.parent.mkdir(parents=True)
        for junk in ("{not json", "[1, 2]", ""):
            path.write_text(junk)
            self.assertEqual(health.read(self.root), {})
        health.record(self.root, [("tick", True, None)], now=5)
        self.assertEqual(json.loads(path.read_text())["tick"]["state"], "ok")
        self.assertEqual(list(path.parent.glob("health.json*.tmp")), [])

    def test_a_component_not_seen_for_a_week_is_pruned(self):
        health.record(self.root, [("loop:wren|gone", False, "x"), ("tick", True, None)], now=0)
        health.record(self.root, [("tick", True, None)], now=health.STALE_SECONDS - 1)
        self.assertIn("loop:wren|gone", health.read(self.root))
        health.record(self.root, [("tick", True, None)], now=health.STALE_SECONDS)
        self.assertEqual(set(health.read(self.root)), {"tick"})


class TestCli(_RootCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = health.health_main(argv)
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_nothing_failing_exits_0_and_says_the_supervisor_is_not_reachable(self):
        health.record(self.root, [("tick", True, None), ("meetings", True, None)])
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 0)
        self.assertNotIn("FAIL", out)
        self.assertIn("2 ok, 0 failing", out)
        self.assertIn("supervisor not reachable", out)

    def test_failing_first_then_the_count_exit_1(self):
        t0 = 1_790_000_000.0
        for i in range(3):
            health.record(self.root, [("dreaming:wren", False, "ImportError: x"),
                                      ("tick", True, None)], now=t0 + 30 * i)
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 1)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("FAIL  dreaming:wren  3x since "), lines[0])
        self.assertIn(health._when(t0), lines[0])
        self.assertTrue(lines[0].endswith("ImportError: x  (not seen since %s)"
                                          % health._when(t0 + 60)), lines[0])
        self.assertIn("1 ok, 1 failing", out)
        self.assertNotIn("ok    tick", out)
        rc, out, _ = self.run_cli(["--all"])
        self.assertIn("ok    tick  last ok ", out)
        rc, out, _ = self.run_cli(["--json"])
        self.assertEqual(rc, 1)
        body = json.loads(out)
        self.assertEqual(body["components"]["dreaming:wren"]["fails"], 3)
        self.assertEqual([r["key"] for r in body["failing"]], ["dreaming:wren"])
        self.assertFalse(body["supervisor"]["reachable"])

    def test_a_supervisor_child_not_running_is_failing(self):
        children = {"loops": {"state": "running", "since": "2026-10-03T01:00:00+00:00",
                              "reason": None},
                    "runner:kestrel": {"state": "backoff", "since": "2026-10-03T06:12:09+00:00",
                                       "reason": "exited (code 1)"}}
        stub = StubSupervisor(self.root, {"status": {"ok": True, "children": children}}).start()
        self.addCleanup(stub.close)
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  runner:kestrel  backoff since 2026-10-03T06:12:09+00:00"
                      "  exited (code 1)", out)
        self.assertNotIn("FAIL  loops", out)
        self.assertNotIn("not reachable", out)

    def test_a_held_stop_is_not_failing_and_a_login_stop_is(self):
        children = {"runner:kestrel": {"state": "stopped", "since": "2026-10-06T10:56:32+00:00",
                                       "reason": "stopped by request", "held": True},
                    "runner:wren": {"state": "stopped", "since": "2026-10-06T11:00:00+00:00",
                                    "reason": "login required (exit 4)", "held": False}}
        stub = StubSupervisor(self.root, {"status": {"ok": True, "children": children}}).start()
        self.addCleanup(stub.close)
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 1)
        self.assertNotIn("runner:kestrel", out)
        self.assertIn("FAIL  runner:wren  stopped", out)
        self.assertIn("1 failing", out)

    def test_an_empty_record_says_where_it_comes_from(self):
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 0)
        self.assertIn("no record yet", out)

    def test_bad_usage_exits_2(self):
        rc, _, err = self.run_cli(["--bogus"])
        self.assertEqual(rc, 2)
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": ""}), \
                mock.patch("cousin_lib.config.FrameworkConfig.looks_like_checkout",
                           return_value=False):
            os.environ.pop("COUSIN_HOME", None)
            rc, _, err = self.run_cli([])
        self.assertEqual(rc, 2)
        self.assertIn("cousin-health:", err)

    def test_help_states_the_exit_codes(self):
        rc, out, _ = self.run_cli(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("0 nothing is failing, 1 something is, 2 bad usage", out)


if __name__ == "__main__":
    unittest.main()
