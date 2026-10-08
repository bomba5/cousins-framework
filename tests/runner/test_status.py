"""A runner cousin's state as a process with no runner object reads it
(runner/status.py): the lock for liveness, the newest stream's head for
what runs there and what it declares unsupported, its last `state` event
for the state."""
import json
import threading
import time
import unittest

from cousin_lib.runner import main, status
from cousin_lib.runner.fake import FakeRunner
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _fake_home(case):
    home = temp_home(case)
    (home / "cousin.toml").write_text(
        '[cousin]\nslug = "wren"\nname = "Wren"\n[agent]\nrunner = "fake"\n')
    return home


class TestNoRunnerYet(HermeticCase):
    def test_a_home_with_no_stream_is_not_alive_and_says_nothing_more(self):
        self.assertEqual(status.status(_fake_home(self)),
                         {"alive": False, "state": None, "since": None, "session": None,
                          "kind": None, "pid": None, "unsupported": [], "model": None})


class TestThroughCousinRunner(HermeticCase):
    def test_a_finished_run_leaves_its_kind_and_last_state(self):
        home = _fake_home(self)
        self.assertEqual(main.runner_main(["--home", str(home), "--once"]), 0)
        out = status.status(home)
        self.assertFalse(out["alive"])
        self.assertEqual((out["kind"], out["unsupported"], out["state"]), ("fake", [], "stopped"))
        self.assertIsInstance(out["pid"], int)
        self.assertTrue(out["session"].startswith("fake-"))

    def test_the_runner_event_is_the_first_event_of_the_process(self):
        home = _fake_home(self)
        main.runner_main(["--home", str(home), "--once"])
        path = status.primary_stream(home)
        kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
        self.assertEqual(kinds[0], "runner")


class TestAlive(HermeticCase):
    def test_a_running_runner_is_alive_with_its_state(self):
        home = _fake_home(self)
        runner = FakeRunner(home)
        ready, done = threading.Event(), threading.Event()

        def hold():
            with main.hold_lock(home):
                runner.stream.append("runner", {"kind": runner.kind, "pid": 1,
                                                "unsupported": ["midturn_fold"]})
                runner.start()
                ready.set()
                done.wait(5)
                runner.stop(timeout=5)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(done.set)
        self.assertTrue(ready.wait(5))
        out = status.status(home)
        self.assertTrue(out["alive"])
        self.assertEqual((out["kind"], out["unsupported"]), ("fake", ["midturn_fold"]))


class TestReportedModel(HermeticCase):
    def test_the_model_the_session_reported_is_named(self):
        """A cousin on its lane's default has no model in its config;
        the SDK's init names the one it runs on, and the fleet row shows
        it marked as the default."""
        from cousin_lib.console.routes_fleet import _with_reported_model
        home = _fake_home(self)
        stream = EventStream(home, "sdk-model")
        stream.append("runner", {"kind": "sdk", "pid": 1, "unsupported": []})
        stream.append("session_init", {"model": "claude-opus-5-5", "session_id": "s"})
        out = status.status(home)
        self.assertEqual((out["kind"], out["model"]), ("sdk", "claude-opus-5-5"))
        self.assertEqual(_with_reported_model({"model": None, "effort": None}, out),
                         {"model": "claude-opus-5-5", "effort": None, "modelDefault": True})
        self.assertEqual(_with_reported_model({"model": "claude-fable-5-1", "effort": None}, out),
                         {"model": "claude-fable-5-1", "effort": None, "modelDefault": False})
        self.assertEqual(_with_reported_model({"model": None, "effort": None}, None)["model"], None)


class TestLongStreams(HermeticCase):
    def test_the_last_state_is_found_past_megabytes_of_events(self):
        home = _fake_home(self)
        stream = EventStream(home, "fake-long")
        stream.append("runner", {"kind": "fake", "pid": 1, "unsupported": []})
        stream.append("state", {"from": "idle", "to": "running", "detail": "turn"})
        for i in range(400):
            stream.append("tool_result", {"text": "x" * 4000, "n": i})
        out = status.status(home)
        self.assertEqual(out["state"], "running")
        self.assertGreater(status.primary_stream(home).stat().st_size, 1_000_000)

    def test_a_side_sessions_newer_stream_never_wins(self):
        """A side session's stream is written beside the primary one,
        headed `side_session`, never `runner`:
        the primary is the newest file whose first event is `runner`."""
        home = _fake_home(self)
        primary = EventStream(home, "sdk-primary")
        primary.append("runner", {"kind": "sdk", "pid": 7, "unsupported": []})
        primary.append("state", {"from": "idle", "to": "running"})
        time.sleep(0.02)
        side = EventStream(home, "sdk-peer-1")
        side.append("side_session", {"kind": "peer", "id": "1", "thread": "peer:testa"})
        side.append("state", {"from": "idle", "to": "errored"})
        self.assertEqual(status.primary_stream(home), primary.path)
        out = status.status(home)
        self.assertEqual((out["session"], out["kind"], out["state"]),
                         ("sdk-primary", "sdk", "running"))

    def test_with_no_runner_head_anywhere_the_newest_stream_wins(self):
        home = _fake_home(self)
        EventStream(home, "fake-old").append("state", {"from": "idle", "to": "errored"})
        time.sleep(0.02)
        EventStream(home, "fake-new").append("state", {"from": "running", "to": "idle"})
        self.assertEqual((status.status(home)["session"], status.status(home)["state"]),
                         ("fake-new", "idle"))


if __name__ == "__main__":
    unittest.main()
