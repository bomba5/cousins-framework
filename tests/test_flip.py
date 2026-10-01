"""The flip: end a generation, start the next, verified.

Tested against a real framework root and the fake tmux executable -
the flip drives the same subprocess path production does. Timing knobs
are injected so deadlines are test-sized, never the behavior itself.
"""
import json
import os
import pathlib
import stat
import time
import unittest
from unittest import mock

from tests._hermetic import HermeticCase

from cousin_lib.flip import flip
from tests._fakes import agent_on_path
from tests._fakes import _FAKE_TMUX


class FlipCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory").mkdir()
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n'
            '[chat]\nport = 8100\ntmux_session = "wren"\n'
        )
        (self.home / "STATUS.md").write_text(
            "# Wren - STATUS\n\n## Open loops\n\n- ship it\n")
        (self.root / "config").mkdir()
        (self.root / "config" / "law.md").write_text("1. Be authored.\n")
        (self.root / "config" / "agent-cmd").write_text(
            "my-agent --sid {session_id}\n")
        self.tmux = self.root / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.log = self.root / "tmux-calls.log"
        self.pane = self.root / "pane.txt"
        self.pane.write_text("working on the report\n")
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "FAKE_TMUX_LOG": str(self.log),
            "FAKE_TMUX_PANE": str(self.pane),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        agent_on_path(self, self.root)

    def _flip(self, **kw):
        kw.setdefault("tmux_bin", str(self.tmux))
        kw.setdefault("handoff_deadline", 1)
        kw.setdefault("halfway", 0.4)
        kw.setdefault("settle", 0)
        return flip("wren", **kw)

    def _calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class TestLegacyFlipRefused(FlipCase):
    """A cousin with no [agent] runner is refused by name before any
    tmux call; 2.0.0 has no legacy lane to flip it on."""

    def test_flipping_a_cousin_with_no_runner_is_refused_with_the_line(self):
        from cousin_lib.delivery import lane_refusal
        for kw in ({"confirm": True}, {"dry_run": True}):
            out = self._flip(**kw)
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["error"], lane_refusal(self.home))
            self.assertEqual(self._calls(), [])
        self.assertFalse((self.home / "data" / ".flip-in-progress.json").exists())


class TestMintSessionId(unittest.TestCase):
    def test_minted_ids_stay_inside_the_charset(self):
        from cousin_lib.spawn import _mint_session_id
        for _ in range(20):
            self.assertRegex(_mint_session_id(), r"^[a-z0-9-]+$")

    def test_a_drifted_mint_raises_instead_of_rendering(self):
        # The constraint must travel with the mint and survive -O: if
        # the generation line ever changes to something that can emit
        # shell-relevant characters, the constructor itself refuses.
        from cousin_lib import spawn as spawn_mod
        with mock.patch.object(spawn_mod.uuid, "uuid4",
                               return_value="Bad_ID!;rm"):
            with self.assertRaises(ValueError):
                spawn_mod._mint_session_id()


class TestCli(FlipCase):
    def test_root_flag_is_accepted(self):
        import contextlib
        import io
        from cousin_lib.flip import flip_main
        seen = {}

        def fake_flip(slug, **kw):
            seen.update(kw, slug=slug,
                        root=os.environ.get("FRAMEWORK_ROOT"))
            return {"ok": True}
        out = io.StringIO()
        with mock.patch.dict(os.environ):
            os.environ.pop("FRAMEWORK_ROOT", None)
            with mock.patch("cousin_lib.flip.flip", fake_flip), \
                    contextlib.redirect_stdout(out):
                rc = flip_main(["wren", "--root", str(self.root),
                                "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(pathlib.Path(seen["root"]), self.root)

if __name__ == "__main__":
    unittest.main()


class TestFlipOnTheRunnerLane(HermeticCase):
    def _cousin(self, slug="wren"):
        from tests.runner._home import temp_home
        home = temp_home(self, slug=slug, runner="fake")
        root = home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}); p.start(); self.addCleanup(p.stop)
        return home

    def test_a_running_runner_rolls_over_and_tmux_is_never_touched(self):
        from cousin_lib import boot, flip
        from cousin_lib.runner.fake import FakeRunner
        from cousin_lib.runner.main import hold_lock
        home = self._cousin()
        with hold_lock(home):
            r = FakeRunner(home); r.start(); self.addCleanup(lambda: r.stop(timeout=5))
            with mock.patch("subprocess.run") as run:
                out = flip.flip("wren", confirm=True, reason="cousin-flip")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["lane"], "runner")
        self.assertEqual(out["stages"][0]["stage"], "rollover")
        self.assertEqual(out["stages"][0]["reason"], "cousin-flip")      # the model sees why
        self.assertEqual(boot.read_generation(home), 1)
        self.assertFalse(any("tmux" in str(c) for c in run.call_args_list))

    def test_a_stopped_runner_is_refused_and_nothing_is_queued(self):
        from cousin_lib import flip
        from cousin_lib.runner.inbox import Inbox
        home = self._cousin()
        out = flip.flip("wren", confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("not running", out["error"])
        self.assertEqual(Inbox(home).pending(), 0)

    def test_queue_if_stopped_leaves_a_durable_row(self):
        from cousin_lib import flip
        from cousin_lib.runner.inbox import Inbox
        home = self._cousin()
        out = flip.flip("wren", confirm=True, queue_if_stopped=True)
        self.assertFalse(out["ok"])
        self.assertEqual(Inbox(home).pending(), 1)
