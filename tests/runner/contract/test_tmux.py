import unittest

from cousin_lib.runner.tmux_runner import TmuxRunner
from tests._hermetic import HermeticCase
from tests.runner._fake_pane import FakePane
from tests.runner.contract.suite import RunnerContract


def make_tmux(home, *, slow=False, fail_first=False):
    runner = TmuxRunner(home, account=None, pane_factory=lambda path: FakePane(
        path, slow=slow, fail_first=fail_first), config_dir=home / ".fake-claude",
        launch_argv=lambda session_id, fresh: ["claude", "--session-id" if fresh else "--resume",
                                               session_id])
    return runner


class TestTmuxRunnerContract(RunnerContract, HermeticCase):
    def make_runner(self, home, *, slow=False, fail_first=False):
        return make_tmux(home, slow=slow, fail_first=fail_first)


if __name__ == "__main__":
    unittest.main()
