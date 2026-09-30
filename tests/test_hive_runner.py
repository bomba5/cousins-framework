"""The hive and a runner cousin (phase 6, task 9).

A hive node is not a framework install: it runs the standalone
templates/hive-node/cousin_node.py. `cousin-hive recall` is answered by
the queen from its own store; the only thing a lane changes is the
environment the call runs in, so the round trip is run from the
environment a runner gives its tools. (A node's `[tell-home: ...]`
reaches a runner cousin through the queen: tests/console/
test_hive_tell_home.py.)

Loopback queen, fake agent command. No model call, no tmux.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

from cousin_lib import accounts
from cousin_lib.hive import HiveStore, build_queen
from cousin_lib.runner.main import export_environment
from tests._hermetic import HermeticCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

def _install(case):
    """A throwaway install root with one runner cousin (Wren, `fake`)
    and one cousin with no runner (Sam)."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    homes = {}
    for slug, agent in (("wren", '[agent]\nrunner = "fake"\n'),
                        ("sam", "")):
        home = root / "cousins" / slug
        for sub in ("data", "run", "memory"):
            (home / sub).mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n\n%s'
            % (slug, slug.capitalize(), agent))
        homes[slug] = home
    return root, homes


class TestRecallFromARunnerToolEnvironment(HermeticCase):
    def test_recall_round_trip_from_a_runner_cousins_tool_environment(self):
        # guard: recall has no lane in it (the queen answers from its
        # store); this pins that the runner's tool environment (its
        # exported COUSIN_HOME/FRAMEWORK_ROOT, scrubbed credentials, the
        # home as the working directory) still reaches the queen
        root, homes = _install(self)
        home = homes["wren"]
        store = HiveStore(root / "hive")
        self.addCleanup(store.close)
        queen = build_queen(store)
        queen.start()
        self.addCleanup(queen.stop)
        store.append_memory("testa", "the greenhouse vent opens at 28C",
                            scope="shared")
        token = store.mint_token("wren", scope=["own", "shared"])
        os.environ["ANTHROPIC_API_KEY"] = "sk-not-a-real-key"
        export_environment(home)
        env = accounts.scrub(dict(os.environ))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["COUSIN_HOME"], str(home))
        self.assertEqual(env["FRAMEWORK_ROOT"], str(root))
        # the child imports this checkout, not whatever the venv installed
        env["PYTHONPATH"] = str(_REPO_ROOT)
        proc = subprocess.run(
            [sys.executable, "-m", "cousin_lib.hive", "recall",
             "--queen", "http://127.0.0.1:%d" % queen.port,
             "--token", token, "greenhouse"],
            cwd=str(home), env=env, capture_output=True, text=True,
            timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "- the greenhouse vent opens at 28C\n")


if __name__ == "__main__":
    unittest.main()
