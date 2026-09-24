"""#104 (b): the SDK runner names what its turn waits on when the wait runs
long. The incidents #104 names show the runner's consumer stopped for
18-39 minutes mid-turn (no stream writes while the CLI kept running tools)
until the SDK's 100-message buffer filled and its reader stopped answering
hooks. A turn's fold, interrupt-row control and query write run under a
site marker; a watchdog on the runner's loop writes a `system` `stall`
event while one runs past STALL_REPORT_S (ongoing), and one when it ends,
with its duration."""
import asyncio
import time
import unittest
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


class TestStallProbe(HermeticCase):
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
        # the fold hands its write to the turn's writer (#118, after #104) and
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
