"""The contract suite's own bookkeeping: every test names an item,
every item has a test, and declaring an item skips exactly its test."""
import unittest

from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner.contract.suite import CONTRACT_ITEMS, RunnerContract


def _tests():
    return {name: getattr(RunnerContract, name) for name in dir(RunnerContract)
            if name.startswith("test_")}


class TestContractItems(HermeticCase):
    def test_every_test_names_a_known_item_and_every_item_has_a_test(self):
        named = {}
        for name, fn in _tests().items():
            self.assertIn(getattr(fn, "contract_item", None), CONTRACT_ITEMS,
                          "%s names no contract item" % name)
            named.setdefault(fn.contract_item, []).append(name)
        self.assertEqual(sorted(named), sorted(CONTRACT_ITEMS))
        self.assertTrue(all(len(v) == 1 for v in named.values()), named)

    def test_declaring_an_item_unsupported_skips_exactly_that_test(self):
        class Declares(FakeRunner):
            def unsupported(self):
                return ["midturn_fold"]

        class Case(RunnerContract, HermeticCase):
            def make_runner(self, home, *, slow=False, fail_first=False):
                return Declares(home, turn_seconds=3.0 if slow else 0.0)

        fold = "test_a_midturn_operator_message_is_closed_by_the_same_result"
        other = "test_enqueue_returns_a_receipt"
        outcome = unittest.TestResult()
        unittest.TestSuite([Case(fold), Case(other)]).run(outcome)
        self.assertEqual(outcome.testsRun, 2)
        self.assertEqual([t._testMethodName for t, _ in outcome.skipped], [fold])
        self.assertTrue(outcome.wasSuccessful(), outcome.failures + outcome.errors)


if __name__ == "__main__":
    unittest.main()
