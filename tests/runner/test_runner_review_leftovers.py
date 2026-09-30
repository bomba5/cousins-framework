"""The runner's phase 2 review leftovers (#68), each pinned on its own:
`--once` against a runner that drains, an inbox read that raises in
`--once`, the echo of a row with attachments, a silent long tool call
against the idle timeout, and the interrupt poll's sqlite read off the
event loop."""
import contextlib
import io
import sqlite3
import threading
import time
import unittest
from unittest import mock

try:
    from claude_agent_sdk import ToolResultBlock, UserMessage
except ImportError:  # the `sdk` extra is optional; discovery skips, never errors
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner import main as runner_main
from cousin_lib.runner.base import INTERRUPT
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import (ScriptedClient, _errors, _results, _wait, assistant,
                                   init_msg, result)


def _stub(states, unfinished, drain=None):
    """A runner for `_once`: `states()` and `unfinished()` are callables
    of the time since the stub was made."""
    t0 = time.monotonic()
    runner = mock.Mock(spec=["worker_alive", "state", "inbox", "login_required",
                             "side_stalled", "drain_timeout_s"])
    runner.worker_alive.return_value = True
    runner.login_required.return_value = False
    runner.side_stalled.return_value = False
    runner.state.side_effect = lambda: states(time.monotonic() - t0)
    runner.inbox.unfinished.side_effect = lambda: unfinished(time.monotonic() - t0)
    runner.drain_timeout_s = drain
    return runner


def _once(runner):
    stop = threading.Event()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = runner_main._once(runner, stop)
    return rc, err.getvalue()


class TestOnceWaitsOutTheDrain(HermeticCase):
    """A failed SDK turn stays `errored` through its resync, up to
    drain_timeout_s (30 s) and a reconnect, before it recovers; `--once`
    gave up after ERRORED_GIVE_UP_S (10 s) and exited 3 on a runner that
    was about to recover."""

    def test_an_errored_runner_inside_its_drain_is_not_given_up(self):
        runner = _stub(lambda t: "errored" if t < 0.5 else "idle",
                       lambda t: 1 if t < 0.5 else 0, drain=0.6)
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.1):
            rc, err = _once(runner)
        self.assertEqual(rc, 0, err)

    def test_past_the_drain_it_still_gives_up(self):
        runner = _stub(lambda t: "errored", lambda t: 1, drain=0.2)
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.1):
            t = time.monotonic()
            rc, err = _once(runner)
        self.assertEqual(rc, 3)
        self.assertGreaterEqual(time.monotonic() - t, 0.3)
        self.assertIn("errored", err)

    def test_a_runner_with_no_drain_keeps_the_plain_budget(self):
        runner = _stub(lambda t: "errored", lambda t: 1)
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.1):
            t = time.monotonic()
            rc, _err = _once(runner)
        self.assertEqual(rc, 3)
        self.assertLess(time.monotonic() - t, 2.0)


class TestOnceSurvivesAnInboxRead(HermeticCase):
    """`_once` read `runner.inbox.unfinished()` unguarded: one busy or
    broken inbox read ended `--once` with a traceback, and the runner's
    stop in `_serve`'s finally was all that ran."""

    def test_a_read_that_raises_once_is_retried(self):
        calls = []

        def unfinished(t):
            calls.append(t)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return 0
        rc, err = _once(_stub(lambda t: "idle", unfinished))
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(len(calls), 3)

    def test_a_read_that_keeps_raising_gives_up_with_the_reason(self):
        def unfinished(t):
            raise sqlite3.OperationalError("disk I/O error")
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.1):
            rc, err = _once(_stub(lambda t: "idle", unfinished))
        self.assertEqual(rc, 3)
        self.assertIn("inbox", err)
        self.assertIn("disk I/O error", err)


class SdkCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def runner(self, scripts, **kw):
        made = {"clients": []}

        def factory(options):
            made["client"] = ScriptedClient(options, scripts)
            made["clients"].append(made["client"])
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory, **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r, made


class TestTheEchoOfARowWithAttachments(SdkCase):
    """The echo is matched on the envelope's exact text. A row with an
    image and a file attachment is sent as the envelope, an image block and
    a text block naming the file; the CLI's replay keeps the text blocks
    and drops the image, and the row is still found and closed."""

    def test_an_image_and_a_file_do_not_hide_the_echo(self):
        image = self.home / "data" / "photo.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
        other = self.home / "data" / "notes.txt"
        other.write_text("a file\n")
        r, made = self.runner([[init_msg(), assistant(text="seen"), result()]])
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "look at these", sender="Priya",
                             attachments=(str(image), str(other))))
        self.assertTrue(_wait(lambda: _results(r)
                              and r.inbox.get(rec.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        sent = made["client"].queries[0]["message"]["content"]
        self.assertEqual([b["type"] for b in sent], ["text", "image", "text"])
        users = [e["payload"] for e in r.events() if e["kind"] == "user"]
        self.assertEqual([u["echo_of"] for u in users], [rec.inbox_id])
        self.assertEqual(_results(r)[0]["inbox_ids"], [rec.inbox_id])


def _tool_result(tool_use_id="tu-1"):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_use_id, content="ok",
                                                is_error=False)])


class TestASilentToolCallIsNotAStall(SdkCase):
    """The idle timeout bounds a stream gone silent (600 s). A tool call
    is silent while it runs, and a long one (a build, a 10-minute Bash)
    failed its turn as if the CLI had hung, and the resync interrupted
    it. While a tool call is open (its tool_use seen, its tool_result
    not yet), the bound is `tool_idle_timeout_s`."""

    def test_a_tool_longer_than_the_idle_timeout_finishes_its_turn(self):
        r, _ = self.runner([[init_msg(), assistant(tool="Bash"), ("SLOW", 1.2),
                             _tool_result(), assistant(text="built"), result()]],
                           idle_timeout_s=0.4)
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "build it", sender="Priya"))
        self.assertTrue(_wait(lambda: _results(r)
                              and r.inbox.get(rec.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        self.assertFalse([e for e in _errors(r) if "no message for" in e])

    def test_silence_after_the_tool_result_is_still_a_stall(self):
        r, _ = self.runner([[init_msg(), assistant(tool="Bash"), _tool_result(),
                             "HANG", result()]], idle_timeout_s=0.4)
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        self.assertTrue(_wait(lambda: any("no message for 0.4s" in e for e in _errors(r))))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "failed")

    def test_an_open_tool_has_its_own_bound(self):
        r, _ = self.runner([[init_msg(), assistant(tool="Bash"), "HANG", result()]],
                           idle_timeout_s=0.2)
        r.tool_idle_timeout_s = 0.6
        r.start()
        r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        self.assertTrue(_wait(lambda: any("no message for 0.6s" in e for e in _errors(r))))
        self.assertFalse([e for e in _errors(r) if "no message for 0.2s" in e])


class TestTheInterruptPollIsOffTheLoop(SdkCase):
    """A live turn looks for interrupt rows every poll_s (5 Hz). The read
    ran on the event loop thread: a busy inbox (sqlite's 30 s lock wait)
    froze the reader, the hooks and the stall watch with it."""

    def test_the_interrupt_rows_are_read_off_the_loop_thread(self):
        r, made = self.runner([[init_msg(), assistant(text="a"), "PAUSE",
                                assistant(text="b"), result()]])
        seen = []
        real = r.inbox.open_rows

        def open_rows(source):
            if source == INTERRUPT:
                seen.append(threading.current_thread())
            return real(source)
        r.inbox.open_rows = open_rows
        r.start()
        r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        self.assertTrue(_wait(lambda: len(seen) >= 3))
        made["client"].resume()
        self.assertTrue(_wait(lambda: _results(r)))
        self.assertNotIn(r._thread, seen)


if __name__ == "__main__":
    unittest.main()
