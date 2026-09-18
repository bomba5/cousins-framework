"""Fleet routes: the registry read on every call and enriched, spawn,
dismiss with its archive, start/stop/restart through spawn, the
editors, peer delivery through the destination's chat server, and the
two flip paths."""
import json
import os
import tarfile
import time
import tomllib
import unittest
from unittest import mock

from cousin_lib import loops
from tests.console._harness import ConsoleCase


class TestAttention(ConsoleCase):
    """A running session whose pane shows one of the harness's
    attention_patterns (a login menu, say) is flagged on its row:
    "running" alone read as healthy while the agent waited on a human."""

    def test_a_matching_pane_is_flagged(self):
        (self.root / "config" / "harness.toml").write_text(
            'attention_patterns = ["Select login method"]\n')
        self.cousin("wren")
        self.pane.write_text("Welcome\nSelect login method:\n 1. account\n")
        self.tmux_running()
        self.serve()
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["attention"], "Select login method")

    def test_no_match_or_no_patterns_is_null(self):
        self.cousin("wren")
        self.pane.write_text("Select login method:\n")
        self.tmux_running()
        self.serve()
        self.assertIsNone(self.get("/api/cousins")[1]["cousins"][0]
                          ["attention"])
        (self.root / "config" / "harness.toml").write_text(
            'attention_patterns = ["Paste code here"]\n')
        self.assertIsNone(self.get("/api/cousins")[1]["cousins"][0]
                          ["attention"])

    def test_a_stopped_cousin_is_never_flagged(self):
        (self.root / "config" / "harness.toml").write_text(
            'attention_patterns = ["Select login method"]\n')
        self.cousin("wren")
        self.pane.write_text("Select login method:\n")
        self.serve()
        self.assertIsNone(self.get("/api/cousins")[1]["cousins"][0]
                          ["attention"])


class TestListCousins(ConsoleCase):
    def test_rows_carry_the_contract_keys_from_the_registry(self):
        self.cousin("wren", operator="Sam",
                    extra='\n[memory]\nscope = "shared"\n'
                          '[heartbeat]\ncontext_beat_seconds = 600\n'
                          '[lifecycle]\nflip_at = "04:00"\n')
        self.cousin("toki", port=None, ctype="worker")
        (self.root / "cousins" / "wren" / "data"
         / "last-activity.txt").write_text("2026-01-01T00:00:00: mapping\n")
        self.serve()
        status, body = self.get("/api/cousins")
        self.assertEqual(status, 200)
        rows = {r["slug"]: r for r in body["cousins"]}
        wren = rows["wren"]
        self.assertEqual(wren["name"], "Wren")
        self.assertEqual(wren["role"], "helper")
        self.assertEqual(wren["type"], "cousin")
        self.assertEqual(wren["port"], self.dead_port)
        self.assertIsNone(wren["host"])
        self.assertEqual(wren["home"], str(self.root / "cousins" / "wren"))
        self.assertEqual(wren["tmuxSession"], "wren")
        self.assertEqual(wren["operator"], "Sam")
        self.assertEqual(wren["memoryScope"], "shared")
        self.assertEqual(wren["heartbeat"], 600)
        self.assertEqual(wren["flipAt"], "04:00")
        self.assertFalse(wren["hidden"])
        self.assertEqual(wren["status"], "stopped")
        self.assertEqual(wren["chat"], "down")
        self.assertFalse(wren["active"])
        self.assertEqual(wren["activity"], "2026-01-01T00:00:00: mapping")
        self.assertEqual(wren["lastMsgTs"], 0)
        self.assertEqual(wren["tokensSpent"], 0)
        toki = rows["toki"]
        self.assertEqual(toki["type"], "worker")
        self.assertEqual(toki["status"], "running")
        self.assertEqual(toki["chat"], "none")
        self.assertIsNone(toki["operator"])
        self.assertIsNone(toki["port"])
        # No [runtime] value and no harness file: the model and effort
        # the next start would render are unknown, and null says so.
        self.assertIsNone(wren["model"])
        self.assertIsNone(wren["effort"])
        for dropped in ("tokenBudget", "livenessTick", "cpu", "mem",
                        "chatCount", "lastTick"):
            self.assertNotIn(dropped, wren)

    def test_liveness_chat_and_last_message_are_live_projections(self):
        fake = self.fake_chat("wren", messages=[
            {"id": 1, "type": "user", "timestamp": "2026-01-01T00:00:00+00:00"},
            {"id": 2, "type": "wren",
             "timestamp": "2026-01-02T00:00:00+00:00"},
        ])
        self.cousin("wren", port=fake.port, operator="Sam")
        self.tmux_running(True)
        self.pane.write_text("line one\n")
        self.serve()
        _, body = self.get("/api/cousins")
        row = body["cousins"][0]
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["chat"], "ok")
        self.assertEqual(row["lastMsgTs"], 1767312000)
        # A changed pane within the window marks the cousin active.
        self.pane.write_text("line one\nline two\n")
        _, body = self.get("/api/cousins")
        self.assertTrue(body["cousins"][0]["active"])

    def test_a_cousin_added_after_boot_appears(self):
        self.serve()
        self.assertEqual(self.get("/api/cousins")[1]["cousins"], [])
        self.cousin("wren")
        self.assertEqual([c["slug"] for c in
                          self.get("/api/cousins")[1]["cousins"]], ["wren"])


class TestSpawn(ConsoleCase):
    def _template(self):
        (self.root / "templates").mkdir()
        (self.root / "templates" / "cousin-CLAUDE.template.md").write_text(
            "# {{NAME}} ({{SLUG}}:{{PORT}})\n{{ROLE_ONE_LINE}}\n"
            "{{ROLE_PARAGRAPH}}\n## Voice\n{{VOICE_GUIDE}}\n")

    def test_creates_through_spawn_and_answers_201(self):
        self._template()
        self.serve()
        status, body = self.post("/api/cousins", {
            "slug": "toki", "name": "Toki", "role": "tester",
            "voice": "plain", "port": 8123, "operator": "Sam"})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["slug"], "toki")
        self.assertEqual(body["port"], 8123)
        home = self.root / "cousins" / "toki"
        self.assertEqual(body["home"], str(home))
        self.assertIn("# Toki (toki:8123)", (home / "CLAUDE.md").read_text())
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(data["operator"]["name"], "Sam")

    def test_validation_and_conflicts(self):
        self._template()
        self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins", {
            "slug": "wren", "role": "r", "voice": "v"})
        self.assertEqual(status, 409)
        status, body = self.post("/api/cousins", {
            "slug": "Bad", "role": "r", "voice": "v"})
        self.assertEqual(status, 400)
        status, body = self.post("/api/cousins", {"slug": "toki", "role": "r"})
        self.assertEqual(status, 400)
        self.assertIn("voice", body["error"])
        (self.root / "cousins" / "orphan").mkdir()
        status, body = self.post("/api/cousins", {
            "slug": "orphan", "role": "r", "voice": "v"})
        self.assertEqual(status, 409)


class TestDismiss(ConsoleCase):
    def test_archives_then_removes_and_reports(self):
        home = self.cousin("wren")
        (home / "notes").mkdir()
        (home / "notes" / "only.md").write_text("x")
        self.serve()
        status, body = self.delete("/api/cousins/wren")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "deleted")
        self.assertEqual(body["left_in_place"], [])
        self.assertFalse(home.exists())
        with tarfile.open(body["archive"]) as tf:
            self.assertIn("wren/notes/only.md", tf.getnames())
        self.assertEqual(self.delete("/api/cousins/wren")[0], 404)

    def test_a_failed_archive_refuses_with_500_and_keeps_the_home(self):
        home = self.cousin("wren")
        (self.root / "data").mkdir()
        (self.root / "data" / "dismissed").write_text("file")
        self.serve()
        status, body = self.delete("/api/cousins/wren")
        self.assertEqual(status, 500)
        self.assertTrue(body["error"].startswith("refusing to delete"))
        self.assertTrue(home.is_dir())


class TestStartStopRestart(ConsoleCase):
    def test_start_uses_the_configured_agent_cmd(self):
        self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(status, 500)
        self.assertIn("agent-cmd", body["error"])
        (self.root / "config" / "agent-cmd").write_text("my-agent --x\n")
        with mock.patch("cousin_lib.spawn._default_chat_server",
                        lambda home: None):
            status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "slug": "wren",
                                "status": "started", "chat_server": "started"})
        self.assertIn("new-session", self.tmux_log.read_text())
        self.assertIn("my-agent --x", self.tmux_log.read_text())

    def test_start_when_already_running_and_chat_reused(self):
        fake = self.fake_chat("wren")
        self.cousin("wren", port=fake.port)
        self.tmux_running(True)
        (self.root / "config" / "agent-cmd").write_text("my-agent\n")
        self.serve()
        status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(body["status"], "already running")
        self.assertEqual(body["chat_server"], "reused")
        self.assertNotIn("new-session", self.tmux_log.read_text())

    def test_stop_is_idempotent_and_names_the_halves(self):
        self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/stop")
        self.assertEqual(body, {"ok": True, "slug": "wren", "status": "stopped",
                                "tmux": "already stopped",
                                "chat_server": "not running"})
        self.tmux_running(True)
        status, body = self.post("/api/cousins/wren/stop")
        self.assertEqual(body["tmux"], "stopped")
        self.assertEqual(self.post("/api/cousins/nobody/stop")[0], 404)

    def test_restart_is_stop_then_start_with_the_starts_code(self):
        self.cousin("wren")
        (self.root / "config" / "agent-cmd").write_text("my-agent\n")
        server = self.serve()
        server.settle_seconds = 0
        seen = []
        server.listeners.append(lambda k, d: seen.append((k, d)))
        with mock.patch("cousin_lib.spawn._default_chat_server",
                        lambda home: None):
            status, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["target"], "cousin/wren")
        self.assertEqual(body["stop"]["tmux"], "already stopped")
        self.assertEqual(body["start"]["status"], "started")
        self.assertIn(("cousin-status", {"slug": "wren",
                                         "status": "stopping"}), seen)


class TestEditors(ConsoleCase):
    def test_role_rewrites_the_key_and_keeps_the_rest(self):
        home = self.cousin("wren", extra="\n[runtime]\nsession_id = \"abc\"\n")
        self.serve()
        status, body = self.post("/api/cousins/wren/role",
                                 {"role": 'the "new" role'})
        self.assertEqual(body, {"ok": True, "slug": "wren",
                                "role": 'the "new" role'})
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(data["cousin"]["role"], 'the "new" role')
        self.assertEqual(data["runtime"]["session_id"], "abc")
        self.assertEqual(self.post("/api/cousins/wren/role",
                                   {"role": "x" * 5001})[0], 400)
        self.assertEqual(self.post("/api/cousins/wren/role", {})[0], 400)

    def test_claude_md_read_and_write_with_backup(self):
        home = self.cousin("wren")
        self.serve()
        status, body = self.get("/api/cousins/wren/claude-md")
        self.assertEqual(body["content"], "# wren\n")
        self.assertEqual(body["bytes"], 7)
        self.assertEqual(body["path"], str(home / "CLAUDE.md"))
        status, body = self.post("/api/cousins/wren/claude-md",
                                 {"content": "# new\n"})
        self.assertEqual(body, {"ok": True, "slug": "wren", "bytes": 6})
        self.assertEqual((home / "CLAUDE.md").read_text(), "# new\n")
        backups = list((home / "data" / "claude-md-backups").glob("CLAUDE-*.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "# wren\n")
        self.assertEqual(self.post("/api/cousins/wren/claude-md",
                                   {"content": 5})[0], 400)
        (home / "CLAUDE.md").unlink()
        status, body = self.get("/api/cousins/wren/claude-md")
        self.assertEqual((body["content"], body["missing"]), ("", True))

    def test_hidden_writes_true_and_removes_false(self):
        home = self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/hidden", {"hidden": True})
        self.assertEqual(body, {"ok": True, "slug": "wren", "hidden": True})
        self.assertIn("hidden = true", (home / "cousin.toml").read_text())
        self.assertTrue(self.get("/api/cousins")[1]["cousins"][0]["hidden"])
        self.post("/api/cousins/wren/hidden", {"hidden": False})
        self.assertNotIn("hidden", (home / "cousin.toml").read_text())
        self.assertEqual(self.post("/api/cousins/wren/hidden",
                                   {"hidden": "yes"})[0], 400)


class TestPeer(ConsoleCase):
    def test_delivers_through_the_destination_chat_server(self):
        fake = self.fake_chat("toki")
        self.cousin("wren", name="Wren")
        self.cousin("toki", port=fake.port)
        self.serve()
        status, body = self.post("/api/cousins/wren/peer",
                                 {"to": "toki", "text": "got a sec?"})
        self.assertEqual(body, {"ok": True, "to": "toki", "id": 1})
        self.assertEqual(fake.sends, [{"user": "Wren", "message": "got a sec?"}])
        self.assertEqual(self.post("/api/cousins/wren/peer",
                                   {"to": "wren", "text": "x"})[0], 400)
        self.assertEqual(self.post("/api/cousins/wren/peer",
                                   {"to": "toki", "text": " "})[0], 400)
        self.assertEqual(self.post("/api/cousins/wren/peer",
                                   {"to": "nobody", "text": "x"})[0], 404)
        fake.stop()
        self.assertEqual(self.post("/api/cousins/wren/peer",
                                   {"to": "toki", "text": "x"})[0], 502)


class TestFlip(ConsoleCase):
    def test_immediate_flip_runs_in_the_background_and_reports(self):
        self.cousin("wren")
        server = self.serve()
        calls = []
        seen = []
        server.listeners.append(lambda k, d: seen.append((k, d)))

        def fake_flip(slug, **kw):
            calls.append((slug, kw.get("confirm")))
            return {"slug": slug, "ok": True, "stages": [{"stage": "x"}],
                    "new_generation": 2, "boot_packet_tokens": 10,
                    "degraded_sections": []}
        server.flip_fn = fake_flip
        self.assertEqual(self.get("/api/cousins/wren/flip")[1]["status"],
                         "idle")
        status, body = self.post("/api/cousins/wren/flip", {"confirm": True})
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "running")
        deadline = time.time() + 5
        while time.time() < deadline:
            _, state = self.get("/api/cousins/wren/flip")
            if state["status"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["result"]["new_generation"], 2)
        self.assertEqual(state["stages"], [{"stage": "x"}])
        self.assertEqual(calls, [("wren", True)])
        phases = [d["phase"] for k, d in seen if k == "cousin-flip"]
        self.assertEqual(phases, ["started", "complete"])

    def test_a_running_flip_refuses_a_second_and_cancel(self):
        self.cousin("wren")
        server = self.serve()
        import threading
        gate = threading.Event()

        def slow_flip(slug, **kw):
            gate.wait(5)
            return {"ok": False, "error": "nope", "stages": []}
        server.flip_fn = slow_flip
        self.post("/api/cousins/wren/flip")
        self.assertEqual(self.post("/api/cousins/wren/flip")[0], 409)
        self.assertEqual(self.post("/api/cousins/wren/flip/cancel")[0], 409)
        gate.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            _, state = self.get("/api/cousins/wren/flip")
            if state["status"] == "failed":
                break
            time.sleep(0.05)
        self.assertEqual(state["status"], "failed")

    def test_delayed_flip_is_a_request_row_and_cancel_marks_it(self):
        self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/flip",
                                 {"delay_seconds": 300})
        self.assertEqual(status, 202)
        self.assertEqual(body["delay_seconds"], 300)
        rows = loops.list_requests()
        self.assertEqual(rows[0]["kind"], "flip")
        self.assertEqual(rows[0]["id"], body["request_id"])
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["reason"], "console")
        self.assertAlmostEqual(payload["fire_at"], body["fire_at"])
        _, state = self.get("/api/cousins/wren/flip")
        self.assertEqual(state["pending"]["request_id"], body["request_id"])
        self.assertGreater(state["pending"]["seconds_until_fire"], 250)
        self.assertEqual(self.post("/api/cousins/wren/flip",
                                   {"delay_seconds": 10})[0], 409)
        self.assertEqual(self.post("/api/cousins/wren/flip",
                                   {"delay_seconds": "soon"})[0], 400)
        status, body = self.post("/api/cousins/wren/flip/cancel")
        self.assertEqual(body, {"ok": True, "slug": "wren",
                                "was_pending": True})
        self.assertEqual(loops.list_requests()[0]["status"], "cancelled")
        self.assertEqual(self.post("/api/cousins/wren/flip/cancel")[1]
                         ["was_pending"], False)

    def test_a_stale_marker_is_reported(self):
        home = self.cousin("wren")
        (home / "data" / ".flip-in-progress.json").write_text(
            json.dumps({"started_at": "2026-01-01T00:00:00+00:00",
                        "slug": "wren"}))
        self.serve()
        _, state = self.get("/api/cousins/wren/flip")
        self.assertEqual(state["status"], "stale_marker")
        self.assertIn("marker", state["recovery"])


class TestTokens(ConsoleCase):
    def test_unavailable_without_the_harness_seam(self):
        self.cousin("wren")
        self.serve()
        _, body = self.get("/api/tokens")
        self.assertFalse(body["available"])
        self.assertIn("harness.toml", body["reason"])
        self.assertEqual(body["cousins"], [])

    def test_sums_usage_per_day_from_the_transcript(self):
        home = self.cousin("wren", extra='\n[runtime]\nsession_id = "s1"\n')
        tdir = self.root / "transcripts"
        tdir.mkdir()
        (self.root / "config" / "harness.toml").write_text(
            'transcripts_dir = "%s"\n' % tdir)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [
            {"timestamp": today + "T10:00:00Z",
             "message": {"usage": {"input_tokens": 10, "output_tokens": 5,
                                   "cache_read_input_tokens": 100,
                                   "cache_creation": {
                                       "ephemeral_5m_input_tokens": 1}}}},
            {"timestamp": "2020-01-01T00:00:00Z",
             "message": {"usage": {"input_tokens": 999}}},
            {"timestamp": today + "T11:00:00Z", "message": {"role": "user"}},
        ]
        (tdir / "s1.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
        self.serve()
        _, body = self.get("/api/tokens")
        self.assertTrue(body["available"])
        series = body["cousins"][0]["series"]
        self.assertEqual(len(series), 14)
        self.assertEqual(series[-1], {"day": today, "total": 116, "output": 5})
        self.assertEqual(self.get("/api/cousins")[1]["cousins"][0]
                         ["tokensSpent"], 116)
        # Incremental: an appended line adds to the same day.
        with open(tdir / "s1.jsonl", "a") as fh:
            fh.write(json.dumps({"timestamp": today + "T12:00:00Z",
                                 "message": {"usage": {"output_tokens": 4}}})
                     + "\n")
        _, body = self.get("/api/tokens")
        self.assertEqual(body["cousins"][0]["series"][-1]["total"], 120)


if __name__ == "__main__":
    unittest.main()


class TestModelAndEffort(ConsoleCase):
    """The fleet row's model and effort are the values the next start
    renders (cousin [runtime], else harness [agent] defaults, else
    null); the two POST routes persist through spawn.persist_runtime
    and say a restart is what applies them; the spawn options route
    hands the dialog its catalogue and defaults."""

    def _harness(self, text):
        (self.root / "config" / "harness.toml").write_text(text)

    def test_rows_carry_the_effective_model_and_effort(self):
        self.cousin("wren", extra='\n[runtime]\neffort = "low"\n')
        self.cousin("toki")
        self._harness('[agent]\ndefault_model = "dm"\n'
                      'default_effort = "high"\n')
        self.serve()
        rows = {r["slug"]: r for r in self.get("/api/cousins")[1]["cousins"]}
        self.assertEqual((rows["wren"]["model"], rows["wren"]["effort"]),
                         ("dm", "low"))
        self.assertEqual((rows["toki"]["model"], rows["toki"]["effort"]),
                         ("dm", "high"))

    def test_effort_persists_and_asks_for_a_restart(self):
        home = self.cousin("wren", extra='\n[runtime]\nsession_id = "abc"\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/effort",
                                 {"effort": "max"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "slug": "wren", "effort": "max",
                                "restart_required": True})
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(data["runtime"], {"session_id": "abc",
                                           "effort": "max"})
        self.assertEqual(self.get("/api/cousins")[1]["cousins"][0]["effort"],
                         "max")
        for bad in ({"effort": "ultra"}, {"effort": 3}, {}):
            status, body = self.post("/api/cousins/wren/effort", bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("effort", body["error"])
        self.assertEqual(self.post("/api/cousins/nobody/effort",
                                   {"effort": "low"})[0], 404)

    def test_model_persists_and_asks_for_a_restart(self):
        home = self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/model",
                                 {"model": "m-two"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "slug": "wren", "model": "m-two",
                                "restart_required": True})
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(data["runtime"]["model"], "m-two")
        self.assertEqual(self.get("/api/cousins")[1]["cousins"][0]["model"],
                         "m-two")
        for bad in ({"model": "two words"}, {"model": ""}, {"model": 1}, {}):
            status, body = self.post("/api/cousins/wren/model", bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("model", body["error"])
        self.assertEqual(self.post("/api/cousins/nobody/model",
                                   {"model": "m"})[0], 404)

    def test_spawn_options_without_a_harness_file(self):
        from cousin_lib.config import DEFAULT_MODELS, EFFORT_LEVELS
        self.serve()
        status, body = self.get("/api/spawn/options")
        self.assertEqual(status, 200)
        self.assertEqual(body["models"], list(DEFAULT_MODELS))
        self.assertEqual(body["default_model"], DEFAULT_MODELS[0])
        self.assertEqual(body["efforts"], list(EFFORT_LEVELS))
        self.assertEqual(body["default_effort"], "high")
        self.assertEqual(body["memory_scopes"], ["private", "shared", "both"])
        self.assertEqual(body["default_memory_scope"], "private")
        self.assertEqual(body["default_heartbeat"], 3600)

    def test_spawn_options_from_the_harness_file(self):
        self._harness('[agent]\ndefault_model = "m-two"\n'
                      'default_effort = "low"\nmodels = ["m-one", "m-two"]\n')
        self.serve()
        status, body = self.get("/api/spawn/options")
        self.assertEqual(body["models"], ["m-one", "m-two"])
        self.assertEqual(body["default_model"], "m-two")
        self.assertEqual(body["default_effort"], "low")

    def test_spawn_body_carries_the_four_runtime_fields(self):
        (self.root / "templates").mkdir()
        (self.root / "templates" / "cousin-CLAUDE.template.md").write_text(
            "# {{NAME}}\n{{ROLE_ONE_LINE}}\n{{VOICE_GUIDE}}\n")
        self.serve()
        status, body = self.post("/api/cousins", {
            "slug": "toki", "role": "tester", "voice": "plain",
            "port": 8123, "model": "m-one", "effort": "medium",
            "heartbeat": 600, "memory_scope": "both"})
        self.assertEqual(status, 201, body)
        data = tomllib.loads(
            (self.root / "cousins" / "toki" / "cousin.toml").read_text())
        self.assertEqual(data["runtime"], {"model": "m-one",
                                           "effort": "medium"})
        self.assertEqual(data["heartbeat"]["context_beat_seconds"], 600)
        self.assertEqual(data["memory"]["scope"], "both")
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertEqual((row["model"], row["effort"], row["heartbeat"],
                          row["memoryScope"]),
                         ("m-one", "medium", 600, "both"))
        for bad in ({"effort": "ultra"}, {"memory_scope": "all"},
                    {"heartbeat": 0}, {"heartbeat": "x"},
                    {"model": "two words"}):
            status, body = self.post("/api/cousins", {
                "slug": "kiwi", "role": "r", "voice": "v", **bad})
            self.assertEqual(status, 400, bad)
            self.assertFalse((self.root / "cousins" / "kiwi").exists())


class TestPidAndUptime(ConsoleCase):
    """pid is the pane's process as tmux reports it (list-panes
    #{pane_pid} on the exact session), uptime_seconds its age from
    /proc; both null whenever the cousin is not a local running
    session or the probe answers nothing usable. Never a zero."""

    def test_running_local_cousin_reports_pid_and_uptime(self):
        self.cousin("wren")
        self.tmux_running(True)
        os.environ["FAKE_TMUX_PANE_PID"] = str(os.getpid())
        self.serve()
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertEqual(row["pid"], os.getpid())
        self.assertIsInstance(row["uptime_seconds"], int)
        self.assertGreaterEqual(row["uptime_seconds"], 0)
        calls = [l for l in self.tmux_log.read_text().splitlines()
                 if "list-panes" in l]
        self.assertTrue(calls)
        self.assertIn("-t =wren", calls[0])
        self.assertIn("#{pane_pid}", calls[0])

    def test_stopped_remote_and_worker_rows_are_null(self):
        self.cousin("wren")
        self.cousin("far", extra='host = "elsewhere"\n')
        self.cousin("toki", port=None, ctype="worker")
        os.environ["FAKE_TMUX_PANE_PID"] = str(os.getpid())
        self.serve()
        rows = {r["slug"]: r for r in self.get("/api/cousins")[1]["cousins"]}
        for slug in ("wren", "far", "toki"):
            self.assertIsNone(rows[slug]["pid"], slug)
            self.assertIsNone(rows[slug]["uptime_seconds"], slug)
        self.assertNotIn("list-panes", self.tmux_log.read_text())

    def test_unusable_probe_or_vanished_process_is_null_not_zero(self):
        self.cousin("wren")
        self.tmux_running(True)
        os.environ["FAKE_TMUX_PANE_PID"] = ""
        self.serve()
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertIsNone(row["pid"])
        self.assertIsNone(row["uptime_seconds"])
        # A pid tmux reports but no process answers to: the pid is
        # what tmux said, the age is unknown.
        os.environ["FAKE_TMUX_PANE_PID"] = "4194303"
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertEqual(row["pid"], 4194303)
        self.assertIsNone(row["uptime_seconds"])

    def test_uptime_reads_proc_then_ps_then_gives_up(self):
        from cousin_lib.console.routes_fleet import uptime_seconds
        age = uptime_seconds(os.getpid())
        self.assertIsInstance(age, int)
        self.assertGreaterEqual(age, 0)
        # Without /proc the ps fallback answers for a live process.
        age = uptime_seconds(os.getpid(), proc=str(self.root / "no-proc"))
        self.assertIsInstance(age, int)
        self.assertIsNone(uptime_seconds(4194303))
        self.assertIsNone(uptime_seconds(None))
