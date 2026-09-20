"""Loops routes: rows from every cousin's [[loops]] plus the synthetic
heartbeat, the daemon status on every response, recent fires, drift
from the fire log, the per-cousin editor and its round-trips, and fire
as a request row."""
import json
import time
import tomllib
import unittest

from cousin_lib import loops
from tests.console._harness import ConsoleCase

LOOPS = ('\n[[loops]]\nname = "report"\ninterval_seconds = 3600\n'
         'prompt = "write the report now please"\n'
         '[[loops]]\nname = "off"\ninterval_seconds = 60\nprompt = "x"\n'
         'enabled = false\n')


class TestLoopsView(ConsoleCase):
    def test_rows_states_and_daemon(self):
        self.cousin("wren", extra=LOOPS)
        self.cousin("toki", ctype="worker",
                    extra='\n[[loops]]\nname = "grind"\ninterval_seconds = 5\n'
                          'prompt = "g"\n')
        (self.root / "cousins" / "bad").mkdir()
        (self.root / "cousins" / "bad" / "cousin.toml").write_text(
            '[cousin]\nslug = "bad"\n[[loops]]\nname = "X"\nprompt="p"\n')
        now = time.time()
        loops._save_state({"last_tick": now, "last_beat": {"wren": now - 30},
                           "last_fires": {"wren|report": now - 600}})
        self.serve()
        status, body = self.get("/api/loops")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["daemon"]["ok"], body)
        rows = {(r["cousin"], r["name"]): r for r in body["loops"]}
        report = rows[("wren", "report")]
        self.assertEqual(report["state"], "healthy", report)
        self.assertEqual(report["interval"], 3600)
        self.assertEqual(report["schedule"]["interval_seconds"], 3600)
        self.assertEqual(report["lastFireTs"], int(now - 600))
        self.assertGreaterEqual(report["lastTick"], 600)
        self.assertEqual(report["nextFireTs"], int(now - 600 + 3600))
        self.assertEqual(report["note"], "write the report now please")
        self.assertEqual(report["source"], "framework")
        self.assertTrue(report["enabled"])
        self.assertFalse(report["hidden"])
        off = rows[("wren", "off")]
        self.assertEqual(off["state"], "disabled")
        self.assertEqual(off["nextFireTs"], 0)
        beat = rows[("wren", "context-heartbeat")]
        self.assertEqual(beat["state"], "healthy")
        self.assertEqual(beat["interval"], 3600)
        self.assertNotIn(("toki", "context-heartbeat"), rows)
        self.assertEqual(rows[("toki", "grind")]["state"], "idle")
        self.assertEqual(len(body["errors"]), 1, body["errors"])
        self.assertIn("bad", body["errors"][0])

    def test_worker_loop_with_a_failed_last_job_is_failed(self):
        from cousin_lib import jobs
        self.cousin("toki", ctype="worker",
                    extra='\n[[loops]]\nname = "grind"\ninterval_seconds = 5\n'
                          'prompt = "g"\n')
        jid = jobs.register_job(kind="other", title="worker toki|grind",
                                spawned_by="toki")
        jobs.finish_job(jid, status="failed", exit_code=1)
        loops._save_state({"last_tick": time.time(), "last_beat": {},
                           "last_fires": {"toki|grind": time.time() - 5}})
        self.serve()
        _, body = self.get("/api/loops")
        self.assertEqual(body["loops"][0]["state"], "failed")

    def test_recent_fires(self):
        self.cousin("wren")
        self.cousin("toki", ctype="worker")
        now = time.time()
        loops._save_state({"last_tick": now,
                           "last_beat": {"wren": now - 10, "toki": now - 5},
                           "last_fires": {"wren|report": now - 100}})
        self.serve()
        _, body = self.get("/api/loops/recent")
        self.assertEqual([(f["cousin"], f["loop"]) for f in body["fires"]],
                         [("wren", "context-heartbeat"), ("wren", "report")])
        self.assertGreaterEqual(body["fires"][1]["ago"], 100)
        self.assertIn("daemon", body)

    def test_drift_series_from_the_fire_log(self):
        self.cousin("wren", extra=LOOPS)
        fires = self.root / "data" / "loops-fires.jsonl"
        fires.parent.mkdir()
        fires.write_text("".join(
            json.dumps({"ts": t, "cousin": "wren", "loop": "report"}) + "\n"
            for t in (1000, 4600, 8260)) + json.dumps(
                {"ts": 5000, "cousin": "wren", "loop": "other"}) + "\n")
        self.serve()
        _, body = self.get("/api/loops/drift/wren/report")
        self.assertEqual(body["n"], 2)
        self.assertEqual(body["interval"], 3600)
        self.assertEqual(body["points"],
                         [{"t": 4600, "interval": 3600, "drift": 0},
                          {"t": 8260, "interval": 3660, "drift": 60}])
        _, body = self.get("/api/loops/drift/wren/nothing")
        self.assertEqual(body["n"], 0)
        self.assertEqual(self.get("/api/loops/drift/Wren/report")[0], 400)
        self.assertEqual(self.get("/api/loops/drift/wren/Bad!")[0], 400)


class TestEditor(ConsoleCase):
    def test_get_and_post_round_trip(self):
        home = self.cousin("wren", extra=LOOPS)
        self.serve()
        _, body = self.get("/api/cousins/wren/loops")
        self.assertEqual([l["name"] for l in body["loops"]], ["report", "off"])
        self.assertEqual(body["last_beat"], 0)
        self.assertEqual(body["last_fires"], {})
        self.assertIn("daemon", body)
        new = [{"name": "daily", "daily_at": "09:00", "days": ["mon"],
                "prompt": "morning", "enabled": True}]
        status, body = self.post("/api/cousins/wren/loops", {"loops": new})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["loops"][0]["name"], "daily")
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(data["loops"][0]["daily_at"], "09:00")
        self.assertEqual(data["cousin"]["slug"], "wren")
        status, body = self.post("/api/cousins/wren/loops",
                                 {"loops": [{"name": "x", "prompt": "p"}]})
        self.assertEqual(status, 400)
        self.assertIn("[0]", body["error"])
        self.assertEqual(self.post("/api/cousins/wren/loops",
                                   {"loops": "no"})[0], 400)

    def test_hidden_round_trips_through_the_save(self):
        home = self.cousin("wren", extra=LOOPS)
        self.serve()
        status, body = self.post("/api/cousins/wren/loops/report/hidden",
                                 {"hidden": True})
        self.assertEqual(status, 200, body)
        self.assertTrue([l for l in body["loops"]
                         if l["name"] == "report"][0]["hidden"])
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertTrue(data["loops"][0]["hidden"])
        self.assertFalse(data["loops"][1]["enabled"])
        self.assertEqual(self.post("/api/cousins/wren/loops/nope/hidden",
                                   {"hidden": True})[0], 404)

    def test_fire_submits_a_request_row(self):
        self.cousin("wren", extra=LOOPS)
        self.serve()
        status, body = self.post("/api/cousins/wren/loops/report/fire")
        self.assertEqual(status, 202)
        rows = loops.list_requests()
        self.assertEqual(rows[0]["id"], body["request_id"])
        self.assertEqual(json.loads(rows[0]["payload"]), {"loop": "report"})
        status, body = self.post(
            "/api/cousins/wren/loops/context-heartbeat/fire")
        self.assertEqual(status, 202)
        self.assertEqual(self.post("/api/cousins/nobody/loops/x/fire")[0],
                         404)


if __name__ == "__main__":
    unittest.main()
