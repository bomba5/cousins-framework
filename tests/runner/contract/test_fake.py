import unittest

from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner.contract.suite import RunnerContract


def make_fake(home, *, slow=False, fail_first=False):
    return FakeRunner(home, turn_seconds=3.0 if slow else 0.0,
                      script=["fail_once"] if fail_first else ["tool"])


class TestFakeRunnerContract(RunnerContract, HermeticCase):
    def make_runner(self, home, *, slow=False, fail_first=False):
        return make_fake(home, slow=slow, fail_first=fail_first)


if __name__ == "__main__":
    unittest.main()
