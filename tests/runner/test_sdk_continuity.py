"""The SDK runner's continuity, end to end: a rollover carries an open
thread into the new session without pasting a packet, and a restart
resumes the same session."""
import os
import subprocess
import time
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner import tools
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result

THREAD = "invoice run - blocked on Sam"


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestContinuity(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)

    def test_a_rollover_carries_an_open_thread_with_no_pasted_packet(self):
        clients, holder = [], {}
        args = {"position": "p", "next_action": "n", "status": "- %s" % THREAD,
                "active_threads": [THREAD]}

        def factory(options):
            n = len(clients)
            if n == 0:
                scripts = [[init_msg(session="s-1"), assistant(text="w"), result(session="s-1")],
                           [init_msg(session="s-1"),
                            ("CALL", lambda: tools.call(holder["r"].tool_context, "handoff", args)),
                            assistant(text="done"), result(session="s-1")]]
            else:
                scripts = [[init_msg(session="s-2"), assistant(text="ok"), result(session="s-2")]] * 3
            clients.append(ScriptedClient(options, scripts))
            return clients[-1]
        r = SdkRunner(self.home, client_factory=factory, handoff_deadline_s=5.0)
        holder["r"] = r
        self.addCleanup(lambda: r.stop(timeout=5))
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as run:
            r.start()
            rec = r.enqueue(Item("operator:priya", "chat", "start", sender="Priya"))
            self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
            self.assertTrue(r.rollover("exit criterion")["ok"])
            self.assertTrue(_wait(lambda: clients[-1].queries))
        first = clients[-1].queries[0]["message"]["content"][0]["text"]
        self.assertIn(THREAD, first)                                     # the thread carried
        self.assertFalse(any("tmux" in str(c) for c in run.call_args_list))  # no pasted packet

    def test_restarting_the_runner_does_not_change_the_session_id(self):
        def factory(options):
            return ScriptedClient(options, [[init_msg(session="s-keep"), assistant(text="ok"),
                                             result(session="s-keep")]] * 3)
        r1 = SdkRunner(self.home, client_factory=factory); r1.start()
        rec = r1.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r1.inbox.get(rec.inbox_id)["state"] == "done"))
        r1.stop(timeout=5)
        seen = []
        r2 = SdkRunner(self.home, client_factory=lambda o: seen.append(o) or factory(o))
        self.addCleanup(lambda: r2.stop(timeout=5))
        r2.start()
        self.assertTrue(_wait(lambda: seen))
        # no account named: the host's claude-login account, which resumes
        # through the CLI's own --resume (the account KIND decides), so
        # options.resume stays unset
        self.assertIsNone(seen[0].resume)
        self.assertEqual(seen[0].extra_args["resume"], "s-keep")


if __name__ == "__main__":
    unittest.main()
