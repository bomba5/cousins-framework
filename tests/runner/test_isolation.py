"""One memory system: the SDK lane switches the agent CLI's own
auto-memory off, on every account, so a cousin never runs two memory
systems (spec, "One memory system")."""
import os
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient  # noqa: E402

SWITCH = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


class TestAutoMemoryOff(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="sdk")
        (self.home.parent.parent / "config").mkdir(exist_ok=True)
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.home.parent.parent)})
        p.start(); self.addCleanup(p.stop)

    def _options(self, **kw):
        r = SdkRunner(self.home, client_factory=lambda o: ScriptedClient(o, []), **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r.options()

    def test_the_login_lane_switches_auto_memory_off(self):
        self.assertEqual(self._options().env.get(SWITCH), "1")

    def test_the_key_lane_switches_it_off_too(self):
        env = self._options(api_key="sk-test-not-a-key").env
        self.assertEqual(env.get(SWITCH), "1")
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-test-not-a-key")   # nothing displaced


if __name__ == "__main__":
    unittest.main()
