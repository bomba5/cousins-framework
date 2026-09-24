"""The out-of-process interrupt on the SDK lane past a turn's first answer
(phase 5 review C1) and when the CLI refuses it (I1). A folded follow-up
starts a second CLI turn after the first result; an interrupt row must
still end it, from a process that holds no runner object."""
import time
import unittest

try:
    from claude_agent_sdk import AssistantMessage  # noqa: F401 - the extra must be installed
except ImportError:                                   # pragma: no cover - CI runs without it
    raise unittest.SkipTest("claude_agent_sdk is not installed (the sdk extra)")

from cousin_lib import delivery
from cousin_lib.runner.base import INTERRUPT
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, _results, _wait, assistant, init_msg, result


def _op(body):
    return delivery.Item("operator:priya", "chat", body, sender="Priya")


def _interrupt():
    return delivery.Item("system", INTERRUPT, "interrupt asked from the console", sender="Priya")


class Case(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[agent]\nrunner = "sdk"\n')

    def _runner(self, scripts, client_cls=ScriptedClient):
        made = {}

        def factory(options):
            made["client"] = client_cls(options, scripts)
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r, made


class TestContinuation(Case):
    def test_an_interrupt_ends_the_cli_turn_a_folded_follow_up_started(self):
        r, made = self._runner([[init_msg(), assistant(text="first answer"), "PAUSE", result()],
                                [assistant(tool="Bash"), "WAIT_FOR_INTERRUPT"]])
        r.start()
        r.enqueue(_op("reconcile the ledger"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        r.enqueue(_op("stop, do the audit instead"))      # folded: a second CLI turn
        self.assertTrue(_wait(lambda: len(made["client"].queries) == 2))
        made["client"].resume()                             # the first result is read
        self.assertTrue(_wait(lambda: any(e["kind"] == "tool" for e in r.events())))
        out = delivery.deliver(self.home, _interrupt(), wait=True, timeout=5.0)
        self.assertEqual(out, delivery.DELIVERED)
        self.assertEqual(made["client"].interrupts, 1)
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=5.0))
        self.assertTrue(_results(r)[-1]["interrupted"])


class _RefusingClient(ScriptedClient):
    async def interrupt(self):
        raise RuntimeError("the CLI refused the interrupt")


class TestRefusedInterrupt(Case):
    def test_a_refused_interrupt_fails_its_row_and_strands_nothing(self):
        r, made = self._runner([[init_msg(), assistant(text="working"), ("SLOW", 1.5), result()]],
                               client_cls=_RefusingClient)
        r.start()
        r.enqueue(_op("slow"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        stop = r.enqueue(_interrupt())
        more = r.enqueue(_op("and also this"))
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "done", timeout=5))
        row = r.inbox.get(stop.inbox_id)
        self.assertEqual(row["outcome"], "failed")
        self.assertIn("RuntimeError: the CLI refused the interrupt", row["detail"])
        self.assertTrue(_wait(lambda: r.inbox.get(more.inbox_id)["state"] == "done", timeout=8))
        self.assertEqual(r.inbox.get(more.inbox_id)["outcome"], "delivered")
        self.assertEqual(r.inbox.unfinished(), 0)
        self.assertNotIn("errored", [e["payload"]["to"] for e in r.events() if e["kind"] == "state"])


if __name__ == "__main__":
    unittest.main()
