import unittest

try:
    import claude_agent_sdk  # noqa: F401 - the `sdk` extra is optional
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner.contract.suite import RunnerContract
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result


def _scripts():
    # enough scripted turns for any contract test; each is start/tool/text/result
    return [[init_msg(), assistant(tool="Bash"), assistant(text="ok"), result()]
            for _ in range(6)]


class TestSdkRunnerContract(RunnerContract, HermeticCase):
    def make_runner(self, home, turn_seconds=0.0):
        delay = 0.15 if turn_seconds else 0.0
        scripts = _scripts()
        if turn_seconds:
            scripts[0] = [init_msg(), assistant(tool="Bash"), "WAIT_FOR_INTERRUPT"] \
                if turn_seconds >= 2.0 else [init_msg(), assistant(tool="Bash"),
                                             assistant(text="ok"), result()]
        return SdkRunner(home, client_factory=lambda o: ScriptedClient(o, scripts, delay=delay))


if __name__ == "__main__":
    unittest.main()
