"""console/longop: a long operation on one cousin (a kind switch, an
account login) run on a thread with its stages reported as they happen,
a `cousin-op` event per step and GET /api/cousins/<slug>/op for its
state; one at a time per cousin, and never beside a flip or a clean
stop (the flip thread's pattern, generalized)."""
import threading
import time
import unittest

from cousin_lib.console import longop
from tests.console._harness import ConsoleCase


class LongOpCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.cousin("wren")
        self.serve()
        self.events = []
        self.server.listeners.append(lambda kind, data: self.events.append((kind, data)))

    def wait_done(self, slug="wren", timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op = longop.status(self.server, slug)
            if op and op["status"] != "running":
                return op
            time.sleep(0.02)
        self.fail("the op did not finish")


class Lifecycle(LongOpCase):
    def test_stages_result_and_events(self):
        def work(op):
            op.stage("close", "done")
            op.stage("import", "done", "3 rows")
            return {"ok": True, "switched": "sdk"}
        entry = longop.start(self.server, "wren", "migrate", work, params={"to": "sdk"})
        self.assertEqual((entry["status"], entry["kind"], entry["slug"]),
                         ("running", "migrate", "wren"))
        op = self.wait_done()
        self.assertEqual(op["status"], "done")
        self.assertEqual(op["result"], {"ok": True, "switched": "sdk"})
        self.assertEqual([(s["name"], s["status"], s["detail"]) for s in op["stages"]],
                         [("close", "done", None), ("import", "done", "3 rows")])
        self.assertEqual(op["params"], {"to": "sdk"})
        self.assertIsNotNone(op["finished_at"])
        phases = [d["phase"] for k, d in self.events if k == longop.EVENT]
        self.assertEqual(phases, ["started", "stage", "stage", "done"])
        self.assertTrue(all(d["id"] == entry["id"] and d["slug"] == "wren"
                            for k, d in self.events if k == longop.EVENT))

    def test_a_raise_or_ok_false_is_failed_with_its_error(self):
        def boom(op):
            op.stage("close", "running")
            raise longop.OpError("the supervisor is gone")
        longop.start(self.server, "wren", "migrate", boom)
        op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertIn("the supervisor is gone", op["error"])
        self.assertEqual(op["stages"][-1]["status"], "failed")
        self.assertEqual(self.events[-1][1]["phase"], "failed")

        longop.start(self.server, "wren", "migrate", lambda op: {"ok": False, "error": "no"})
        op = self.wait_done()
        self.assertEqual((op["status"], op["error"]), ("failed", "no"))

    def test_one_op_per_cousin_at_a_time(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        longop.start(self.server, "wren", "login", lambda op: gate.wait(5) and {})
        with self.assertRaises(longop.Busy):
            longop.start(self.server, "wren", "migrate", lambda op: {})
        self.cousin("owl")
        longop.start(self.server, "owl", "login", lambda op: {})     # another cousin: fine
        gate.set()
        self.wait_done()
        longop.start(self.server, "wren", "migrate", lambda op: {})  # after it: fine
        self.wait_done()

    def test_never_beside_a_flip_or_clean_stop(self):
        self.server.state.setdefault("flips", {})["wren"] = {"status": "running",
                                                              "started_at": time.time()}
        with self.assertRaises(longop.Busy) as cm:
            longop.start(self.server, "wren", "migrate", lambda op: {})
        self.assertIn("flip", str(cm.exception))

    def test_a_flip_is_refused_while_an_op_runs(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        longop.start(self.server, "wren", "migrate", lambda op: gate.wait(5) and {})
        status, body = self.post("/api/cousins/wren/flip", {})
        self.assertEqual(status, 409, body)
        self.assertIn("migrate", body["error"])
        gate.set()
        self.wait_done()


class Robustness(LongOpCase):
    """An op ends failed on any exit, and only an op error shows its words."""

    def test_a_system_exit_still_ends_the_op_failed(self):
        def work(op):
            op.stage("parse", "running")
            import argparse
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                argparse.ArgumentParser().parse_args(["--bogus"])
        longop.start(self.server, "wren", "migrate", work)
        op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertIsNone(longop.running(self.server, "wren"))
        longop.start(self.server, "wren", "migrate", lambda op: {})    # not locked
        self.wait_done()

    def test_only_an_op_error_says_its_words(self):
        import contextlib
        import io

        def leaky(op):
            op.stage("login", "running")
            raise RuntimeError("login failed for key sk-ant-SECRET123456")
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            longop.start(self.server, "wren", "login", leaky)
            op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertNotIn("SECRET", str(op))
        self.assertNotIn("SECRET", str(self.events))
        self.assertIn("RuntimeError", op["error"])
        self.assertIn("console log", op["error"])
        self.assertIn("SECRET123456", log.getvalue())     # the host's log keeps it

        def said(op):
            raise longop.OpError("the account has no login yet")
        longop.start(self.server, "wren", "login", said)
        op = self.wait_done()
        self.assertEqual(op["error"], "the account has no login yet")


class FleetRefusesDuringAnOp(LongOpCase):
    """Dismiss, start, stop and restart are 409
    while an op runs on the cousin, as the flip is."""

    def test_each_is_409(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        longop.start(self.server, "wren", "migrate", lambda op: gate.wait(5) and {})
        for method, path in (("POST", "/api/cousins/wren/start"),
                             ("POST", "/api/cousins/wren/stop"),
                             ("POST", "/api/cousins/wren/restart"),
                             ("DELETE", "/api/cousins/wren")):
            status, body = self.request(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 409, (path, body))
            self.assertIn("migrate", body["error"])
        self.assertTrue((self.root / "cousins" / "wren").is_dir())
        gate.set()
        self.wait_done()


class Exclusive(LongOpCase):
    """routes_fleet's five routes (dismiss, start,
    stop, restart, set_auth) used to read op_running() unlocked and then
    do their own unlocked work, marking the cousin busy nowhere - a
    longop.start() (a migrate, a login) could start beside them. exclusive()
    is the fix: under the same lock, refuse as op_running's callers
    already do, else occupy the same table start() reads."""

    def test_marks_busy_so_a_longop_refuses(self):
        hold = longop.exclusive(self.server, "wren", "dismiss")
        with self.assertRaises(longop.Busy) as cm:
            longop.start(self.server, "wren", "migrate", lambda op: {})
        self.assertIn("dismiss", str(cm.exception))
        self.assertIn("wren", str(cm.exception))
        hold.release()
        longop.start(self.server, "wren", "migrate", lambda op: {})   # released: fine
        self.wait_done()

    def test_release_is_idempotent_and_works_as_a_context_manager(self):
        with longop.exclusive(self.server, "wren", "start") as hold:
            self.assertEqual(longop.op_running(self.server, "wren"), "start")
        self.assertIsNone(longop.op_running(self.server, "wren"))
        hold.release()          # a second release is a no-op, not an error

    def test_releases_on_the_callers_error_path(self):
        with self.assertRaises(ValueError):
            with longop.exclusive(self.server, "wren", "dismiss"):
                raise ValueError("boom")
        self.assertIsNone(longop.op_running(self.server, "wren"))
        longop.start(self.server, "wren", "migrate", lambda op: {})    # not left held
        self.wait_done()

    def test_refuses_beside_a_flip(self):
        self.server.state.setdefault("flips", {})["wren"] = {
            "status": "running", "started_at": time.time()}
        with self.assertRaises(longop.Busy) as cm:
            longop.exclusive(self.server, "wren", "start")
        self.assertIn("flip", str(cm.exception))

    def test_refuses_beside_a_running_op(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        longop.start(self.server, "wren", "migrate", lambda op: gate.wait(5) and {})
        with self.assertRaises(longop.Busy) as cm:
            longop.exclusive(self.server, "wren", "start")
        self.assertIn("migrate", str(cm.exception))
        gate.set()
        self.wait_done()

    def test_a_held_mark_is_invisible_to_status_and_the_op_route(self):
        # A held entry must not leak through as
        # a fake running op, on either the function or the GET route -
        # op_running() is the only thing that needs to see it.
        hold = longop.exclusive(self.server, "wren", "dismiss")
        self.assertIsNone(longop.status(self.server, "wren"))
        self.assertEqual(longop.op_running(self.server, "wren"), "dismiss")
        status, body = self.get("/api/cousins/wren/op")
        self.assertEqual((status, body), (200, {"ok": True, "op": None}))
        hold.release()
        self.assertIsNone(longop.status(self.server, "wren"))
        self.assertIsNone(self.get("/api/cousins/wren/op")[1]["op"])

    def test_two_holds_on_one_slug_the_second_refuses(self):
        first = longop.exclusive(self.server, "wren", "start")
        with self.assertRaises(longop.Busy):
            longop.exclusive(self.server, "wren", "stop")
        first.release()
        longop.exclusive(self.server, "wren", "stop").release()   # released: fine


class Route(LongOpCase):
    def test_get_is_null_before_any_op_then_the_op(self):
        status, body = self.get("/api/cousins/wren/op")
        self.assertEqual((status, body), (200, {"ok": True, "op": None}))
        longop.start(self.server, "wren", "migrate", lambda op: {"ok": True})
        self.wait_done()
        status, body = self.get("/api/cousins/wren/op")
        self.assertEqual(body["op"]["status"], "done")
        self.assertEqual(body["op"]["kind"], "migrate")

    def test_an_unknown_cousin_is_404(self):
        self.assertEqual(self.get("/api/cousins/nobody/op")[0], 404)

    def test_a_routes_start_helper_maps_busy_to_409(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        status, body = longop.start_response(self.server, "wren", "login",
                                             lambda op: gate.wait(5) and {})
        self.assertEqual(status, 202)
        self.assertEqual(body["op"]["status"], "running")
        from cousin_lib.console.app import HttpError
        with self.assertRaises(HttpError) as cm:
            longop.start_response(self.server, "wren", "login", lambda op: {})
        self.assertEqual(cm.exception.status, 409)
        gate.set()


if __name__ == "__main__":
    unittest.main()
