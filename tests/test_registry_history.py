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

    def test_the_topic_wording_says_one_claim_per_topic_and_reaches_old_homes(self):
        # the distilled views show the newest entry per topic: a topic with
        # several independent facts hid all but one, so the tool says so
        import json
        import tempfile
        import tomllib
        from cousin_lib import template_sync

        def topic(text):
            return tomllib.loads(text)["tools"]["memory"]["properties"]["topic"]["description"]
        shipped = (ROOT / registry_history.REGISTRY).read_text()
        new, old = topic(shipped), \
            "decision topic (decide, remember); the topic to retire (obsolete)"
        self.assertIn("one claim per topic", new)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config").mkdir()
            (root / registry_history.REGISTRY).write_text(shipped)
            home = root / "cousins" / "wren"
            home.mkdir(parents=True)
            reg = home / "mcp-registry.toml"
            reg.write_text(shipped.replace(json.dumps(new), json.dumps(old)))
            self.assertEqual(topic(reg.read_text()), old)
            out = template_sync._registry_sync(home, root, apply=True)
            self.assertIn("tools.memory.properties.topic", out["corrected"])
            self.assertEqual(topic(reg.read_text()), new)
