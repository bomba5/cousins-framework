"""The three harness hooks under hooks/, run for real against a temp home.

Each hook takes COUSIN_HOME from the environment and nothing else,
writes only under <home>/data/, and is executable in the tree (a hook
the harness cannot exec is a hook that silently never fires). These
tests run the actual scripts, not a reading of them.
"""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HOOKS = _REPO_ROOT / "hooks"
_NAMES = ("pre_compact.sh", "session_checkpoint.sh", "session_init.sh")


def _tree(root):
    """Every path under root, relative, so a test can prove a hook
    touched nothing outside data/."""
    return sorted(str(p.relative_to(root))
                  for p in root.rglob("*") if p.is_file())


class HookCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\n')
        (self.home / "CLAUDE.md").write_text("# Testa\n\nrules\n")
        (self.home / "STATUS.md").write_text(
            "# Status\n\n## Open loops\n\n- [ ] descale the machine\n"
            "- [~] port the docs\n\n## Recently closed\n\n- [x] old\n")
        (self.home / "data" / "last-activity.txt").write_text(
            "2026-09-17T10:00:00: porting the hooks\n")
        (self.home / "data" / "decisions.jsonl").write_text(
            json.dumps({"topic": "retention window",
                        "decision": "keep 30 days",
                        "reasoning": "audits need a month"}) + "\n")

    def _run(self, name, env_extra=None, clear_home=False):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if not clear_home:
            env["COUSIN_HOME"] = str(self.home)
        env.update(env_extra or {})
        return subprocess.run([str(_HOOKS / name)], env=env,
                              capture_output=True, text=True, timeout=30)


class TestShape(HookCase):
    def test_every_hook_is_executable_in_the_tree(self):
        for name in _NAMES:
            path = _HOOKS / name
            self.assertTrue(path.is_file(), name)
            self.assertTrue(os.access(path, os.X_OK),
                            "%s is not executable" % name)

    def test_hooks_write_only_under_data(self):
        for name in _NAMES:
            before = set(_tree(self.home))
            self._run(name)
            new = set(_tree(self.home)) - before
            for path in new:
                self.assertTrue(path.startswith("data/"),
                                "%s wrote %s outside data/" % (name, path))

    def test_without_a_home_every_hook_exits_zero_and_writes_nothing(self):
        for name in _NAMES:
            before = _tree(self.home)
            proc = self._run(name, clear_home=True)
            self.assertEqual(proc.returncode, 0, (name, proc.stderr))
            self.assertEqual(_tree(self.home), before, name)


class TestPreCompact(HookCase):
    def test_writes_the_checkpoint_with_activity_and_decisions(self):
        proc = self._run("pre_compact.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = (self.home / "data" / "pre-compact-checkpoint.md").read_text()
        self.assertIn("testa", text)
        self.assertIn("porting the hooks", text)
        self.assertIn("retention window", text)
        self.assertIn("keep 30 days", text)
        self.assertIn("STATUS.md", text)

    def test_stdout_is_a_system_message_naming_the_checkpoint(self):
        proc = self._run("pre_compact.sh")
        msg = json.loads(proc.stdout)["systemMessage"]
        self.assertIn("pre-compact-checkpoint.md", msg)

    def test_slug_from_environment_wins(self):
        self._run("pre_compact.sh", {"COUSIN_SLUG": "other"})
        text = (self.home / "data" / "pre-compact-checkpoint.md").read_text()
        self.assertIn("other", text.splitlines()[0])


class TestSessionCheckpoint(HookCase):
    def test_writes_open_work_activity_and_decisions(self):
        proc = self._run("session_checkpoint.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = (self.home / "data" / "session-checkpoint.md").read_text()
        self.assertIn("descale the machine", text)
        self.assertIn("port the docs", text)
        self.assertNotIn("- [x] old", text)
        self.assertIn("porting the hooks", text)
        self.assertIn("retention window", text)

    def test_stdout_is_a_system_message_naming_the_checkpoint(self):
        proc = self._run("session_checkpoint.sh")
        msg = json.loads(proc.stdout)["systemMessage"]
        self.assertIn("session-checkpoint.md", msg)

    def test_absent_inputs_still_produce_a_checkpoint(self):
        (self.home / "STATUS.md").unlink()
        (self.home / "data" / "last-activity.txt").unlink()
        (self.home / "data" / "decisions.jsonl").unlink()
        proc = self._run("session_checkpoint.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            (self.home / "data" / "session-checkpoint.md").is_file())


class TestSessionInit(HookCase):
    def test_banner_names_who_where_and_what_is_on_disk(self):
        proc = self._run("session_init.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("testa", proc.stdout)
        self.assertIn(str(self.home), proc.stdout)
        self.assertIn("CLAUDE.md", proc.stdout)
        self.assertIn("STATUS.md", proc.stdout)
        self.assertNotIn("MEMORY.md", proc.stdout)  # not on this disk

    def test_banner_points_at_the_last_checkpoint_when_one_exists(self):
        (self.home / "data" / "session-checkpoint.md").write_text("# x\n")
        proc = self._run("session_init.sh")
        self.assertIn("session-checkpoint.md", proc.stdout)

    def test_banner_writes_nothing(self):
        before = _tree(self.home)
        self._run("session_init.sh")
        self.assertEqual(_tree(self.home), before)


if __name__ == "__main__":
    unittest.main()
