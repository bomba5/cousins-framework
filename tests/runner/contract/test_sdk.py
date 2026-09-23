import unittest

try:
    import claude_agent_sdk  # noqa: F401 - the `sdk` extra is optional
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner.contract.suite import RunnerContract
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result


def _turn():
    return [init_msg(), assistant(tool="Bash"), assistant(text="ok"), result()]


def _scripts(slow):
    # enough scripted CLI turns for any contract test; a slow first turn is
    # silent for 3 s, or until interrupted, before it answers
    scripts = [_turn() for _ in range(6)]
    if slow:
        scripts[0] = [init_msg(), assistant(tool="Bash"), ("SLOW", 3.0),
                      assistant(text="ok"), result()]
    return scripts


class TestSdkRunnerContract(RunnerContract, HermeticCase):
    def make_runner(self, home, *, slow=False, fail_first=False):
        clients = []

        def factory(options):
            if fail_first and not clients:
                scripts = [[init_msg(), "END"]]   # the CLI dies mid-turn
            else:
                scripts = _scripts(slow and not clients)
            clients.append(ScriptedClient(options, scripts))
            return clients[-1]
        return SdkRunner(home, client_factory=factory, drain_timeout_s=2.0)


if __name__ == "__main__":
    unittest.main()
