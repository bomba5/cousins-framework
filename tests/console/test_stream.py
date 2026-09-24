"""The runner cousin's reasoning pane routes (console/stream.py): the event
stream as SSE, the interrupt, the say box. The generator is driven by an
injected sleep; the routes run through the router against a FakeRunner
that holds the cousin's lock, as cousin-runner does."""
import json
import pathlib
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from cousin_lib.console import router, sse
from cousin_lib.console import stream as console_stream
from cousin_lib.runner import main
from cousin_lib.runner.fake import FakeRunner
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase
from tests.console._harness import ConsoleCase


def _frames(gen, n):
    return [next(gen) for _ in range(n)]


def _data(frame):
    for line in frame.decode().splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


class HomeCase(HermeticCase):
    runner = "fake"

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        agent = '[agent]\nrunner = "%s"\n' % self.runner if self.runner else ""
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[chat]\nport = 1\n'
            '[operator]\nname = "Priya"\n' + agent)


class TestGenerator(HomeCase):
    def test_every_event_then_each_new_one(self):
        s = EventStream(self.home, "fake-a")
        s.append("state", {"from": "idle", "to": "running"})
        s.append("tool", {"name": "Bash"})

        def sleep(_):
            s.append("result", {"inbox_ids": [1]})
        gen = console_stream.stream_events(self.home, sleep=sleep, poll=0)
        frames = _frames(gen, 3)
        self.assertEqual([_data(f)["kind"] for f in frames], ["state", "tool", "result"])
        self.assertTrue(frames[0].startswith(b"id: fake-a:1\nevent: runner-event\n"))

    def test_after_skips_what_the_client_has(self):
        s = EventStream(self.home, "fake-a")
        for kind in ("state", "tool", "result"):
            s.append(kind, {})
        gen = console_stream.stream_events(self.home, after=2, sleep=lambda _: None, poll=0)
        self.assertEqual(_data(next(gen))["seq"], 3)

    def test_a_torn_line_waits_for_its_end(self):
        s = EventStream(self.home, "fake-a")
        s.append("state", {"to": "running"})
        path = s.path
        whole = json.dumps({"seq": 2, "ts": 0, "kind": "tool", "payload": {}}) + "\n"
        steps = iter([whole[:10], whole[10:]])

        def sleep(_):
            with open(path, "a") as fh:
                fh.write(next(steps))
        gen = console_stream.stream_events(self.home, sleep=sleep, poll=0)
        self.assertEqual([_data(f)["seq"] for f in _frames(gen, 2)], [1, 2])

    def test_a_restarted_runner_is_followed_after_the_old_file_is_drained(self):
        old = EventStream(self.home, "fake-old")
        old.append("state", {"to": "running"})
        state = {"n": 0}

        def sleep(_):
            state["n"] += 1
            if state["n"] == 1:
                old.append("state", {"to": "stopped"})
                time.sleep(0.02)
                EventStream(self.home, "fake-new").append("runner", {"kind": "fake"})
        gen = console_stream.stream_events(self.home, sleep=sleep, poll=0)
        frames = _frames(gen, 4)
        self.assertEqual(_data(frames[1])["payload"]["to"], "stopped")
        self.assertIn(b"event: session", frames[2])
        self.assertEqual(_data(frames[2]), {"session": "fake-new"})
        self.assertEqual(_data(frames[3])["kind"], "runner")

    def test_silence_is_a_ping(self):
        EventStream(self.home, "fake-a")
        clock = iter([0.0, 20.0, 40.0])
        gen = console_stream.stream_events(self.home, sleep=lambda _: None,
                                           clock=lambda: next(clock), poll=0)
        self.assertEqual(next(gen), sse.PING)


class TestResumeAndTail(HomeCase):
    """Review I3 (a reconnect after a runner restart) and I4 (a fresh
    connect is a bounded tail, not the whole file)."""

    def _file(self, session, n):
        s = EventStream(self.home, session)
        s.append("runner", {"kind": "fake", "pid": 1, "unsupported": []})
        for i in range(n - 1):
            s.append("tool", {"name": "Bash", "n": i})
        return s

    def test_a_reconnect_after_a_restart_gets_the_new_runners_events(self):
        self._file("fake-old", 50)
        time.sleep(0.02)
        self._file("fake-new", 5)
        gen = console_stream.stream_events(self.home, after=50, session="fake-old",
                                           sleep=lambda _: None, poll=0)
        first = next(gen)
        self.assertIn(b"event: session", first)
        self.assertEqual(_data(first), {"session": "fake-new"})
        frames = _frames(gen, 5)
        self.assertEqual([_data(f)["seq"] for f in frames], [1, 2, 3, 4, 5])
        self.assertTrue(frames[0].startswith(b"id: fake-new:1\n"))

    def test_a_pane_opened_before_the_runner_wrote_anything_is_told_its_session(self):
        """Phase 5 execution review (Task 5): the pane is opened while the
        runner starts, before its stream exists. When the first file
        appears, a `session` frame names it and every id carries it; a
        reconnect then resumes instead of re-receiving a tail."""
        state = {"n": 0}

        def sleep(_):
            state["n"] += 1
            if state["n"] == 2:
                self._file("fake-first", 3)
        gen = console_stream.stream_events(self.home, sleep=sleep, poll=0)
        first = next(gen)
        self.assertIn(b"event: session", first)
        self.assertEqual(_data(first), {"session": "fake-first"})
        frames = _frames(gen, 3)
        self.assertTrue(frames[0].startswith(b"id: fake-first:1\n"), frames[0][:40])
        self.assertTrue(frames[2].startswith(b"id: fake-first:3\n"), frames[2][:40])

    def test_the_label_is_the_file_follow_reads_never_a_second_lookup(self):
        """A restart between two lookups must not tag one file's events with
        the other's session: the label comes from follow alone. The old
        file holds tool events, the new one text events."""
        old = EventStream(self.home, "fake-old")
        old.append("runner", {"kind": "fake"}); old.append("tool", {"name": "Bash"})
        time.sleep(0.02)
        new = EventStream(self.home, "fake-new")
        new.append("runner", {"kind": "fake"}); new.append("text", {"text": "hi"})
        calls = {"n": 0}

        def newest(home):
            calls["n"] += 1
            return old.path if calls["n"] == 1 else new.path
        gen = console_stream.stream_events(self.home, newest=newest, sleep=lambda _: None, poll=0)
        frames = [f for f in (next(gen) for _ in range(6)) if f.startswith(b"id: ")]
        for f in frames:
            label = f.split(b"\n", 1)[0][4:].split(b":")[0].decode()
            kind = _data(f)["kind"]
            if kind == "tool":
                self.assertEqual(label, "fake-old", f[:60])
            if kind == "text":
                self.assertEqual(label, "fake-new", f[:60])

    def test_a_reconnect_to_the_same_runner_resumes_after_its_last_event(self):
        self._file("fake-a", 10)
        gen = console_stream.stream_events(self.home, after=7, session="fake-a",
                                           sleep=lambda _: None, poll=0)
        self.assertEqual([_data(f)["seq"] for f in _frames(gen, 3)], [8, 9, 10])

    def test_a_fresh_connect_is_the_tail_of_a_large_stream(self):
        s = self._file("fake-big", 1)
        pad = "x" * 4000
        for i in range(5000):                     # about 20 MB
            s.append("tool_result", {"text": pad, "n": i})
        started = time.monotonic()
        gen = console_stream.stream_events(self.home, sleep=lambda _: None, poll=0)
        first = _data(next(gen))
        self.assertEqual(first["seq"], 5001 - console_stream.TAIL_EVENTS + 1)
        self.assertLess(time.monotonic() - started, 5.0)
        seqs = [first["seq"]] + [_data(f)["seq"] for f in _frames(gen, console_stream.TAIL_EVENTS - 1)]
        self.assertEqual(seqs[-1], 5001)


class RouteCase(HomeCase):
    def setUp(self):
        super().setUp()
        router.clear()
        console_stream.register()

    def _req(self, body=None, query=None):
        return SimpleNamespace(root=self.root, query=query or {}, body=body or {},
                               user="Priya", headers={})

    def _post(self, path, **body):
        return router.dispatch("POST", path, req=self._req(body=body))

    def _running(self, turn_seconds=0.0):
        runner = FakeRunner(self.home, turn_seconds=turn_seconds)
        held, release = threading.Event(), threading.Event()

        def hold():
            with main.hold_lock(self.home):
                runner.start()
                held.set()
                release.wait(10)
                runner.stop(timeout=5)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        return runner


class TestRoutes(RouteCase):
    def test_the_stream_route_streams(self):
        status, body = router.dispatch("GET", "/api/cousins/wren/stream", req=self._req())
        self.assertEqual(status, 200)
        self.assertIsInstance(body, sse.Stream)
        body.close()

    def test_a_bad_after_is_400(self):
        status, _ = router.dispatch("GET", "/api/cousins/wren/stream",
                                    req=self._req(query={"after": "x"}))
        self.assertEqual(status, 400)

    def test_interrupt_ends_a_live_turn(self):
        runner = self._running(turn_seconds=3.0)
        runner.enqueue(__import__("cousin_lib.delivery", fromlist=["Item"]).Item(
            "operator:Priya", "chat", "slow", sender="Priya"))
        deadline = time.monotonic() + 5
        while runner.state() != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self._post("/api/cousins/wren/interrupt"),
                         (200, {"ok": True, "outcome": "delivered"}))

    def test_interrupt_with_no_turn_is_failed(self):
        self._running()
        self.assertEqual(self._post("/api/cousins/wren/interrupt"),
                         (200, {"ok": False, "outcome": "failed"}))

    def test_interrupt_with_no_runner_is_409_and_puts_nothing(self):
        status, body = self._post("/api/cousins/wren/interrupt")
        self.assertEqual((status, body["error"]), (409, "no runner is running"))
        self.assertEqual(Inbox(self.home).unfinished(), 0)

    def test_say_queues_on_the_operators_thread_and_stores_no_chat_row(self):
        self.assertEqual(self._post("/api/cousins/wren/say", text="look at the ledger"),
                         (200, {"ok": True, "outcome": "queued"}))
        [row] = Inbox(self.home).open_rows("chat")
        self.assertEqual((row["thread_id"], row["sender"], row["body"]),
                         ("operator:Priya", "Priya", "look at the ledger"))
        self.assertFalse((self.home / "data" / "chat.db").exists())

    def test_a_login_code_said_into_the_pane_is_diverted_never_delivered(self):
        """R18 (phase 4): every operator send path diverts a login code first
        (review I2); the pane's box is one."""
        from cousin_lib import accounts
        accounts.arm_capture(self.root, via="wren", operator="Priya", account_name="fleet",
                             ttl=60)
        code = "abcdefghijklmnopqrstuv#wxyz012345"
        self.assertEqual(self._post("/api/cousins/wren/say", text=code),
                         (200, {"ok": True, "outcome": "diverted"}))
        self.assertEqual(Inbox(self.home).unfinished(), 0)
        self.assertEqual(accounts.read_capture(self.root, "fleet")["code"], code)

    def test_say_needs_text(self):
        self.assertEqual(self._post("/api/cousins/wren/say", text=" ")[0], 400)


class TestTmuxCousin(RouteCase):
    runner = None

    def test_every_route_is_409_for_a_tmux_cousin(self):
        for method, path in (("GET", "/api/cousins/wren/stream"),
                             ("POST", "/api/cousins/wren/interrupt"),
                             ("POST", "/api/cousins/wren/say")):
            status, body = router.dispatch(method, path, req=self._req(body={"text": "x"}))
            self.assertEqual(status, 409, (path, body))


class TestNeedsLogin(ConsoleCase):
    """Review M13: the three routes sit behind the console's session like
    every non-exempt route (console/app.py AUTH_EXEMPT); pinned so an edit
    to that list cannot open them."""

    def test_all_three_are_401_without_a_session(self):
        from cousin_lib.console import auth
        auth.Users(self.root / "config" / "console-users.json").set_password("priya", "a long passphrase")
        self.cousin("wren", operator="Priya", extra='\n[agent]\nrunner = "fake"\n')
        self.serve()
        self.assertEqual(self.get("/api/cousins/wren/stream")[0], 401)
        self.assertEqual(self.post("/api/cousins/wren/interrupt", {})[0], 401)
        self.assertEqual(self.post("/api/cousins/wren/say", {"text": "x"})[0], 401)


if __name__ == "__main__":
    unittest.main()
