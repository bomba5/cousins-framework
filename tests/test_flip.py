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
        # Packet injected after respawn. The packet is multi-line, so
        # the fake's one-line-per-call log spreads it across lines;
        # assert against the whole log text rather than per-line.
        log_text = self.log.read_text()
        self.assertIn("BOOT PACKET FOR COUSIN: wren", log_text)
        self.assertIn("Do not announce", log_text)
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


class TestMintSessionId(unittest.TestCase):
    def test_minted_ids_stay_inside_the_charset(self):
        from cousin_lib.flip import _mint_session_id
        for _ in range(20):
            self.assertRegex(_mint_session_id(), r"^[a-z0-9-]+$")

    def test_a_drifted_mint_raises_instead_of_rendering(self):
        # The constraint must travel with the mint and survive -O: if
        # the generation line ever changes to something that can emit
        # shell-relevant characters, the constructor itself refuses.
        from cousin_lib import flip as flip_mod
        with mock.patch.object(flip_mod.uuid, "uuid4",
                               return_value="Bad_ID!;rm"):
            with self.assertRaises(ValueError):
                flip_mod._mint_session_id()


class TestCli(FlipCase):
    def test_dry_run_via_the_cli_exits_zero(self):
        import contextlib
        import io
        from cousin_lib.flip import flip_main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = flip_main(["wren", "--dry-run"])
        # Dry-run assembles and stops before anything destructive; on
        # this fixture it must succeed. (It reaches the real tmux for
        # one read-only has-session query; no session named wren
        # exists, which is exactly the not-alive path.)
        self.assertEqual(rc, 0)
        self.assertIn('"ok": true', out.getvalue())


class TestTranscriptMineStage(FlipCase):
    """The flip mines the dying session's transcript into raw memory
    after capture and before archive: best-effort, recorded as a stage,
    never able to fail the flip."""

    def _configure_harness(self, session_id, text):
        transcripts = self.root / "transcripts"
        transcripts.mkdir()
        (self.root / "config" / "harness.toml").write_text(
            'transcripts_dir = "%s"\n' % transcripts)
        (transcripts / (session_id + ".jsonl")).write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }) + "\n")
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('[runtime]\nsession_id = "%s"\n' % session_id)

    def _stage(self, out, name):
        return next(s for s in out["stages"] if s["stage"] == name)

    def test_dry_run_records_the_skip_and_writes_nothing(self):
        self._configure_harness(
            "old-session-1234", "It failed because the socket was gone.")
        out = self._flip(dry_run=True)
        self.assertEqual(self._stage(out, "transcript_mine"),
                         {"stage": "transcript_mine", "skipped": "dry-run"})
        # the boot assembler lays out memory/ (distill runs on assemble);
        # what must not exist is a raw candidate file
        self.assertEqual(list((self.home / "memory" / "raw").glob("*.jsonl"))
                         if (self.home / "memory" / "raw").exists() else [], [])

    def test_absent_harness_config_records_the_skip(self):
        out = self._flip()
        self.assertTrue(out["ok"], out)
        self.assertEqual(
            self._stage(out, "transcript_mine"),
            {"stage": "transcript_mine",
             "skipped": "config/harness.toml absent"})

    def test_mines_the_old_session_before_archive(self):
        self._configure_harness(
            "old-session-1234",
            "Read the log first. It failed because the socket was gone.")
        out = self._flip()
        self.assertTrue(out["ok"], out)
        self.assertEqual(self._stage(out, "transcript_mine"),
                         {"stage": "transcript_mine", "mined": 1})
        names = [s["stage"] for s in out["stages"]]
        self.assertLess(names.index("capture"),
                        names.index("transcript_mine"))
        self.assertLess(names.index("transcript_mine"),
                        names.index("archive"))
        raw = "".join(p.read_text() for p in
                      (self.home / "memory" / "raw").glob("*.jsonl"))
        self.assertIn("episode:old-sess", raw)
        self.assertIn("socket was gone", raw)
        # The new identity was minted after the mine: the old id is
        # what the transcript belonged to.
        cfg = tomllib.loads((self.home / "cousin.toml").read_text())
        self.assertNotEqual(cfg["runtime"]["session_id"],
                            "old-session-1234")


if __name__ == "__main__":
    unittest.main()
