"""routes_lifecycle: reincarnate and transplant from the console,
each a long operation over cousin_lib.lifecycle. The flip and the
bequest prompt are injected through the server's test seams; nothing is
respawned and no chat server is reached."""
import json
import threading
import time
import tomllib

from cousin_lib.console import longop
from tests.console._harness import ConsoleCase


class LifecycleCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        runner = '\n[agent]\nrunner = "fake"\n'
        self.wren = self.cousin("wren", role="archivist", extra=runner)
        self.owl = self.cousin("owl", role="scout", extra=runner)
        (self.wren / "CLAUDE.md").write_text("# Wren - archivist\n\nbody\n")
        (self.owl / "CLAUDE.md").write_text("# Owl - scout\n\nbody\n")
        (self.wren / "MEMORY.md").write_text("wren remembers\n")
        (self.owl / "MEMORY.md").write_text("owl remembers\n")
        self.serve()
        self.flips, self.prompts = [], []
        self.flip_ok = True
        self.gate = None

        def do_flip(slug, reason=None):
            if self.gate is not None:
                self.gate.wait(5)
            self.flips.append((slug, reason))
            return {"ok": self.flip_ok, "new_generation": 4,
                    **({} if self.flip_ok else {"error": "the pane never came back"})}

        def send(config, text):
            self.prompts.append((config.slug, text))
            (config.home / "data" / "handoff.md").write_text("my bequest\n")
            return {"ok": True}
        self.server.state["lifecycle.do_flip"] = do_flip
        self.server.state["lifecycle.send"] = send

    def wait_done(self, slug, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op = longop.status(self.server, slug)
            if op and op["status"] != "running":
                return op
            time.sleep(0.02)
        self.fail("the op did not finish")

    def stages(self, op):
        return [(s["name"], s["status"]) for s in op["stages"]]


class TestReincarnate(LifecycleCase):
    def test_it_needs_a_confirmation_and_a_one_line_role(self):
        url = "/api/cousins/wren/reincarnate"
        self.assertEqual(self.post(url, {"new_role": "librarian"})[0], 400)
        for role in ("", "   ", "two\nlines", "x" * 201, 7):
            status, _ = self.post(url, {"new_role": role, "confirm": True})
            self.assertEqual(status, 400, role)
        self.assertEqual(self.post(url, {"new_role": "librarian", "confirm": True,
                                         "timeout": 5})[0], 400)
        self.assertEqual(self.flips, [])

    def test_it_runs_snapshot_bequest_rewrite_flip(self):
        status, body = self.post("/api/cousins/wren/reincarnate",
                                 {"new_role": "librarian", "confirm": True})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["kind"], "reincarnate")
        op = self.wait_done("wren")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(self.stages(op), [("snapshot", "done"), ("bequest", "done"),
                                           ("rewrite", "done"), ("flip", "done")])
        self.assertEqual([slug for slug, _ in self.flips], ["wren"])
        self.assertEqual(self.prompts, [])      # a runner's bequest rides the rollover
        self.assertTrue((self.wren / "CLAUDE.md").read_text().startswith("# Wren - librarian"))
        role = tomllib.loads((self.wren / "cousin.toml").read_text())["cousin"]["role"]
        self.assertEqual(role, "librarian")
        self.assertIn("data/lifecycle/wren", op["result"]["snapshot"])

    def test_a_failed_flip_fails_the_op(self):
        self.flip_ok = False
        self.post("/api/cousins/wren/reincarnate", {"new_role": "librarian", "confirm": True})
        op = self.wait_done("wren")
        self.assertEqual(op["status"], "failed")
        self.assertIn("the pane never came back", op["error"])
        self.assertEqual(dict(self.stages(op))["flip"], "failed")

    def test_a_running_flip_refuses_it(self):
        self.server.state.setdefault("flips", {})["wren"] = {"status": "running"}
        status, body = self.post("/api/cousins/wren/reincarnate",
                                 {"new_role": "librarian", "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertTrue(body["busy"])

    def test_unknown_cousin_is_404(self):
        status, _ = self.post("/api/cousins/nope/reincarnate",
                              {"new_role": "librarian", "confirm": True})
        self.assertEqual(status, 404)


class TestTransplant(LifecycleCase):
    URL = "/api/lifecycle/transplant"

    def test_the_modes_are_served(self):
        status, body = self.get("/api/lifecycle/modes")
        self.assertEqual(status, 200)
        modes = {m["id"]: m for m in body["modes"]}
        self.assertEqual(set(modes), {"soul-donation", "body-swap", "merge"})
        self.assertEqual(modes["body-swap"]["confirm"], "typed")
        self.assertEqual(modes["body-swap"]["phrase"], "swap {donor} {recipient}")
        self.assertEqual(modes["soul-donation"]["confirm"], "typed")
        self.assertEqual(modes["soul-donation"]["phrase"], "donate {donor} {recipient}")
        self.assertEqual(modes["merge"]["confirm"], "second")
        self.assertEqual(body["op_kinds"], ["reincarnate", "transplant"])

    def test_refusals(self):
        base = {"donor": "wren", "recipient": "owl", "mode": "merge", "confirm": True}
        for patch, code in (({"mode": "swap"}, 400), ({"recipient": "wren"}, 400),
                            ({"donor": "nope"}, 404), ({"confirm": False}, 400)):
            status, body = self.post(self.URL, dict(base, **patch))
            self.assertEqual(status, code, (patch, body))
        self.assertEqual(self.flips, [])

    def test_body_swap_needs_the_typed_confirmation(self):
        base = {"donor": "wren", "recipient": "owl", "mode": "body-swap"}
        for confirm in (True, "swap owl wren", "owl"):
            status, body = self.post(self.URL, dict(base, confirm=confirm))
            self.assertEqual(status, 400, confirm)
            self.assertIn("swap wren owl", body["error"])
        status, body = self.post(self.URL, dict(base, confirm="swap wren owl"))
        self.assertEqual(status, 202, body)
        op = self.wait_done("owl")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(self.stages(op), [("snapshot", "done"), ("apply", "done"),
                                           ("flip wren", "done"), ("flip owl", "done")])
        self.assertTrue((self.owl / "CLAUDE.md").read_text().startswith("# Wren"))

    def test_soul_donation_needs_its_typed_confirmation(self):
        base = {"donor": "wren", "recipient": "owl", "mode": "soul-donation"}
        status, body = self.post(self.URL, dict(base, confirm=True))
        self.assertEqual(status, 400, body)
        self.assertIn("donate wren owl", body["error"])
        status, body = self.post(self.URL, dict(base, confirm="donate wren owl"))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.wait_done("owl")["status"], "done")

    def test_the_console_writes_who_asked_into_the_lifecycle_audit(self):
        self.post(self.URL, {"donor": "wren", "recipient": "owl", "mode": "merge", "confirm": True})
        self.wait_done("owl")
        self.post("/api/cousins/wren/reincarnate", {"new_role": "librarian", "confirm": True})
        self.wait_done("wren")
        rows = [json.loads(l) for l in
                (self.root / "data" / "lifecycle" / "audit.jsonl").read_text().splitlines()]
        mine = [r for r in rows if r.get("by") == "console"]
        self.assertEqual([(r["op"], r["step"]) for r in mine],
                         [("transplant", "console-request"), ("transplant", "console-result"),
                          ("reincarnate", "console-request"), ("reincarnate", "console-result")])
        self.assertTrue(all("actor" in r and r["op_id"] for r in mine))
        self.assertTrue(mine[1]["ok"])

    def test_the_donor_says_it_is_held_by_the_transplant(self):
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        _, body = self.post(self.URL, {"donor": "wren", "recipient": "owl",
                                       "mode": "merge", "confirm": True})
        status, held = self.get("/api/cousins/wren/lifecycle")
        self.assertEqual(status, 200, held)
        self.assertEqual((held["held"]["recipient"], held["held"]["mode"], held["held"]["op_id"]),
                         ("owl", "merge", body["op"]["id"]))
        self.assertIsNone(self.get("/api/cousins/owl/lifecycle")[1]["held"])
        self.gate.set()
        self.wait_done("owl")
        self.assertIsNone(self.get("/api/cousins/wren/lifecycle")[1]["held"])

    def test_both_cousins_are_held_while_it_runs(self):
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        status, body = self.post(self.URL, {"donor": "wren", "recipient": "owl",
                                            "mode": "merge", "confirm": True})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["slug"], "owl")
        self.assertEqual(body["op"]["params"]["donor"], "wren")
        with self.assertRaises(longop.Busy):
            longop.exclusive(self.server, "wren", "start")
        with self.assertRaises(longop.Busy):
            longop.exclusive(self.server, "owl", "start")
        status, body = self.post("/api/cousins/wren/reincarnate",
                                 {"new_role": "x", "confirm": True})
        self.assertEqual(status, 409, body)
        self.gate.set()
        op = self.wait_done("owl")
        self.assertEqual(op["status"], "done", op)
        longop.exclusive(self.server, "wren", "start").release()   # the donor is free again
        self.assertIn("owl remembers", (self.owl / "MEMORY.md").read_text())
        self.assertIn("wren remembers", (self.owl / "MEMORY.md").read_text())

    def test_a_flip_on_the_donor_refuses_and_holds_nothing(self):
        self.server.state.setdefault("flips", {})["wren"] = {"status": "running"}
        status, body = self.post(self.URL, {"donor": "wren", "recipient": "owl",
                                            "mode": "merge", "confirm": True})
        self.assertEqual(status, 409, body)
        longop.exclusive(self.server, "owl", "start").release()

    def test_an_op_on_the_recipient_refuses_and_releases_the_donor(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        longop.start(self.server, "owl", "login", lambda op: gate.wait(5) and {})
        status, body = self.post(self.URL, {"donor": "wren", "recipient": "owl",
                                            "mode": "merge", "confirm": True})
        self.assertEqual(status, 409, body)
        longop.exclusive(self.server, "wren", "start").release()
        gate.set()


if __name__ == "__main__":
    import unittest
    unittest.main()
