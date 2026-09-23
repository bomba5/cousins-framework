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

from tests._hermetic import HermeticCase

from cousin_lib.flip import flip
from tests._fakes import agent_on_path
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
        agent_on_path(self, self.root)
        # A flip respawns through start_cousin, whose default launches a
        # REAL chat server; in a test it outlived its deleted temp home
        # on :8100 (install re-test 5). The respawn is what is tested,
        # not the server.
        import functools
        from cousin_lib import flip as flip_mod
        start = mock.patch.object(flip_mod, "start_cousin", functools.partial(
            flip_mod.start_cousin, start_chat_server=lambda home: None))
        start.start()
        self.addCleanup(start.stop)

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

    def test_distilled_floor_includes_the_flip_record(self):
        # The flip records itself after the packet is assembled; the
        # floor used to lag by that one entry until the next start.
        self._flip()
        before = {p.name: p.read_text() for p in
                  (self.home / "memory" / "distilled").glob("*.md")}
        from cousin_lib import distill
        distill.distill(self.home)
        after = {p.name: p.read_text() for p in
                 (self.home / "memory" / "distilled").glob("*.md")}
        self.assertEqual(before, after)
        self.assertIn("flipped to generation 1", "".join(after.values()))

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
        from cousin_lib import spawn as spawn_mod
        with mock.patch.object(spawn_mod.uuid, "uuid4",
                               return_value="Bad_ID!;rm"):
            with self.assertRaises(ValueError):
                flip_mod._mint_session_id()


class TestHostPreflight(FlipCase):
    """A flip checks what the respawn needs from the host before it
    touches the running session: tmux, and the agent command's
    executable. Without them the old session would be killed and the
    new one would never start."""

    def test_missing_tmux_fails_preflight_cleanly(self):
        out = self._flip(tmux_bin=str(self.root / "no-such-tmux"))
        self.assertFalse(out["ok"])
        self.assertIn("preflight", out["error"])
        self.assertIn("tmux", out["error"])
        self.assertFalse(self.log.exists())

    def test_missing_tmux_fails_a_dry_run_too_without_a_traceback(self):
        out = self._flip(tmux_bin=str(self.root / "no-such-tmux"),
                         dry_run=True)
        self.assertFalse(out["ok"])
        self.assertIn("tmux", out["error"])

    def test_unresolvable_agent_fails_preflight_before_any_damage(self):
        (self.root / "config" / "agent-cmd").write_text(
            "not-an-agent-anywhere --sid {session_id}\n")
        out = self._flip()
        self.assertFalse(out["ok"])
        self.assertIn("not-an-agent-anywhere", out["error"])
        self.assertNotIn("kill-session", " ".join(self._calls()))


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

    @unittest.skipUnless(__import__("shutil").which("tmux"),
                         "tmux is a documented prerequisite; not installed")
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


class TestFlipRendersModelAndEffort(FlipCase):
    """The agent-cmd's {model} and {effort} placeholders reach the
    respawn rendered, and a placeholder with no value anywhere fails
    preflight before the old session is killed."""

    def test_unrenderable_placeholder_fails_preflight_before_any_damage(self):
        (self.root / "config" / "agent-cmd").write_text(
            "my-agent --effort {effort} --sid {session_id}\n")
        out = self._flip()
        self.assertFalse(out["ok"])
        self.assertIn("preflight", out["error"])
        self.assertIn("{effort}", out["error"])
        self.assertIn("harness.toml", out["error"])
        self.assertNotIn("kill-session", " ".join(self._calls()))

    def test_values_render_from_the_cousin_and_the_install(self):
        (self.root / "config" / "agent-cmd").write_text(
            "my-agent --model {model} --effort {effort} --sid {session_id}\n")
        (self.root / "config" / "harness.toml").write_text(
            '[agent]\ndefault_model = "dm"\n')
        (self.home / "cousin.toml").write_text(
            (self.home / "cousin.toml").read_text()
            + '\n[runtime]\neffort = "low"\n')
        out = self._flip()
        self.assertTrue(out["ok"], out)
        spawn = [c for c in self._calls() if "new-session" in c][-1]
        self.assertIn("--model dm --effort low", spawn)
        self.assertNotIn("{", spawn)


class TestHandoffPromptAsksForMemory(FlipCase):
    """The pre-exit prompt carries a fourth write: what the session
    learned goes into memory, with its truth level, before it ends."""

    def test_flip_prompt_names_the_memory_step(self):
        from cousin_lib.flip import handoff_prompt
        text = handoff_prompt()
        self.assertIn("[cousin-flip in progress] Four pre-exit writes", text)
        self.assertIn("3. Save what this session learned", text)
        # The handoff is the done signal, so every other write comes
        # before it.
        self.assertIn("4. LAST, write your handoff", text)
        for earlier in ("STATUS.md", "active-threads.md",
                        "cousin-memory remember"):
            self.assertLess(text.index(earlier),
                            text.index("data/handoff.md"), earlier)
        self.assertIn("cousin-memory remember", text)
        self.assertIn("cousin-memory decide", text)
        self._flip()
        self.assertIn("cousin-memory remember", self.log.read_text())

    def test_stop_prompt_says_stop_not_flip(self):
        from cousin_lib.flip import handoff_prompt
        text = handoff_prompt("cousin-stop", "The cousin is being stopped.")
        self.assertTrue(text.startswith("[cousin-stop in progress]"))
        self.assertIn("3. Save what this session learned", text)
        self.assertTrue(text.endswith("The cousin is being stopped."))


class TestCloseSession(FlipCase):
    """A clean stop is the first half of a flip, then the stop: the
    next packet waits in data/pending-boot.json for the next start."""

    def _close(self, **kw):
        from cousin_lib.flip import close_session
        kw.setdefault("tmux_bin", str(self.tmux))
        kw.setdefault("handoff_deadline", 1)
        kw.setdefault("halfway", 0.4)
        with mock.patch("cousin_lib.spawn._pid_bound_to_port",
                        lambda port: None):
            return close_session("wren", **kw)

    def test_a_live_cousin_hands_off_and_leaves_a_pending_packet(self):
        def cousin_writes():
            time.sleep(0.3)
            (self.home / "data" / "handoff.md").write_text("# H\nok\n")
        threading.Thread(target=cousin_writes, daemon=True).start()
        out = self._close()
        self.assertTrue(out["ok"], out)
        stages = {s["stage"]: s for s in out["stages"]}
        self.assertTrue(stages["wait_handoff"]["wrote_clean"])
        self.assertIn("transcript_mine", stages)
        self.assertEqual(out["new_generation"], 1)
        log_text = self.log.read_text()
        self.assertIn("[cousin-stop in progress]", log_text)
        self.assertIn("cousin-memory remember", log_text)
        calls = self._calls()
        self.assertTrue(any("kill-session" in c for c in calls))
        self.assertFalse(any("new-session" in c for c in calls))
        pending = json.loads(
            (self.home / "data" / "pending-boot.json").read_text())
        self.assertEqual(pending["generation"], 1)
        self.assertIn("BOOT PACKET FOR COUSIN: wren",
                      pathlib.Path(pending["packet"]).read_text())
        self.assertFalse(
            (self.home / "data" / ".flip-in-progress.json").exists())

    def test_a_stopped_cousin_is_just_stopped(self):
        os.environ["FAKE_TMUX_RC"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_TMUX_RC", None)
        out = self._close()
        self.assertTrue(out["ok"])
        self.assertEqual(out["stop"]["tmux"], "already stopped")
        self.assertFalse((self.home / "data" / "pending-boot.json").exists())
        self.assertNotIn("cousin-stop in progress",
                         self.log.read_text() if self.log.exists() else "")

    def test_refuses_while_a_flip_is_running(self):
        (self.home / "data" / ".flip-in-progress.json").write_text(
            json.dumps({"started_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat()}))
        out = self._close()
        self.assertFalse(out["ok"])
        self.assertIn("refusing", out["error"])

    def test_a_flip_supersedes_a_pending_packet(self):
        pending = self.home / "data" / "pending-boot.json"
        packet = self.home / "data" / "boot-packet-gen-0009.md"
        packet.write_text("old packet\n")
        pending.write_text(json.dumps({"generation": 9,
                                       "packet": str(packet)}))
        out = self._flip()
        self.assertTrue(out["ok"], out)
        self.assertFalse(pending.exists())
        self.assertNotIn("the last session closed cleanly",
                         self.log.read_text())


class TestAssembleSeesTheDyingSessionId(FlipCase):
    """The boot packet's MCP warning scopes itself to the generation
    that just died by reading `runtime.session_id` off cousin.toml, and
    that is correct only because the flip assembles the packet BEFORE
    it persists the new id. Move the persist earlier and the warning
    scopes to a session with no log, returns None and goes silent
    forever: a diagnostic that dies quietly, which is the whole defect
    #54 was about. Nothing else asserts this order."""

    def test_the_packet_is_assembled_before_the_new_id_is_persisted(self):
        from cousin_lib import boot, flip as flip_mod
        from cousin_lib.config import read_session_id
        (self.home / "cousin.toml").write_text(
            (self.home / "cousin.toml").read_text()
            + '\n[runtime]\nsession_id = "dying-generation"\n')
        seen = []
        real = boot.assemble

        def watched(slug, home, **kw):
            seen.append(read_session_id(home))
            return real(slug, home, **kw)

        with mock.patch.object(flip_mod.boot, "assemble", watched):
            def cousin_writes():
                time.sleep(0.3)
                (self.home / "data" / "handoff.md").write_text("# H\nok\n")
            threading.Thread(target=cousin_writes, daemon=True).start()
            out = self._flip()
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen, ["dying-generation"])
        self.assertNotEqual(
            read_session_id(self.home), "dying-generation",
            "the flip should have persisted a new id after assembling")


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
