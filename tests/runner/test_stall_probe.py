"""The SDK runner names what its turn waits on when the wait runs long.
A runner's consumer has been seen stopped for 18-39 minutes mid-turn (no stream writes while the CLI kept running tools)
until the SDK's 100-message buffer filled and its reader stopped answering
hooks. A turn's fold, interrupt-row control and query write run under a
site marker; a watchdog on the runner's loop writes a `system` `stall`
event while one runs past STALL_REPORT_S (ongoing), and one when it ends,
with its duration."""
import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner import sdk as sdk_mod
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, init_msg, result


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class SlowFoldClient(ScriptedClient):
    """The second query (the fold) takes `hold` seconds to be written, as a
    write blocked on the CLI's stdin would."""
    hold = 2.0

    async def query(self, prompt, session_id="default"):
        if len(self.queries) >= 1:
            await asyncio.sleep(self.hold)
        return await super().query(prompt, session_id)


class SlowContextClient(ScriptedClient):
    """The context-usage reading after the result takes a second."""

    async def get_context_usage(self):
        await asyncio.sleep(1.0)
        return {"percentage": 1.0}


class OwnedLock:
    """The stream's lock, refusing the thread that holds it (a plain Lock
    would hang there)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None

    def __enter__(self):
        if self._owner == threading.get_ident():
            raise RuntimeError("the stream's lock, again, on the thread that holds it")
        self._lock.acquire()
        self._owner = threading.get_ident()

    def __exit__(self, *exc):
        self._owner = None
        self._lock.release()


class GatedRunner:
    """A runner whose loop writes a dump's tasks only once `gate` opens,
    so two dumps can overlap on purpose."""

    def __init__(self, home):
        self.home = home
        self.gate = threading.Event()
        self.scheduled, self.events = [], []
        self.stream = SimpleNamespace(append=lambda kind, payload: self.events.append(payload))

    def dump_tasks(self, path, done=None):
        n = len(self.scheduled) + 1

        def write():
            self.gate.wait(10)
            with open(path, "a") as fh:
                fh.write("tasks of dump %d\n" % n)
            if done is not None:
                done.set()
        thread = threading.Thread(target=write, daemon=True)
        self.scheduled.append(thread)
        thread.start()
        return True


class TestStallProbe(HermeticCase):
    def test_a_wait_after_the_result_is_named(self):
        # a turn that never came back after its result said nothing in its
        # stream: every step after the result is a named site now
        home = temp_home(self)
        r = SdkRunner(home, client_factory=lambda o: SlowContextClient(
            o, [[init_msg(), result()]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        with mock.patch.object(sdk_mod, "STALL_REPORT_S", 0.3, create=True), \
                mock.patch.object(sdk_mod, "STALL_CHECK_S", 0.05, create=True):
            r.start()
            rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
            self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"
                                  and r.state() == "idle"))
        stalls = [e["payload"] for e in r.events() if e["kind"] == "system"
                  and e["payload"].get("subtype") == "stall"]
        self.assertEqual({(s["site"], s["ongoing"]) for s in stalls},
                         {("after the result: context usage", True),
                          ("after the result: context usage", False)})

    def test_the_stack_dump_names_every_thread_and_the_turns_task(self):
        # kill -USR1 on a runner that hangs: where it waits, without ptrace
        from cousin_lib.runner import main as runner_main
        home = temp_home(self)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(
            o, [[init_msg(), "HANG", result()]]))
        r.poll_s = 0.05
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "start", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        path = runner_main.dump_stacks(r)
        self.assertEqual(path, home / "data" / "runner-stacks.txt")
        self.assertTrue(_wait(lambda: "--- asyncio tasks" in path.read_text()))
        text = path.read_text()
        self.assertIn("most recent call first", text)    # every thread's stack
        self.assertIn("_turn", text)                     # the turn's task, where it awaits
        self.assertIn(str(path), [e["payload"].get("path") for e in r.events()
                                  if e["kind"] == "system"
                                  and e["payload"].get("subtype") == "stacks"])

    def test_the_signal_dumps_off_the_thread_it_interrupts(self):
        # SIGUSR1 runs between two bytecodes of the main thread, maybe in the
        # middle of a stream append: the dump must not take that lock there
        from cousin_lib.runner import main as runner_main
        home = temp_home(self)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, []))
        lock = OwnedLock()
        r.stream._lock = lock
        with lock:                              # the append the signal interrupts
            thread = runner_main.dump_stacks_soon(r)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertIn(str(home / "data" / "runner-stacks.txt"),
                      [e["payload"].get("path") for e in r.events()
                       if e["kind"] == "system" and e["payload"].get("subtype") == "stacks"])

    def test_two_dumps_close_together_leave_one_whole_dump(self):
        # two SIGUSR1s in a row: each dump rewrites the file, so the second
        # waits until the first one's tasks are written
        from cousin_lib.runner import main as runner_main
        r = GatedRunner(temp_home(self))
        first = runner_main.dump_stacks_soon(r)
        self.assertTrue(_wait(lambda: len(r.scheduled) == 1))
        second = runner_main.dump_stacks_soon(r)
        _wait(lambda: len(r.scheduled) == 2, timeout=0.5)    # it must not start yet
        r.gate.set()
        for thread in [first, second] + r.scheduled:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        text = (r.home / "data" / "runner-stacks.txt").read_text()
        self.assertEqual(text.count(" stacks at "), 1)
        self.assertEqual([l for l in text.splitlines() if l.startswith("tasks of dump")],
                         ["tasks of dump 2"])
        self.assertEqual([e.get("tasks") for e in r.events if e.get("subtype") == "stacks"],
                         [True, True])

    def test_a_blocked_loop_still_gets_its_thread_stacks(self):
        # a loop that never writes its half: after TASKS_WAIT_S the dump
        # ends anyway, and its event says the tasks are missing
        from cousin_lib.runner import main as runner_main
        r = GatedRunner(temp_home(self))                     # the gate stays shut
        with mock.patch.object(runner_main, "TASKS_WAIT_S", 0.2):
            path = runner_main.dump_stacks(r)
        self.assertIn("most recent call first", path.read_text())
        self.assertEqual([e.get("tasks") for e in r.events if e.get("subtype") == "stacks"],
                         [False])
        r.gate.set()
        for thread in r.scheduled:
            thread.join(timeout=10)

    def test_a_long_fold_is_named_while_it_runs_and_when_it_ends(self):
        home = temp_home(self)
        r = SdkRunner(home, client_factory=lambda o: SlowFoldClient(
            o, [[init_msg(), "HANG", result()]]))
        r.poll_s = 0.05
        self.addCleanup(lambda: r.stop(timeout=5))
        with mock.patch.object(sdk_mod, "STALL_REPORT_S", 0.5, create=True), \
                mock.patch.object(sdk_mod, "STALL_CHECK_S", 0.1, create=True):
            r.start()
            r.enqueue(Item("operator:priya", "chat", "start", sender="Priya"))
            self.assertTrue(_wait(lambda: r.state() == "running"))
            r.enqueue(Item("operator:priya", "chat", "and this too", sender="Priya"))

            def stalls():
                return [e["payload"] for e in r.events() if e["kind"] == "system"
                        and e["payload"].get("subtype") == "stall"]
            self.assertTrue(_wait(lambda: any(s.get("ongoing") for s in stalls()), 5),
                            "no stall named while it ran")
            self.assertTrue(_wait(lambda: any(not s.get("ongoing") for s in stalls()), 5),
                            "no stall named when it ended")
        # the fold hands its write to the turn's writer and
        # returns at once: the write (send) is the site that held, named while
        # it runs and when it ends; the reader never waits at the fold
        self.assertTrue(_wait(lambda: {s["site"] for s in stalls() if not s.get("ongoing")}
                              == {"send"}, 5), stalls())
        self.assertEqual({s["site"] for s in stalls() if s.get("ongoing")}, {"send"})
        ended = {s["site"]: s["seconds"] for s in stalls() if not s.get("ongoing")}
        self.assertGreaterEqual(ended["send"], 1.5)

    def test_short_waits_write_nothing(self):
        home = temp_home(self)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(
            o, [[init_msg(), result()]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"
                              and r.state() == "idle"))
        self.assertEqual([e for e in r.events() if e["kind"] == "system"
                          and e["payload"].get("subtype") == "stall"], [])


if __name__ == "__main__":
    unittest.main()
