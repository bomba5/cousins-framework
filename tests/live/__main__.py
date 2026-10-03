"""`COUSIN_LIVE=1 python -m tests.live`: run the live harness matrix
(tests/live/test_matrix.py) and print the "tested with" block a lock
bump's pull request carries. The exit status is the test run's: 0 when
every item passed (a skipped item is named as skipped), 1 otherwise, 2
without COUSIN_LIVE=1. Nothing is written into the checkout."""
import platform
import sys
import unittest

from cousin_lib import harness_lock, version
from tests.live import test_matrix


class _Result(unittest.TextTestResult):
    """The run's own result, plus one outcome per test id."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def _mark(self, test, outcome):
        name = test.id().rsplit(".", 1)[-1].split(" ", 1)[0]
        if self.outcomes.get(name) not in ("FAIL", "ERROR"):
            self.outcomes[name] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self._mark(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "FAIL")

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test, "ERROR")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._mark(test, "skipped: %s" % reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._mark(test, "FAIL" if issubclass(err[0], test.failureException) else "ERROR")


def _versions():
    lock = harness_lock.load()
    lines = ["framework %s, python %s" % (version.version(), platform.python_version())]
    sdk = harness_lock.check("sdk", lock=lock)
    lines.append("claude-agent-sdk %s (installed %s)"
                 % (lock["sdk"]["claude-agent-sdk"], sdk["installed"].get("claude-agent-sdk")))
    lines.append("Claude Code %s (installed %s, %s)"
                 % (lock["sdk"]["bundled_cli"], sdk["installed"].get("cli"),
                    sdk["installed"].get("cli_source", "none")))
    binary = test_matrix._opencode_bin()
    oc = harness_lock.check("opencode", {"opencode_bin": binary or "opencode"}, lock=lock)
    lines.append("opencode %s (installed %s)"
                 % (lock["opencode"]["version"], oc["installed"].get("opencode")))
    return lines


def main():
    if not test_matrix.LIVE:
        print("tests.live: set COUSIN_LIVE=1 (it needs a login and spends a few model"
              " turns)", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_matrix)
    runner = unittest.TextTestRunner(resultclass=_Result, verbosity=2)
    result = runner.run(suite)
    print("\ntested with (config/harness.lock.toml):")
    for line in _versions():
        print("  " + line)
    for name, label in test_matrix.ITEMS.items():
        print("  %-32s %s" % (label, result.outcomes.get(name, "not run")))
    print("  %-32s %s" % ("6 bare-host quick start",
                          "manual (docs/development.md, \"The harness lock\")"))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
