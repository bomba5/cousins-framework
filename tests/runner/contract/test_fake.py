import unittest

from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner.contract.suite import RunnerContract


class TestFakeRunnerContract(RunnerContract, HermeticCase):
    def make_runner(self, home, **kw):
        return FakeRunner(home, **kw)


if __name__ == "__main__":
    unittest.main()
