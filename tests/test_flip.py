"""The flip: end a generation, start the next, verified.

Tested against a real framework root and the fake tmux executable -
the flip drives the same subprocess path production does. Timing knobs
are injected so deadlines are test-sized, never the behavior itself.
"""
import json
import os
import pathlib
import stat
import threading
import time
import tomllib
import unittest
from unittest import mock

from cousin_lib.flip import flip
from tests.server.test_injection import _FAKE_TMUX


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

    def _flip(self, **kw):
        kw.setdefault("tmux_bin", str(self.tmux))
        kw.setdefault("handoff_deadline", 1)
        kw.setdefault("halfway", 0.4)
        kw.setdefault("settle", 0)
        return flip("wren", **kw)

    def _calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class TestDryRun(FlipCase):
    def test_assembles_but_kills_and_spawns_nothing(self):
        out = self._flip(dry_run=True)
        self.assertTrue(out["ok"])
        calls = " ".join(self._calls())
        self.assertNotIn("kill-session", calls)
        self.assertNotIn("new-session", calls)
        self.assertFalse(
            (self.home / "data" / ".flip-in-progress.json").exists())


class TestFullFlip(FlipCase):
    def test_end_to_end_with_a_cooperative_cousin(self):
        # The "cousin" writes its handoff shortly after the prompt.
        def cousin_writes():
            time.sleep(0.3)
            (self.home / "data" / "handoff.md").write_text("# H\nok\n")
        threading.Thread(target=cousin_writes, daemon=True).start()
        out = self._flip()
        self.assertTrue(out["ok"], out)
        stages = {s["stage"]: s for s in out["stages"]}
        self.assertTrue(stages["wait_handoff"]["wrote_clean"])
        # Generation bumped, packet written.
        self.assertEqual(out["new_generation"], 1)
        packet = self.home / "data" / "boot-packet-gen-0001.md"
        self.assertTrue(packet.is_file())
        self.assertIn("BOOT PACKET FOR COUSIN: wren", packet.read_text())
        # Killed and respawned through the single tmux site with the
        # session id rendered into the agent command.
        calls = self._calls()
        self.assertTrue(any("kill-session" in c for c in calls))
        spawn = next(c for c in calls if "new-session" in c)
        self.assertIn("my-agent --sid ", spawn)
        self.assertNotIn("{session_id}", spawn)
        # The minted id persisted into cousin.toml and the file still
        # parses.
        cfg = tomllib.loads((self.home / "cousin.toml").read_text())
        sid = cfg["runtime"]["session_id"]
        self.assertIn(sid, spawn)
        # Packet injected after respawn.
        pastes = [c for c in calls if " -l " in c]
        self.assertTrue(any("BOOT PACKET" in c for c in pastes))
        # Marker cleared.
        self.assertFalse(
            (self.home / "data" / ".flip-in-progress.json").exists())

    def test_silent_cousin_gets_an_emergency_handoff(self):
        out = self._flip()
        stages = {s["stage"]: s for s in out["stages"]}
        self.assertFalse(stages["wait_handoff"]["wrote_clean"])
        handoff = (self.home / "data" / "handoff.md").read_text()
        self.assertIn("EMERGENCY HANDOFF", handoff)
        self.assertIn("degraded_state: true", handoff)
        # The flip still completes: emergency is degraded, not fatal.
        self.assertTrue(out["ok"])

    def test_prior_generation_is_archived(self):
        (self.home / "data" / "handoff.md").write_text("# old handoff\n")
        (self.home / "data" / "boot-packet-gen-0000.md").write_text("old\n")
        self._flip()
        archived = list((self.home / "data" / "generations").glob("*"))
        self.assertEqual(len(archived), 1)
        names = {p.name for p in archived[0].iterdir()}
        self.assertIn("handoff.md", names)


class TestGuards(FlipCase):
    def test_fresh_marker_refuses_a_concurrent_flip(self):
        marker = self.home / "data" / ".flip-in-progress.json"
        marker.write_text(json.dumps({
            "started_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
        }))
        out = self._flip()
        self.assertFalse(out["ok"])
        self.assertIn("concurrent", out["error"])

    def test_stale_marker_passes_through(self):
        marker = self.home / "data" / ".flip-in-progress.json"
        marker.write_text(json.dumps(
            {"started_at": "2020-01-01T00:00:00+00:00"}))
        out = self._flip()
        self.assertTrue(out["ok"], out)

    def test_missing_agent_cmd_fails_preflight_before_any_damage(self):
        (self.root / "config" / "agent-cmd").unlink()
        out = self._flip()
        self.assertFalse(out["ok"])
        self.assertIn("preflight", out["error"])
        calls = " ".join(self._calls())
        self.assertNotIn("kill-session", calls)


if __name__ == "__main__":
    unittest.main()
