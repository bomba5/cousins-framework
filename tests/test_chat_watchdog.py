"""The chat-server watchdog.

A cousin's chat server is started detached by spawn or flip and nothing
supervises it; when it dies the cousin goes silent on the chat surface
until someone notices. The watchdog is one idempotent ensure pass for a
10-minute timer. Pinned here: the pure decision table, the one-pass run
with every probe injected, dry-run, the lock, the spawn wait, and the
property that the module has no kill path at all.
"""
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from cousin_lib import chat_watchdog as W


def _make_fleet(root, cousins):
    """cousins: {slug: port or None}."""
    for slug, port in cousins.items():
        home = root / "cousins" / slug
        home.mkdir(parents=True)
        chat = "[chat]\nport = %d\n" % port if port else ""
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n%s' % (slug, slug.title(), chat))


class TestEnsureAction(unittest.TestCase):
    def test_decision_table(self):
        a = W.ensure_action
        # stopped cousin or no port: never touched
        self.assertEqual(a(has_tmux=False, port=8090, health_ok=False,
                           port_in_use=False), "skip")
        self.assertEqual(a(has_tmux=False, port=8090, health_ok=True,
                           port_in_use=True), "skip")
        self.assertEqual(a(has_tmux=True, port=None, health_ok=False,
                           port_in_use=False), "skip")
        self.assertEqual(a(has_tmux=True, port=0, health_ok=False,
                           port_in_use=False), "skip")
        # healthy: leave it alone
        self.assertEqual(a(has_tmux=True, port=8090, health_ok=True,
                           port_in_use=True), "ok")
        # occupied but sick: alert only, never kill blind
        self.assertEqual(a(has_tmux=True, port=8090, health_ok=False,
                           port_in_use=True), "alert")
        # dead server, free port: the one case that acts
        self.assertEqual(a(has_tmux=True, port=8090, health_ok=False,
                           port_in_use=False), "spawn")

    def test_every_action_is_one_of_the_four(self):
        seen = set()
        for has_tmux in (True, False):
            for port in (None, 8090):
                for health_ok in (True, False):
                    for port_in_use in (True, False):
                        seen.add(W.ensure_action(
                            has_tmux=has_tmux, port=port,
                            health_ok=health_ok, port_in_use=port_in_use))
        self.assertEqual(seen, set(W.ACTIONS))


class TestNeverKills(unittest.TestCase):
    def test_module_has_no_kill_path(self):
        src = pathlib.Path(W.__file__).read_text().lower()
        # the docstring may say "never kill"; nothing else may
        stripped = src.replace("never kill", "").replace("no kill", "")
        self.assertNotIn("kill", stripped)
        self.assertNotIn("sigterm", stripped)
        self.assertNotIn("terminate(", stripped)


class WatchdogCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FRAMEWORK_ROOT", None)
        self.out, self.err = io.StringIO(), io.StringIO()

    def _run(self, **kw):
        kw.setdefault("sleep", lambda s: None)
        with contextlib.redirect_stdout(self.out), \
                contextlib.redirect_stderr(self.err):
            return W.watchdog_run(self.root, **kw)

    def _pass(self, **kw):
        kw.setdefault("sleep", lambda s: None)
        with contextlib.redirect_stdout(self.out), \
                contextlib.redirect_stderr(self.err):
            return W.ensure_pass(self.root, **kw)


class TestEnsurePass(WatchdogCase):
    def test_one_pass_over_the_fleet_with_every_probe_injected(self):
        _make_fleet(self.root, {"testa": 8090, "testb": 8091,
                                "testc": None, "testd": 8093,
                                "teste": 8094})
        tmux = {"testa", "testc", "testd", "teste"}     # testb stopped
        healthy = {8090}                                # testa fine
        occupied = {8090, 8093}                         # testd squatted
        spawned = []

        def spawn(home):
            spawned.append(home)
            healthy.add(8094)                           # it comes up
            return True

        results = self._pass(has_tmux=lambda s: s in tmux,
                             health=lambda port, slug: port in healthy,
                             port_in_use=lambda port: port in occupied,
                             spawn=spawn)
        self.assertEqual(spawned, [self.root / "cousins" / "teste"])
        actions = {r["slug"]: r["action"] for r in results}
        self.assertEqual(actions, {"testa": "ok", "testb": "skip",
                                   "testc": "skip", "testd": "alert",
                                   "teste": "spawn"})
        self.assertEqual(W.exit_code(results), 1,
                         "an alert makes the pass non-zero")
        err = self.err.getvalue()
        self.assertIn("ALERT testd", err)
        self.assertIn("8093", err)
        self.assertIn("spawned teste on :8094", self.out.getvalue())

    def test_all_quiet_exits_zero_and_touches_nothing(self):
        _make_fleet(self.root, {"testa": 8090, "testb": 8091})
        spawned = []
        rc = self._run(has_tmux=lambda s: True,
                       health=lambda port, slug: True,
                       port_in_use=lambda port: True,
                       spawn=lambda home: spawned.append(home) or True)
        self.assertEqual(rc, 0)
        self.assertEqual(spawned, [])

    def test_stopped_cousin_is_never_probed(self):
        # No tmux session: the port is not even looked at, so a dead
        # port on a stopped cousin cannot become a spawn.
        _make_fleet(self.root, {"testa": 8090})
        probed = []
        rc = self._run(has_tmux=lambda s: False,
                       health=lambda port, slug: probed.append("h") or False,
                       port_in_use=lambda port: probed.append("p") or False,
                       spawn=lambda home: probed.append("s") or True)
        self.assertEqual(rc, 0)
        self.assertEqual(probed, [])

    def test_alert_never_spawns(self):
        _make_fleet(self.root, {"testa": 8090})
        spawned = []
        rc = self._run(has_tmux=lambda s: True,
                       health=lambda port, slug: False,
                       port_in_use=lambda port: True,
                       spawn=lambda home: spawned.append(home) or True)
        self.assertEqual(rc, 1)
        self.assertEqual(spawned, [])

    def test_health_is_asked_for_this_cousins_slug(self):
        _make_fleet(self.root, {"testa": 8090})
        asked = []
        self._run(has_tmux=lambda s: True,
                  health=lambda port, slug: asked.append((port, slug)) or True,
                  port_in_use=lambda port: True,
                  spawn=lambda home: True)
        self.assertEqual(asked, [(8090, "testa")])

    def test_tmux_is_asked_for_the_configured_session_name(self):
        home = self.root / "cousins" / "testa"
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n[chat]\nport = 8090\n'
            'tmux_session = "testa-agent"\n')
        asked = []
        self._run(has_tmux=lambda s: asked.append(s) or False,
                  health=lambda port, slug: True,
                  port_in_use=lambda port: True,
                  spawn=lambda home: True)
        self.assertEqual(asked, ["testa-agent"])

    def test_a_raised_probe_isolates_to_that_cousin(self):
        _make_fleet(self.root, {"testa": 8090, "testb": 8091})

        def health(port, slug):
            if slug == "testa":
                raise RuntimeError("probe blew up")
            return True

        results = self._pass(has_tmux=lambda s: True, health=health,
                             port_in_use=lambda port: True,
                             spawn=lambda home: True)
        self.assertEqual(W.exit_code(results), 1)
        self.assertIn("testa", self.err.getvalue())
        self.assertEqual([(r["slug"], r["action"]) for r in results],
                         [("testa", "error"), ("testb", "ok")])


class TestTheRunnerLane(WatchdogCase):
    """A runner cousin has no tmux session and the supervisor runs no chat
    server: this pass is what brings its chat server back after a reboot
    or a crash (phase 7b review round 2, C2)."""

    def _fleet(self):
        _make_fleet(self.root, {"testa": 8090, "testb": 8091})
        for slug in ("testa", "testb"):
            with open(self.root / "cousins" / slug / "cousin.toml", "a") as fh:
                fh.write('\n[agent]\nrunner = "sdk"\n')

    def test_a_running_runner_cousins_dead_chat_server_is_spawned(self):
        self._fleet()
        spawned, asked_tmux = [], []

        def spawn(home):
            spawned.append(home.name)
            return True
        results = self._pass(has_tmux=lambda s: asked_tmux.append(s) or False,
                             runner_alive=lambda home: home.name == "testa",
                             health=lambda port, slug: bool(spawned),
                             port_in_use=lambda port: False, spawn=spawn)
        self.assertEqual({r["slug"]: r["action"] for r in results},
                         {"testa": "spawn", "testb": "skip"})    # testb's runner is stopped
        self.assertEqual(spawned, ["testa"])
        self.assertEqual(asked_tmux, [])                         # never tmux on this lane

    def test_the_default_liveness_is_the_runners_lock(self):
        self._fleet()
        home = self.root / "cousins" / "testa"
        self.assertTrue(W._default_runner_lane(home))
        with mock.patch("cousin_lib.delivery.is_alive", return_value=True) as alive:
            self.assertTrue(W._default_runner_alive(home))
        alive.assert_called_once_with(home)


class TestSpawnWait(WatchdogCase):
    def test_spawn_that_never_answers_within_the_wait_is_non_zero(self):
        _make_fleet(self.root, {"testa": 8090})
        slept = []
        rc = self._run(has_tmux=lambda s: True,
                       health=lambda port, slug: False,
                       port_in_use=lambda port: False,
                       spawn=lambda home: True,
                       sleep=slept.append)
        self.assertEqual(rc, 1)
        self.assertAlmostEqual(sum(slept), W.SPAWN_WAIT_S, delta=W.POLL_S)
        self.assertIn("no answer", self.out.getvalue())

    def test_spawn_that_answers_early_stops_polling(self):
        _make_fleet(self.root, {"testa": 8090})
        calls = {"n": 0}
        slept = []

        def health(port, slug):
            calls["n"] += 1
            return calls["n"] >= 3

        rc = self._run(has_tmux=lambda s: True, health=health,
                       port_in_use=lambda port: False,
                       spawn=lambda home: True, sleep=slept.append)
        self.assertEqual(rc, 0)
        self.assertLess(sum(slept), W.SPAWN_WAIT_S)

    def test_spawn_that_fails_to_launch_is_non_zero_without_waiting(self):
        _make_fleet(self.root, {"testa": 8090})
        slept = []
        rc = self._run(has_tmux=lambda s: True,
                       health=lambda port, slug: False,
                       port_in_use=lambda port: False,
                       spawn=lambda home: False, sleep=slept.append)
        self.assertEqual(rc, 1)
        self.assertEqual(slept, [])


class TestDryRun(WatchdogCase):
    def test_dry_run_prints_decisions_and_spawns_nothing(self):
        _make_fleet(self.root, {"testa": 8090, "testb": 8091,
                                "testc": 8092})
        spawned = []
        rc = self._run(dry_run=True,
                       has_tmux=lambda s: s != "testc",
                       health=lambda port, slug: port == 8090,
                       port_in_use=lambda port: port == 8090,
                       spawn=lambda home: spawned.append(home) or True)
        self.assertEqual(rc, 0)
        self.assertEqual(spawned, [])
        out = self.out.getvalue()
        self.assertIn("testa: ok", out)
        self.assertIn("testb: spawn", out)
        self.assertIn("would spawn", out)
        self.assertIn("testc: skip", out)

    def test_dry_run_still_reports_an_alert(self):
        _make_fleet(self.root, {"testa": 8090})
        rc = self._run(dry_run=True,
                       has_tmux=lambda s: True,
                       health=lambda port, slug: False,
                       port_in_use=lambda port: True,
                       spawn=lambda home: True)
        self.assertEqual(rc, 1)
        self.assertIn("testa: alert", self.out.getvalue())


class TestLock(WatchdogCase):
    def test_lock_lives_under_the_root_data_dir(self):
        self.assertEqual(W.lock_path(self.root),
                         self.root / "data" / "chat-watchdog.lock")

    def test_held_lock_exits_zero_with_a_line_and_probes_nothing(self):
        import fcntl
        _make_fleet(self.root, {"testa": 8090})
        (self.root / "data").mkdir()
        holder = open(W.lock_path(self.root), "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        probed = []
        rc = self._run(has_tmux=lambda s: probed.append(s) or True,
                       health=lambda port, slug: True,
                       port_in_use=lambda port: True,
                       spawn=lambda home: True)
        self.assertEqual(rc, 0)
        self.assertEqual(probed, [])
        self.assertIn("another pass is running", self.err.getvalue())

    def test_lock_is_released_after_a_pass(self):
        import fcntl
        _make_fleet(self.root, {"testa": 8090})
        self._run(has_tmux=lambda s: True, health=lambda p, s: True,
                  port_in_use=lambda p: True, spawn=lambda h: True)
        with open(W.lock_path(self.root), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise


class TestDefaultProbes(unittest.TestCase):
    def test_tmux_argv_is_an_exact_session_match_on_the_configured_socket(self):
        self.assertEqual(
            W.tmux_argv("testa", tmux_bin="tmux", tmux_socket=None),
            ["tmux", "has-session", "-t", "=testa"])
        self.assertEqual(
            W.tmux_argv("testa", tmux_bin="/opt/bin/tmux",
                        tmux_socket="/run/x/sock"),
            ["/opt/bin/tmux", "-S", "/run/x/sock", "has-session", "-t",
             "=testa"])

    def test_health_requires_status_ok_and_this_slug(self):
        self.assertTrue(W.health_answers(
            {"status": "ok", "slug": "testa", "port": 8090}, "testa"))
        self.assertFalse(W.health_answers(
            {"status": "ok", "slug": "testb", "port": 8090}, "testa"))
        self.assertFalse(W.health_answers({"status": "down"}, "testa"))
        self.assertFalse(W.health_answers("not json", "testa"))

    def test_default_spawn_runs_the_chat_server_detached_with_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "cousins" / "testa"
            home.mkdir(parents=True)
            with mock.patch("cousin_lib.chat_watchdog.subprocess.Popen") \
                    as popen:
                ok = W._default_spawn(home)
            self.assertTrue(ok)
            args, kwargs = popen.call_args
            argv = args[0]
            self.assertEqual(argv[0], sys.executable)
            self.assertEqual(argv[1:], ["-m", "cousin_lib.server.app",
                                        "--home", str(home)])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["stdin"], W.subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"].name,
                             str(home / "data" / "chat-server.log"))
            self.assertTrue((home / "data" / "chat-server.log").exists())

    def test_default_spawn_reports_a_launch_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "cousins" / "testa"
            home.mkdir(parents=True)
            with mock.patch("cousin_lib.chat_watchdog.subprocess.Popen",
                            side_effect=OSError("no interpreter")):
                self.assertFalse(W._default_spawn(home))


class TestMain(unittest.TestCase):
    def test_no_root_is_a_usage_error(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), \
                tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            os.environ.pop("FRAMEWORK_ROOT", None)
            with contextlib.redirect_stderr(err):
                rc = W.watchdog_main([])
        self.assertEqual(rc, 2)
        self.assertIn("--root", err.getvalue())

    def test_unwritable_root_is_a_configuration_error_not_a_traceback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = W.watchdog_main(["--root", "/proc/no-such-root"])
        self.assertEqual(rc, 2)
        self.assertIn("cousin-chat-watchdog:", err.getvalue())

    def test_dry_run_flag_reaches_the_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _make_fleet(root, {"testa": 8090})
            seen = {}

            def fake_run(r, *, dry_run=False, **kw):
                seen["root"] = pathlib.Path(r)
                seen["dry_run"] = dry_run
                return 0

            with mock.patch("cousin_lib.chat_watchdog.watchdog_run",
                            fake_run):
                rc = W.watchdog_main(["--dry-run", "--root", tmp])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"root": root, "dry_run": True})


if __name__ == "__main__":
    unittest.main()
