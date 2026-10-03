"""cousin-runner's harness check at start (runner/main.harness_at_start):
a `harness` event right after the head, a warning naming both versions
on a mismatch, exit 2 under [agent] strict_harness = true, and the
cousin's `harness:<slug>` row in the health record. The runner is the
fake (patched in for the kind under test), the versions fake metadata
and fake binaries: nothing here needs the SDK or a login."""
import contextlib
import importlib.metadata
import io
import json
import pathlib
import stat
import tempfile
from unittest import mock

from cousin_lib import harness_lock, health
from cousin_lib.runner import main as runner_main
from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _run(argv):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        rc = runner_main.runner_main(argv)
    return rc, stderr.getvalue()


def _events(home):
    out = []
    for path in sorted((home / "data" / "stream").glob("*.jsonl")):
        out += [json.loads(line) for line in path.read_text().splitlines()]
    return out


class _Case(HermeticCase):
    kind = "sdk"

    def setUp(self):
        self.lock = harness_lock.load()
        self.home = temp_home(self, runner=self.kind)
        self.root = self.home.parent.parent
        built = []

        def fake_runner_for(home, *, kind=None):
            # the kind's own checks, then the fake in its place
            runner_main.strict_harness_of(runner_main._agent_table(home))
            built.append(FakeRunner(home))
            return built[-1]
        patcher = mock.patch.object(runner_main, "runner_for", fake_runner_for)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.built = built

    def strict(self, value="true"):
        (self.home / "cousin.toml").write_text(
            (self.home / "cousin.toml").read_text() + "strict_harness = %s\n" % value)

    def run_once(self):
        return _run(["--home", str(self.home), "--once"])

    def harness_events(self):
        return [e for e in _events(self.home) if e["kind"] == "harness"]

    def row(self):
        return health.read(self.root).get("harness:wren")


class SdkLane(_Case):
    @contextlib.contextmanager
    def installed(self, version, cli=None):
        """importlib.metadata says `version` for the SDK (every other
        distribution is read as it is), its wheel bundles `cli`."""
        real = importlib.metadata.version
        own = {"installed": True, "cli": cli or self.lock["sdk"]["bundled_cli"], "bundled": True}

        def fake(name):
            return version if name == harness_lock.SDK_DIST else real(name)
        with mock.patch.object(importlib.metadata, "version", fake), \
                mock.patch.object(harness_lock, "sdk_cli", return_value=own):
            yield

    def test_the_locked_harness_is_one_info_event_after_the_head(self):
        with self.installed(self.lock["sdk"]["claude-agent-sdk"]):
            rc, err = self.run_once()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("harness", err)
        events = _events(self.home)
        self.assertEqual([e["kind"] for e in events[:2]], ["runner", "harness"])
        payload = events[1]["payload"]
        self.assertEqual((payload["ok"], payload["level"], payload["kind"]), (True, "info", "sdk"))
        self.assertEqual(payload["installed"]["claude-agent-sdk"],
                         self.lock["sdk"]["claude-agent-sdk"])
        self.assertEqual(self.row()["state"], "ok")

    def test_another_sdk_is_a_warning_with_both_versions_and_the_runner_starts(self):
        # red case 4: the installed SDK is not the lock's
        locked = self.lock["sdk"]["claude-agent-sdk"]
        with self.installed("0.2.170"):
            rc, err = self.run_once()
        self.assertEqual(rc, 0, err)
        want = "installed 0.2.170, locked %s" % locked
        self.assertIn("warning", err)
        self.assertIn(want, err)
        [event] = self.harness_events()
        self.assertEqual((event["payload"]["ok"], event["payload"]["level"]), (False, "warning"))
        self.assertIn(want, event["payload"]["message"])
        self.assertTrue(self.built[0]._thread is not None, "the runner started")
        row = self.row()
        self.assertEqual(row["state"], "failing")
        self.assertIn(want, row["error"])

    def test_strict_refuses_with_exit_2_naming_both_versions(self):
        # red case 5: the same under [agent] strict_harness = true
        self.strict()
        locked = self.lock["sdk"]["claude-agent-sdk"]
        with self.installed("0.2.170"):
            rc, err = self.run_once()
        self.assertEqual(rc, 2, err)
        self.assertIn("installed 0.2.170, locked %s" % locked, err)
        self.assertIn("strict_harness", err)
        self.assertIsNone(self.built[0]._thread, "a refused runner never starts")
        self.assertEqual(self.harness_events(), [])
        self.assertEqual(self.row()["state"], "failing")

    def test_strict_with_the_locked_harness_starts(self):
        self.strict()
        with self.installed(self.lock["sdk"]["claude-agent-sdk"]):
            rc, err = self.run_once()
        self.assertEqual(rc, 0, err)

    def test_a_quoted_strict_harness_is_exit_2(self):
        self.strict('"true"')
        rc, err = self.run_once()
        self.assertEqual(rc, 2)
        self.assertIn("strict_harness must be true or false", err)

    def test_a_check_that_raises_is_a_warning_not_a_crash(self):
        with mock.patch.object(harness_lock, "check", side_effect=RuntimeError("boom")):
            rc, err = self.run_once()
        self.assertEqual(rc, 0, err)
        self.assertIn("harness check failed: RuntimeError: boom", err)


class TmuxLane(_Case):
    kind = "tmux"

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = pathlib.Path(tmp.name)

    def claude(self, body):
        path = self.bin / "claude"
        path.write_text("#!/bin/sh\n%s\n" % body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return mock.patch.object(harness_lock.shutil, "which",
                                 lambda name: str(path) if name == "claude" else None)

    def test_a_claude_version_that_fails_is_an_unpinned_cli_warning(self):
        # red case 6: never silence
        with self.claude("exit 1"):
            rc, err = self.run_once()
        self.assertEqual(rc, 0, err)
        self.assertIn("unpinned CLI", err)
        [event] = self.harness_events()
        self.assertEqual(event["payload"]["level"], "warning")
        self.assertIn("unpinned CLI: `claude --version` exited 1", event["payload"]["message"])

    def test_the_locked_cli_on_path_is_ok(self):
        with self.claude('echo "%s (Claude Code)"' % self.lock["sdk"]["bundled_cli"]):
            rc, err = self.run_once()
        self.assertEqual(rc, 0, err)
        [event] = self.harness_events()
        self.assertTrue(event["payload"]["ok"], event)
        self.assertEqual(event["payload"]["installed"]["cli_source"], "path")


class FakeLane(_Case):
    kind = "fake"

    def test_the_fake_kind_has_no_harness_event_and_no_row(self):
        rc, err = self.run_once()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.harness_events(), [])
        self.assertIsNone(self.row())
