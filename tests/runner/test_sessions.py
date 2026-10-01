"""Sessions: a primary and its side sessions in one runner. The
exit criterion (one cousin answers a peer while its primary session runs a
long task), a reply routed to the side session's own thread, the Runner
contract answered for the primary, and cousin-runner building it from
[agent.sessions]."""
import contextlib
import io
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # the `sdk` extra is optional; discovery skips, never errors
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner import main as runner_main
from cousin_lib.runner import sessions, tools
from cousin_lib.runner.base import RunnerError
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.contract.suite import RunnerContract
from tests.runner.contract.test_sdk import _scripts
from tests.runner.test_sdk import ScriptedClient, _wait, assistant, init_msg, result


def _install(case, sessions_table='peer = "own"\n'):
    """A framework root with wren (a runner cousin with side sessions) and
    testa (its peer)."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    (root / "config").mkdir()
    for slug in ("wren", "testa"):
        home = root / "cousins" / slug
        for sub in ("data", "run", "memory", "chat"):
            (home / sub).mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n\n[operator]\nname = "Priya"\n\n'
            '[agent]\nrunner = "sdk"\n\n[agent.sessions]\n%s'
            % (slug, slug.capitalize(), sessions_table if slug == "wren" else ""))
    os.environ["FRAMEWORK_ROOT"] = str(root)
    os.environ["COUSIN_HOME"] = str(root / "cousins" / "wren")
    return root / "cousins" / "wren"


def _long_task():
    """A primary turn that runs until it is interrupted."""
    return [[init_msg(session="s-main"), assistant(tool="Bash"), "WAIT_FOR_INTERRUPT"]]


def _text(query):
    return query["message"]["content"][0]["text"]


def _chat_rows(home):
    conn = sqlite3.connect(home / "data" / "chat.db")
    try:
        return conn.execute("SELECT chat_user, message FROM messages ORDER BY id").fetchall()
    finally:
        conn.close()


class SessionsCase(HermeticCase):
    def build(self, home, kinds, side_scripts, primary_scripts=None):
        self.clients = {}

        def factory_for(name, scripts):
            def factory(options):
                self.clients.setdefault(name, []).append(ScriptedClient(options, scripts))
                return self.clients[name][-1]
            return factory

        factories = {"primary": factory_for("primary", primary_scripts or _long_task())}
        for kind in kinds:
            factories[kind] = factory_for(kind, side_scripts)
        s = sessions.Sessions(home, kinds=kinds, factories=factories)
        self.addCleanup(lambda: s.stop(timeout=5))
        return s


class TestExitCriterion(SessionsCase):
    def test_one_cousin_answers_a_peer_while_its_primary_runs_a_long_task(self):
        home = _install(self)
        seen = {}

        def answer():
            seen["primary"] = s.primary.state()
            seen["op_row"] = s.inbox.get(op.inbox_id)["state"]
            seen["send"] = tools.call(s.sides["peer"].tool_context, "send",
                                      {"to": "testa", "text": "The ledger closes on Monday."})

        s = self.build(home, ("peer",), [[init_msg(session="s-peer"), ("CALL", answer),
                                          assistant(text="sent"), result(session="s-peer")]])
        with mock.patch("cousin_lib.chat.send_message", return_value={"ok": True, "id": 7}) as sm:
            s.start()
            op = s.enqueue(Item("operator:priya", "chat", "rebuild the quarterly report",
                                sender="Priya"))
            self.assertTrue(_wait(lambda: s.primary.activity()["thread_kinds"] == ["operator"]))
            peer = s.enqueue(Item("peer:testa", "chat", "when does the ledger close?",
                                  sender="Testa"))
            self.assertTrue(_wait(lambda: s.inbox.get(peer.inbox_id)["state"] == "done"))
        self.assertEqual(s.inbox.get(peer.inbox_id)["outcome"], "delivered")
        self.assertEqual(seen["primary"], "running")
        self.assertEqual(seen["op_row"], "claimed")
        self.assertFalse(seen["send"][1], seen["send"][0])
        self.assertEqual(sm.call_args.args[2], "testa")
        self.assertEqual(s.primary.state(), "running")     # the long task never paused
        first = _text(self.clients["peer"][0].queries[0])
        self.assertIn("Primary session: running, in a turn on operator threads", first)
        self.assertNotIn("quarterly", first)                # never the primary's words
        s.interrupt()
        self.assertTrue(_wait(lambda: s.inbox.get(op.inbox_id)["state"] == "done"))


class TestReplyRouting(SessionsCase):
    def test_a_reply_from_a_side_session_routes_to_its_own_thread(self):
        home = _install(self, 'person = "own"\n')
        out = {}

        def both_reply():
            out["side"] = tools.call(s.sides["person"].tool_context, "reply",
                                     {"text": "from the side session"})
            out["primary"] = tools.call(s.primary.tool_context, "reply",
                                        {"text": "from the primary session"})

        s = self.build(home, ("person",), [[init_msg(session="s-person"), ("CALL", both_reply),
                                            assistant(text="ok"), result(session="s-person")]])
        s.start()
        op = s.enqueue(Item("operator:priya", "chat", "a long one", sender="Priya"))
        self.assertTrue(_wait(lambda: s.primary.activity()["thread_kinds"] == ["operator"]))
        person = s.enqueue(Item("person:mallory", "chat", "hello?", sender="Mallory"))
        self.assertTrue(_wait(lambda: s.inbox.get(person.inbox_id)["state"] == "done"))
        self.assertFalse(out["side"][1], out["side"][0])
        self.assertFalse(out["primary"][1], out["primary"][0])
        self.assertEqual(_chat_rows(home), [("mallory", "from the side session"),
                                            ("priya", "from the primary session")])
        s.interrupt()
        self.assertTrue(_wait(lambda: s.inbox.get(op.inbox_id)["state"] == "done"))


class TestSessionsAsARunner(SessionsCase):
    def test_every_session_stops(self):
        home = _install(self)
        s = self.build(home, ("peer",), [], primary_scripts=[])
        s.start()
        s.stop(timeout=5)
        self.assertEqual(set(s.states().values()), {"stopped"})

    def test_a_side_session_that_gives_up_is_restarted_and_the_primary_runs_on(self):
        """A side session's failure never ends the
        process and never touches the primary's running turn; the side is
        rebuilt after a backoff and answers once its CLI starts."""
        home = _install(self)
        made = []

        def flaky(options):
            client = ScriptedClient(options, [[init_msg(session="s-peer"),
                                               assistant(text="answered"),
                                               result(session="s-peer")]])
            made.append(client)
            if len(made) <= 2:              # the first two CLIs do not start
                async def connect(prompt=None):
                    raise OSError("the CLI did not start")
                client.connect = connect
            return client

        s = sessions.Sessions(home, kinds=("peer",),
                              factories={"primary": lambda o: ScriptedClient(o, _long_task()),
                                         "peer": flaky})
        self.addCleanup(lambda: s.stop(timeout=5))
        with mock.patch.object(sessions, "RESTART_BASE_S", 0.05):
            s.start()
            op = s.enqueue(Item("operator:priya", "chat", "a long task", sender="Priya"))
            self.assertTrue(_wait(lambda: s.primary.activity()["thread_kinds"] == ["operator"]))
            peer = s.enqueue(Item("peer:testa", "chat", "hello?", sender="Testa"))
            self.assertTrue(_wait(lambda: s.inbox.get(peer.inbox_id)["state"] == "done",
                                  timeout=10))
            # the rebuilt side starts, then its `side_restarted` is appended: it
            # may take and close the row first
            self.assertTrue(_wait(lambda: len([e for e in s.events() if e["kind"] == "system"
                                               and e["payload"].get("subtype")
                                               == "side_restarted"]) >= 2))
        self.assertEqual(s.inbox.get(peer.inbox_id)["outcome"], "delivered")
        self.assertTrue(s.worker_alive())
        self.assertIsNone(s.fatal)
        self.assertIsNone(runner_main._gone(s))
        self.assertEqual(s.primary.state(), "running")       # the long task never paused
        self.assertEqual(s.inbox.get(op.inbox_id)["state"], "claimed")
        gave_up = [e["payload"] for e in s.events() if e["kind"] == "error"
                   and "side session peer gave up" in e["payload"].get("error", "")]
        self.assertEqual(len(gave_up), 2)
        self.assertEqual([g["restart_in_s"] for g in gave_up], [0.05, 0.1])
        restarted = [e["payload"] for e in s.events() if e["kind"] == "system"
                     and e["payload"].get("subtype") == "side_restarted"]
        self.assertEqual([r["attempt"] for r in restarted], [1, 2])
        s.interrupt()
        self.assertTrue(_wait(lambda: s.inbox.get(op.inbox_id)["state"] == "done"))

    def test_stop_keeps_the_whole_stop_within_its_timeout(self):
        """The stop budget: `cousin-runner` gives `stop` STOP_TIMEOUT_S
        and the supervisor kills the child soon after, so the budget is the
        TOTAL, never per session: three sessions that each take 3 s to stop
        still return within a 1 s budget."""
        home = _install(self, 'peer = "own"\nperson = "own"\n')
        s = self.build(home, ("person", "peer"), [], primary_scripts=[])
        s.start()
        slow = []
        for runner in s.sessions().values():
            real = runner.stop

            def hang(*, timeout=30.0, _real=real):
                slow.append(timeout)
                time.sleep(3.0)
                _real(timeout=0.5)
            runner.stop = hang
        t0 = time.monotonic()
        s.stop(timeout=1.0)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(len(slow), 3)
        self.assertTrue(all(budget <= 1.0 for budget in slow), slow)

    def test_the_rows_a_dead_side_session_claimed_go_back(self):
        home = _install(self)
        s = self.build(home, ("peer",), [], primary_scripts=[])
        a = s.enqueue(Item("peer:testa", "chat", "one", sender="Testa"))
        b = s.enqueue(Item("operator:priya", "chat", "two", sender="Priya"))
        s.inbox.claim(kinds=("peer",), claimant="sdk-peer-dead")
        s.inbox.claim(exclude_kinds=("peer",), claimant="sdk-primary-live")
        self.assertEqual(s.inbox.requeue_claimant("sdk-peer-dead"), 1)
        self.assertEqual(s.inbox.get(a.inbox_id)["state"], "queued")
        self.assertEqual(s.inbox.get(b.inbox_id)["state"], "claimed")

    def test_sessions_reports_kind_sdk(self):
        """The `runner` head event reports `runner.kind`."""
        self.assertEqual(sessions.Sessions.kind, "sdk")

    def test_sessions_names_the_primary_first_then_each_side(self):
        home = _install(self, 'peer = "own"\nperson = "own"\n')
        s = self.build(home, ("person", "peer"), [], primary_scripts=[])
        self.assertEqual(list(s.sessions()), ["primary", "person", "peer"])
        self.assertEqual(s.primary.exclude_kinds, ("person", "peer"))
        self.assertIs(s.inbox, s.primary.inbox)


class TestStopRacesTheWatcher(SessionsCase):
    """stop() snapshots sessions() after its capped join of the
    watcher; a rebuild still in flight then must not leave a side session
    (a CLI process) running after the stop."""

    def _gives_up_once(self, home):
        made = []

        def peer(options):
            client = ScriptedClient(options, [])
            made.append(client)
            if len(made) == 1:              # the first CLI does not start
                async def connect(prompt=None):
                    raise OSError("the CLI did not start")
                client.connect = connect
            return client

        s = sessions.Sessions(home, kinds=("peer",),
                              factories={"primary": lambda o: ScriptedClient(o, []),
                                         "peer": peer})
        self.addCleanup(lambda: s.stop(timeout=5))
        return s

    def test_a_rebuild_in_flight_when_stop_runs_leaves_no_side_running(self):
        home = _install(self)
        s = self._gives_up_once(home)
        import threading
        building, stop_returned = threading.Event(), threading.Event()
        real_build = s._build_side

        def slow_build(kind):
            building.set()
            stop_returned.wait(15)          # lands after stop() took its snapshot
            fresh = real_build(kind)
            self.addCleanup(lambda: fresh.stop(timeout=5))
            return fresh

        s._build_side = slow_build
        with mock.patch.object(sessions, "RESTART_BASE_S", 0.05):
            s.start()
            self.assertTrue(building.wait(15), "the watcher never rebuilt the side")
            s.stop(timeout=5)
            stop_returned.set()
            self.assertTrue(_wait(lambda: not s._watcher.is_alive(), timeout=15))
        self.assertTrue(_wait(lambda: not any(r.worker_alive() for r in s.sessions().values()),
                              timeout=10),
                        {name: r.worker_alive() for name, r in s.sessions().items()})

    def test_a_look_after_a_stop_began_gives_up_nothing_and_rebuilds_nothing(self):
        home = _install(self)
        s = self._gives_up_once(home)
        side = s.sides["peer"]
        side.start()
        self.assertTrue(_wait(lambda: not side.worker_alive(), timeout=15))
        s._stopping.set()
        s._look_after("peer")
        self.assertEqual(s._restart_at, {})
        self.assertEqual([e for e in s.events() if e["kind"] == "error"
                          and "gave up" in e["payload"].get("error", "")], [])
        self.assertIs(s.sides["peer"], side)

    def test_a_side_rebuilt_while_stopping_begins_its_stop_before_it_starts(self):
        """A rebuild that lands after the stop began
        claims nothing for the moment it runs."""
        home = _install(self)
        s = self.build(home, ("peer",), [], primary_scripts=[])
        calls = []

        class Stub:
            def begin_stop(self):
                calls.append("begin_stop")

            def start(self):
                calls.append("start")

            def stop(self, timeout=None):
                calls.append("stop")

        def build(kind):
            s._stopping.set()                   # the stop began while the side was built
            return Stub()
        s._build_side = build
        s._restart_at["peer"] = 0.0
        s._look_after("peer")
        self.assertEqual(calls, ["begin_stop", "start", "stop"])

    def test_stop_waits_for_the_watcher_at_most_its_cap(self):
        """The watcher's join is capped at 2 * watch_s + 1 s: a look that
        hangs never holds the stop for longer."""
        home = _install(self)
        s = self.build(home, ("peer",), [], primary_scripts=[])
        import threading
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hang(kind):
            entered.set()
            release.wait(30)

        s._look_after = hang
        s.start()
        self.assertTrue(entered.wait(15))
        t0 = time.monotonic()
        s.stop(timeout=30)
        took = time.monotonic() - t0
        cap = 2 * s.watch_s + 1.0
        self.assertGreaterEqual(took, cap - 0.05)
        self.assertLess(took, cap + 5.0)
        self.assertTrue(s._watcher.is_alive())      # still in its look: left to the exit


class TestOnceSeesASideLogin(HermeticCase):
    def test_once_exits_4_when_only_a_side_session_waits_for_a_login(self):
        """The primary idle, a side session's rows queued behind a
        dead login: `--once` must say 4, not spin."""
        class Stub:
            fatal = None

            class inbox:
                @staticmethod
                def unfinished():
                    return 1

            def worker_alive(self):
                return True

            def state(self):
                return "idle"

            def login_required(self):
                return True

        import threading
        stop, out = threading.Event(), []
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.1), \
                contextlib.redirect_stderr(io.StringIO()):
            t = threading.Thread(target=lambda: out.append(runner_main._once(Stub(), stop)))
            t.start()
            t.join(3)
            stop.set()
            t.join(3)
        self.assertEqual(out, [4])


class TestOnceGivesUpOnASideThatNeverStarts(HermeticCase):
    def test_once_exits_3_when_a_side_sessions_cli_never_starts(self):
        """A side session that gives up keeps being rebuilt, so its
        rows never drain; the batch mode must end, not spin."""
        home = _install(self)

        def refuse(options):
            client = ScriptedClient(options, [])

            async def connect(prompt=None):
                raise OSError("the CLI did not start")
            client.connect = connect
            return client

        s = sessions.Sessions(home, kinds=("peer",),
                              factories={"primary": lambda o: ScriptedClient(o, []),
                                         "peer": refuse})
        self.addCleanup(lambda: s.stop(timeout=5))
        s.enqueue(Item("peer:testa", "chat", "hello?", sender="Testa"))
        import threading
        stop, out = threading.Event(), []
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.3), \
                mock.patch.object(sessions, "RESTART_BASE_S", 60.0), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            s.start()
            t = threading.Thread(target=lambda: out.append(runner_main._once(s, stop)))
            t.start()
            t.join(10)
            stop.set()
            t.join(3)
        self.assertEqual(out, [3])
        self.assertIn("a side session could not start", err.getvalue())
        self.assertTrue(s.primary.worker_alive())


class TestOnceStallClockUnderTheRealBackoff(HermeticCase):
    def test_once_exits_3_about_the_give_up_time_after_a_side_first_gives_up(self):
        """Under the real backoff (1 s, doubling) each rebuild ends the
        stall for a moment; restarting `--once`'s clock there would put exit
        3 at about 26 s, not the documented 10. The clock runs on across
        rebuilds until one connects."""
        home = _install(self)

        def refuse(options):
            client = ScriptedClient(options, [])

            async def connect(prompt=None):
                raise OSError("the CLI did not start")
            client.connect = connect
            return client

        s = sessions.Sessions(home, kinds=("peer",),
                              factories={"primary": lambda o: ScriptedClient(o, []),
                                         "peer": refuse})
        self.addCleanup(lambda: s.stop(timeout=5))
        s.enqueue(Item("peer:testa", "chat", "hello?", sender="Testa"))
        import threading
        stop, out = threading.Event(), []
        self.assertEqual(sessions.RESTART_BASE_S, 1.0)
        self.assertEqual(runner_main.ERRORED_GIVE_UP_S, 10.0)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            s.start()
            t0 = time.monotonic()
            t = threading.Thread(target=lambda: out.append(runner_main._once(s, stop)))
            t.start()
            t.join(60)
            took = time.monotonic() - t0
            stop.set()
            t.join(5)
        self.assertEqual(out, [3])
        self.assertIn("a side session could not start", err.getvalue())
        self.assertGreaterEqual(took, runner_main.ERRORED_GIVE_UP_S)
        self.assertLess(took, 18.0)


class TestOnceDrainsARebuiltSide(HermeticCase):
    def test_once_exits_0_when_a_rebuilt_side_connects_and_serves_a_slow_row(self):
        """A side that gave up once and was rebuilt, connected and
        serving is not stalled. Counting it stalled until it had been up
        RESTART_RESET_S would make `--once` exit 3 on a healthy cousin whenever a
        turn outlasted the give-up time."""
        home = _install(self)
        made = []

        def peer(options):
            client = ScriptedClient(options, [[init_msg(session="s-peer"), ("SLOW", 4.0),
                                               assistant(text="answered"),
                                               result(session="s-peer")]])
            made.append(client)
            if len(made) == 1:              # the first CLI does not start
                async def connect(prompt=None):
                    raise OSError("the CLI did not start")
                client.connect = connect
            return client

        s = sessions.Sessions(home, kinds=("peer",),
                              factories={"primary": lambda o: ScriptedClient(o, []),
                                         "peer": peer})
        self.addCleanup(lambda: s.stop(timeout=5))
        receipt = s.enqueue(Item("peer:testa", "chat", "hello?", sender="Testa"))
        import threading
        stop, out = threading.Event(), []
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 2.5), \
                mock.patch.object(sessions, "RESTART_BASE_S", 0.05), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            s.start()
            t = threading.Thread(target=lambda: out.append(runner_main._once(s, stop)))
            t.start()
            t.join(60)
            stop.set()
            t.join(5)
        self.assertEqual(out, [0], err.getvalue())
        self.assertEqual(len(made), 2)
        self.assertEqual(s.inbox.get(receipt.inbox_id)["outcome"], "delivered")
        self.assertFalse(s.side_stalled())


class TestOneCachedPrefix(SessionsCase):
    """Every input to the cached prefix is the same bytes in
    the primary and a side session: the preset prompt, the tools, the cwd,
    the model, the permission mode, the setting sources and the env."""

    def test_the_side_sessions_options_match_the_primarys(self):
        home = _install(self)
        (home.parent.parent / "config" / "law.md").write_text("Be kind. Say what you know.\n")
        (home / "self-portrait.md").write_text("# Wren\n\nWren keeps the ledger.\n")
        s = self.build(home, ("peer",), [], primary_scripts=[])
        side = s.sides["peer"]
        a, b = s.primary.options(), side.options()
        for field in ("system_prompt", "cwd", "model", "permission_mode", "setting_sources",
                      "env", "extra_args"):
            with self.subTest(field=field):
                self.assertEqual(getattr(a, field), getattr(b, field))
        self.assertEqual(a.session_store.path, b.session_store.path)   # one transcript store
        self.assertIn("Wren keeps the ledger",
                      pathlib.Path(a.extra_args["append-system-prompt-file"]).read_text())
        self.assertEqual(sorted(a.mcp_servers), sorted(b.mcp_servers))
        self.assertEqual(tools.tool_definitions(s.primary.tool_context.registry),
                         tools.tool_definitions(side.tool_context.registry))
        self.assertEqual(sorted(a.hooks), sorted(b.hooks))


class TestRunnerFor(HermeticCase):
    def test_an_sdk_cousin_with_a_side_kind_gets_sessions(self):
        home = _install(self)
        r = runner_main.runner_for(home)
        self.addCleanup(lambda: r.stop(timeout=5))
        self.assertIsInstance(r, sessions.Sessions)
        self.assertEqual(list(r.sides), ["peer"])

    def test_an_sdk_cousin_without_one_gets_the_plain_runner(self):
        home = _install(self, "")
        r = runner_main.runner_for(home)
        self.addCleanup(lambda: r.stop(timeout=5))
        # by name: another test module reloads runner.sdk, so the class object
        # this module imported may not be the one runner_for builds from
        self.assertEqual((type(r).__module__, type(r).__name__),
                         ("cousin_lib.runner.sdk", "SdkRunner"))

    def test_side_sessions_on_the_fake_runner_are_refused(self):
        home = temp_home(self, runner="fake")
        with open(home / "cousin.toml", "a") as fh:
            fh.write('\n[agent.sessions]\npeer = "own"\n')
        with self.assertRaisesRegex(RunnerError, 'side sessions need runner = "sdk"'):
            runner_main.runner_for(home)

    def test_a_bad_table_is_exit_2_naming_it(self):
        home = temp_home(self, runner="fake")
        with open(home / "cousin.toml", "a") as fh:
            fh.write('\n[agent.sessions]\noperator = "own"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            # --once: without the check a fake runner would serve until signalled
            rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 2)
        self.assertIn("[agent.sessions] operator", stderr.getvalue())


class TestSessionsContract(RunnerContract, HermeticCase):
    """The Runner contract, answered by a primary with a person side session
    (no contract item uses a person thread, so every item is the primary's)."""

    def make_runner(self, home, *, slow=False, fail_first=False):
        clients = []

        def primary(options):
            if fail_first and not clients:
                scripts = [[init_msg(), "END"]]
            else:
                scripts = _scripts(slow and not clients)
            clients.append(ScriptedClient(options, scripts))
            return clients[-1]
        return sessions.Sessions(home, kinds=("person",), drain_timeout_s=2.0,
                                 factories={"primary": primary,
                                            "person": lambda o: ScriptedClient(o, [])})


if __name__ == "__main__":
    unittest.main()
