"""config/harness.lock.toml: the one source of the harness versions.

Every other place that names one is checked against it here, on every
pull request and with no network: the image's requirements and its
opencode stage, pyproject.toml's sdk extra, DEFAULT_MODELS and the
README's opencode model. Each check is a reader over text, so the red
cases (a pin bumped in one place only) are proven here too, against the
real files with one value changed. The comparison a runner makes at its
start (harness_lock.check) is tested against fake metadata and fake
binaries; the runner's event and refusal are in tests/runner/test_harness_start.py."""
import os
import pathlib
import re
import stat
import tempfile
import time
import tomllib
import unittest
from unittest import mock

from cousin_lib import harness_lock
from tests._hermetic import HermeticCase

_REPO = pathlib.Path(__file__).resolve().parents[1]
_LOCK = _REPO / "config" / "harness.lock.toml"


def _read(rel):
    return (_REPO / rel).read_text(encoding="utf-8")


def requirements_sdk(text):
    """The claude-agent-sdk version docker/requirements.txt installs."""
    found = re.findall(r"^claude-agent-sdk==(\S+)", text, re.M)
    return found[0] if len(found) == 1 else None


def dockerfile_opencode(text):
    """The opencode release the Dockerfile's fetch stage downloads."""
    found = re.findall(r"\bversion=([0-9][0-9A-Za-z.+-]*);", text)
    return found[0] if len(found) == 1 else None


def pyproject_sdk(text):
    """The sdk extra's requirement, as one string."""
    extra = tomllib.loads(text)["project"]["optional-dependencies"]["sdk"]
    return extra[0] if len(extra) == 1 else None


def readme_opencode_model(text):
    """The model the README's opencode quick start spawns with."""
    found = re.findall(r"--runner opencode\b.*?--model (\S+)", " ".join(text.split()))
    return found[0] if len(found) == 1 else None


class TheLock(unittest.TestCase):
    def test_it_loads_and_has_every_key(self):
        lock = harness_lock.load()
        self.assertEqual(harness_lock.LOCK_PATH, _LOCK)
        for value in (lock["sdk"]["claude-agent-sdk"], lock["sdk"]["bundled_cli"],
                      lock["opencode"]["version"]):
            self.assertRegex(value, r"^\d+(\.\d+)+$")
        self.assertTrue(lock["models"]["claude"])
        self.assertRegex(lock["models"]["opencode_default"], r"^[^/\s]+/\S+$")

    def test_a_missing_or_partial_lock_is_a_lock_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "harness.lock.toml"
            with self.assertRaisesRegex(harness_lock.LockError, "no harness lock"):
                harness_lock.load(path)
            path.write_text(_LOCK.read_text().replace('bundled_cli = ', 'cli = '))
            with self.assertRaisesRegex(harness_lock.LockError, r"\[sdk\] bundled_cli"):
                harness_lock.load(path)
            path.write_text("[sdk\n")
            with self.assertRaisesRegex(harness_lock.LockError, "cannot read"):
                harness_lock.load(path)


class EveryPinIsTheLocks(unittest.TestCase):
    """Red when a pin moves without the lock, or the lock without it."""

    def setUp(self):
        self.lock = harness_lock.load()

    def test_the_image_installs_the_locked_sdk(self):
        self.assertEqual(requirements_sdk(_read("docker/requirements.txt")),
                         self.lock["sdk"]["claude-agent-sdk"])

    def test_the_image_fetches_the_locked_opencode(self):
        self.assertEqual(dockerfile_opencode(_read("Dockerfile")),
                         self.lock["opencode"]["version"])

    def test_the_sdk_extra_is_the_locked_sdk_exactly(self):
        # exact, not a range: a bare-host `pip install -e ".[sdk]"` gets
        # the tested version (the CHANGELOG's 3.16.0 install note)
        self.assertEqual(pyproject_sdk(_read("pyproject.toml")),
                         "claude-agent-sdk==%s" % self.lock["sdk"]["claude-agent-sdk"])

    def test_default_models_are_the_locked_models(self):
        from cousin_lib.config import DEFAULT_MODELS
        self.assertEqual(list(DEFAULT_MODELS), self.lock["models"]["claude"])

    def test_the_readme_runs_the_locked_opencode_model(self):
        self.assertEqual(readme_opencode_model(_read("README.md")),
                         self.lock["models"]["opencode_default"])


class TheRedCases(unittest.TestCase):
    """Each pin bumped alone, on the real file's text: the readers above
    see the change, so the tests above would go red."""

    def setUp(self):
        self.lock = harness_lock.load()

    def test_requirements_bumped_and_the_lock_not(self):
        text = _read("docker/requirements.txt").replace(
            "claude-agent-sdk==%s" % self.lock["sdk"]["claude-agent-sdk"],
            "claude-agent-sdk==0.2.999")
        self.assertEqual(requirements_sdk(text), "0.2.999")

    def test_the_lock_bumped_and_the_dockerfile_not(self):
        bumped = _read("config/harness.lock.toml").replace(
            'version = "%s"' % self.lock["opencode"]["version"], 'version = "9.9.9"')
        self.assertNotEqual(dockerfile_opencode(_read("Dockerfile")),
                            tomllib.loads(bumped)["opencode"]["version"])

    def test_a_model_added_to_default_models_and_not_the_lock(self):
        from cousin_lib import config
        with mock.patch.object(config, "DEFAULT_MODELS",
                               config.DEFAULT_MODELS + ("claude-sonnet-9",)):
            self.assertNotEqual(list(config.DEFAULT_MODELS), self.lock["models"]["claude"])

    def test_a_range_in_the_sdk_extra(self):
        text = _read("pyproject.toml").replace(
            '"claude-agent-sdk==%s"' % self.lock["sdk"]["claude-agent-sdk"],
            '"claude-agent-sdk>=0.2.159,<0.3"')
        self.assertEqual(pyproject_sdk(text), "claude-agent-sdk>=0.2.159,<0.3")


class TheLiveMatrixStaysOffPublicCi(unittest.TestCase):
    def test_no_workflow_turns_it_on(self):
        for path in sorted((_REPO / ".github" / "workflows").glob("*.y*ml")):
            self.assertNotIn("COUSIN_LIVE", path.read_text(), path)

    @unittest.skipIf(os.environ.get("COUSIN_LIVE") == "1", "COUSIN_LIVE=1 runs the matrix")
    def test_without_the_variable_every_item_is_a_visible_skip(self):
        import io
        from tests.live import test_matrix
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_matrix)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertEqual(result.testsRun, len(test_matrix.ITEMS))
        self.assertEqual(len(result.skipped), result.testsRun)
        for _test, reason in result.skipped:
            self.assertIn("COUSIN_LIVE=1", reason)
        import importlib
        live_main = importlib.import_module("tests.live.__main__")
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(live_main.main(), 2)


def _fake_binary(dirpath, name, body):
    path = pathlib.Path(dirpath) / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class Check(HermeticCase):
    """harness_lock.check: what a kind runs against the lock."""

    def setUp(self):
        self.lock = harness_lock.load()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = tmp.name
        self.sdk = self.lock["sdk"]["claude-agent-sdk"]
        self.cli = self.lock["sdk"]["bundled_cli"]

    def _sdk(self, version, cli=None, bundled=True):
        own = {"installed": version is not None, "cli": cli or self.cli, "bundled": bundled}
        return mock.patch.multiple(
            harness_lock, sdk_version=mock.Mock(return_value=version),
            sdk_cli=mock.Mock(return_value=own))

    def _which(self, name):
        found = pathlib.Path(self.bin) / name
        return str(found) if found.is_file() else None

    def test_the_fake_kind_runs_no_harness(self):
        self.assertIsNone(harness_lock.check("fake"))

    def test_the_locked_sdk_is_ok(self):
        with self._sdk(self.sdk):
            got = harness_lock.check("sdk", lock=self.lock)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["installed"], {"claude-agent-sdk": self.sdk, "cli": self.cli,
                                            "cli_source": "bundled"})
        self.assertIn(self.sdk, got["message"])

    def test_another_installed_sdk_names_both_versions(self):
        # red case 4, at the comparison: importlib.metadata says 0.2.170
        with mock.patch.object(harness_lock.importlib.metadata, "version",
                               return_value="0.2.170"), \
                mock.patch.object(harness_lock, "sdk_cli", return_value={
                    "installed": True, "cli": self.cli, "bundled": True}):
            got = harness_lock.check("sdk", lock=self.lock)
        self.assertFalse(got["ok"])
        self.assertEqual(got["problems"],
                         ["claude-agent-sdk: installed 0.2.170, locked %s" % self.sdk])

    def test_a_missing_sdk_and_another_bundled_cli(self):
        with self._sdk(None):
            self.assertIn("claude-agent-sdk: not installed, locked %s" % self.sdk,
                          harness_lock.check("sdk", lock=self.lock)["problems"])
        with self._sdk(self.sdk, cli="2.0.1"):
            self.assertEqual(harness_lock.check("sdk", lock=self.lock)["problems"],
                             ["bundled Claude Code CLI: installed 2.0.1, locked %s" % self.cli])

    def test_an_sdk_with_no_bundled_cli_reads_claude_on_path(self):
        _fake_binary(self.bin, "claude", 'echo "%s (Claude Code)"' % self.cli)
        with self._sdk(self.sdk, bundled=False):
            got = harness_lock.check("sdk", lock=self.lock, which=self._which)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["installed"]["cli_source"], "path")

    def test_a_claude_whose_version_fails_is_an_unpinned_cli(self):
        # red case 6: the tmux kind's `claude --version` fails
        _fake_binary(self.bin, "claude", "echo broken >&2; exit 1")
        got = harness_lock.check("tmux", lock=self.lock, which=self._which)
        self.assertFalse(got["ok"])
        self.assertEqual(got["problems"], ["unpinned CLI: `claude --version` exited 1"])

    def test_no_claude_on_path_is_an_unpinned_cli(self):
        got = harness_lock.check("tmux", lock=self.lock, which=self._which)
        self.assertEqual(got["problems"], ["unpinned CLI: no `claude` on PATH"])

    def test_a_hung_binary_is_unreadable_not_a_hung_start(self):
        _fake_binary(self.bin, "claude", "exec sleep 30")
        started = time.monotonic()
        with mock.patch.object(harness_lock, "VERSION_TIMEOUT_S", 0.5):
            got = harness_lock.check("tmux", lock=self.lock, which=self._which)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(got["problems"],
                         ["unpinned CLI: `claude --version` gave no answer within 0.5s"])

    def test_a_tmux_cli_other_than_the_locked_one(self):
        _fake_binary(self.bin, "claude", 'echo "2.1.1 (Claude Code)"')
        got = harness_lock.check("tmux", lock=self.lock, which=self._which)
        self.assertEqual(got["problems"],
                         ["claude on PATH: installed 2.1.1, locked %s" % self.cli])

    def test_opencode_reads_its_own_binary(self):
        locked = self.lock["opencode"]["version"]
        same = _fake_binary(self.bin, "oc-same", "echo %s" % locked)
        other = _fake_binary(self.bin, "oc-other", "echo 1.0.0")
        got = harness_lock.check("opencode", {"opencode_bin": same}, lock=self.lock)
        self.assertTrue(got["ok"], got)
        got = harness_lock.check("opencode", {}, lock=self.lock,
                                 environ={"COUSIN_OPENCODE_BIN": other})
        self.assertEqual(got["problems"], ["opencode: installed 1.0.0, locked %s" % locked])
        got = harness_lock.check("opencode", {"opencode_bin": str(pathlib.Path(self.bin) / "x")},
                                 lock=self.lock)
        self.assertEqual(len(got["problems"]), 1)
        self.assertTrue(got["problems"][0].startswith("unpinned CLI: `x --version`: "),
                        got["problems"])

    def test_no_lock_is_a_problem(self):
        with mock.patch.object(harness_lock, "LOCK_PATH", pathlib.Path(self.bin) / "none.toml"):
            got = harness_lock.check("tmux")
        self.assertFalse(got["ok"])
        self.assertIn("no harness lock", got["message"])

    def test_the_runner_cli_line_reads_the_same(self):
        from cousin_lib import migrate
        with mock.patch.object(harness_lock, "sdk_cli", return_value={
                "installed": True, "cli": "2.1.9", "bundled": True}):
            self.assertTrue(migrate.runner_cli().startswith("Claude Code 2.1.9 (bundled"))
        with mock.patch.object(harness_lock, "sdk_cli", return_value={
                "installed": False, "cli": None, "bundled": False}):
            self.assertIn("no claude-agent-sdk installed", migrate.runner_cli())


if __name__ == "__main__":
    unittest.main()
