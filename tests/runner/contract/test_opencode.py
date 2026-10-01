"""THE contract suite against OpencodeRunner, on the fake `opencode
serve` through the runner's server_factory seam: no binary, no network.
Every item passes or is declared in unsupported() with its evidence;
none is declared (midturn_fold is measured). The live test on the real
binary is tests/runner/test_opencode_live.py."""
import unittest

from cousin_lib import accounts
from cousin_lib.runner.opencode import OpencodeRunner
from tests._hermetic import HermeticCase
from tests.runner.contract.suite import RunnerContract
from tests.runner.test_opencode import Factory

ENDPOINT = "http://127.0.0.1:11434/v1"      # never called: the fake server answers the turns
MODEL = "local/m1"


def _turn():
    # turn_events wants a tool between turn_start and the result
    return [("tool", "bash", {"command": "true"}, ""), ("text", "ok")]


def _scripts(slow, fail_first):
    # enough scripted opencode runs for any contract test; a slow first run
    # is silent for 3 s, or until aborted, before it answers
    scripts = [_turn() for _ in range(8)]
    if slow:
        scripts[0] = [("tool", "bash", {"command": "true"}, ""), ("SLOW", 3.0), ("text", "ok")]
    if fail_first:
        scripts[0] = [("FAIL", "UnknownError", "the provider fell over")]
    return scripts


class TestOpencodeRunnerContract(RunnerContract, HermeticCase):
    def make_runner(self, home, *, slow=False, fail_first=False):
        with open(home / "cousin.toml", "a") as f:
            f.write('model = "%s"\n' % MODEL)
        account = accounts.Account("lab", "opencode", None, None,
                                   data_dir=home.parent.parent / ".secrets" / "accounts"
                                   / "lab.opencode", endpoint=ENDPOINT, endpoint_model="m1")
        return OpencodeRunner(home, account=account,
                              server_factory=Factory(_scripts(slow, fail_first)))


if __name__ == "__main__":
    unittest.main()
