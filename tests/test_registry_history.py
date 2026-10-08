"""registry_history.SHIPPED_BEFORE holds every value a past release
shipped in the registry that the current one changed: the sync can only
migrate what it knows was the framework's own text."""
import pathlib
import subprocess
import unittest

from cousin_lib import registry_history

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _full_history():
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-shallow-repository"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return out == "false"


class TestRegistryHistory(unittest.TestCase):
    @unittest.skipUnless(_full_history(), "needs a full git clone")
    def test_every_past_value_is_known(self):
        history = registry_history.from_git(ROOT)
        missing = {key: [v for v in olds if v not in registry_history.SHIPPED_BEFORE.get(key, ())]
                   for key, olds in history.items()}
        missing = {k: v for k, v in missing.items() if v}
        self.assertEqual(missing, {}, "run `python -m cousin_lib.registry_history --write`")

    def test_the_sync_migrates_from_it(self):
        from cousin_lib import template_sync
        for key, olds in registry_history.SHIPPED_BEFORE.items():
            self.assertEqual([old for old, _ in template_sync._MIGRATIONS[key]], list(olds))
