"""`GET /api/events`: a poller that diffs the stores other components own
and a broker that fans events out to every open stream
(docs/reference/console-api.md, "`GET /api/events`: the live stream"). Framing,
the snapshot-first rule, the ping comment, clean close on disconnect."""
import json
import os
import pathlib
import queue
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from cousin_lib import jobs, loops, tracker
from cousin_lib.console import router, sse


def _frames(chunks):
    out = []
    for raw in b"".join(chunks).split(b"\n\n"):
        if not raw:
            continue
        text = raw.decode()
        if text.startswith(":"):
            out.append((None, text))
        else:
            self_data = [l[6:] for l in text.split("\n")
                         if l.startswith("data: ")]
            out.append(tuple(json.loads(self_data[0]).get(k)
                             for k in ("kind", "data")))
    return out


class EventsCase(unittest.TestCase):
    def setUp(self):
        router.clear()
        sse.register()
        sse.reset()
        self.addCleanup(sse.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "cousins" / "testa").mkdir(parents=True)
        (self.root / "cousins" / "testa" / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\n[chat]\nport = 1\n')
        patcher = mock.patch.dict("os.environ", {
            "FRAMEWORK_ROOT": str(self.root), "COUSIN_HOME": str(self.root / "cousins" / "testa")})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestFraming(unittest.TestCase):
    def test_data_frame_carries_kind_and_data(self):
        raw = sse.frame("job-add", {"id": 1})
        self.assertEqual(raw, b'data: {"kind": "job-add", "data": {"id": 1}}\n\n')

    def test_named_event_frame(self):
        raw = sse.event_frame("geom", {"cols": 1})
        self.assertEqual(raw, b'event: geom\ndata: {"cols": 1}\n\n')

    def test_ping_is_a_comment_frame(self):
        self.assertEqual(sse.PING, b": ping\n\n")


class TestPoller(EventsCase):
    def _poller(self, **kw):
        kw.setdefault("root", self.root)
        return sse.Poller(**kw)

    def test_first_poll_is_a_baseline_with_refresh_events_only(self):
        p = self._poller()
        jobs.register_job(kind="shell", title="pre-existing")
        kinds = [k for k, _ in p.poll(now=0.0)]
        self.assertIn("cousins-refresh", kinds)
        self.assertIn("loops-refresh", kinds)
        self.assertNotIn("job-add", kinds)

    def test_job_rows_diff_into_add_update_delete(self):
        p = self._poller()
        p.poll(now=0.0)
        jid = jobs.register_job(kind="shell", title="t")
        events = p.poll(now=2.0)
        self.assertEqual([(k, d["id"]) for k, d in events
                          if k.startswith("job-")], [("job-add", jid)])
        jobs.finish_job(jid, status="done", summary="fine")
        events = p.poll(now=4.0)
        upd = [d for k, d in events if k == "job-update"]
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0]["status"], "done")
        self.assertEqual(upd[0]["result_summary"], "fine")
        import sqlite3
        con = sqlite3.connect(self.root / "data" / "jobs.db")
        con.execute("DELETE FROM jobs WHERE id=?", (jid,))
        con.commit()
        con.close()
        events = p.poll(now=6.0)
        self.assertIn(("job-delete", {"id": jid}), events)

    def test_jobs_poll_every_two_seconds_fleet_every_fifteen(self):
        calls = {"jobs": 0, "cousins": 0, "loops": 0}

        def counting(name, value):
            def source(*a, **k):
                calls[name] += 1
                return value
            return source

        p = self._poller(sources={"jobs": counting("jobs", []),
                                  "cousins": counting("cousins", []),
                                  "loops": counting("loops", [])})
        p.poll(now=0.0)
        p.poll(now=1.0)
        self.assertEqual((calls["jobs"], calls["cousins"]), (1, 1))
        p.poll(now=2.0)
        self.assertEqual((calls["jobs"], calls["cousins"]), (2, 1))
        p.poll(now=14.0)
        self.assertEqual(calls["cousins"], 1)
        p.poll(now=15.0)
        self.assertEqual((calls["cousins"], calls["loops"]), (2, 2))

    def test_loop_fire_when_last_fires_advances(self):
        p = self._poller()
        state_path = self.root / "data" / "loops-state.json"
        state_path.parent.mkdir(exist_ok=True)
        state_path.write_text(json.dumps({
            "last_tick": 1.0, "last_beat": {},
            "last_fires": {"testa|daily": 100.0}}))
        p.poll(now=0.0)
        state_path.write_text(json.dumps({
            "last_tick": 2.0, "last_beat": {},
            "last_fires": {"testa|daily": 200.0, "testa|weekly": 150.0}}))
        events = [(k, d) for k, d in p.poll(now=15.0) if k == "loop-fire"]
        self.assertEqual(sorted(d["loop"] for _, d in events),
                         ["daily", "weekly"])
        self.assertEqual([d for _, d in events if d["loop"] == "daily"][0],
                         {"cousin": "testa", "loop": "daily", "ts": 200.0})
        self.assertEqual([k for k, _ in p.poll(now=30.0) if k == "loop-fire"],
                         [])

    def test_tracker_items_diff_into_tracker_change(self):
        p = self._poller()
        p.poll(now=0.0)
        item = tracker.add("build it", root=self.root)
        events = p.poll(now=2.0)
        self.assertIn(("tracker-change", {"id": item["id"], "op": "add"}),
                      events)
        time.sleep(1.1)  # updated_at has second resolution
        tracker.update(item["id"], state="active", root=self.root)
        events = p.poll(now=4.0)
        self.assertIn(("tracker-change", {"id": item["id"], "op": "update"}),
                      events)
        tracker.delete(item["id"], root=self.root)
        events = p.poll(now=6.0)
        self.assertIn(("tracker-change", {"id": item["id"], "op": "delete"}),
                      events)

    def test_consumed_timed_flips_become_cousin_flip_events(self):
        p = self._poller()
        p.poll(now=0.0)
        rid = loops.submit_request("flip", cousin="testa",
                                   payload={"fire_at": 500.0})
        events = p.poll(now=2.0)
        flips = [d for k, d in events if k == "cousin-flip"]
        self.assertEqual(flips, [{"slug": "testa", "phase": "scheduled",
                                  "fire_at": 500.0, "request_id": rid}])
        import sqlite3
        con = sqlite3.connect(self.root / "data" / "loop-requests.db")
        con.execute("UPDATE requests SET status='done' WHERE id=?", (rid,))
        con.commit()
        flips = [d for k, d in p.poll(now=4.0) if k == "cousin-flip"]
        self.assertEqual(flips[0]["phase"], "complete")
        rid2 = loops.submit_request("flip", cousin="testa", payload={})
        p.poll(now=6.0)
        con.execute("UPDATE requests SET status='expired',"
                    " reason='daemon missed ticks' WHERE id=?", (rid2,))
        con.commit()
        con.close()
        flips = [d for k, d in p.poll(now=8.0) if k == "cousin-flip"]
        self.assertEqual((flips[0]["phase"], flips[0]["error"]),
                         ("failed", "daemon missed ticks"))

    def test_a_failing_source_never_kills_the_poll(self):
        def boom(*a, **k):
            raise RuntimeError("store away")
        p = self._poller(sources={"jobs": boom})
        import io
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            events = p.poll(now=0.0)
        self.assertIn("cousins-refresh", [k for k, _ in events])
        self.assertIn("store away", err.getvalue())

    def test_poller_thread_starts_and_stops(self):
        p = self._poller(fast=0.01, slow=0.01)
        p.start()
        time.sleep(0.05)
        p.stop()
        self.assertFalse(p.running)


class TestBroker(EventsCase):
    def test_emit_reaches_every_subscriber(self):
        a, b = sse.subscribe(), sse.subscribe()
        sse.emit("cousin-status", {"slug": "testa", "status": "starting"})
        for q in (a, b):
            self.assertEqual(json.loads(q.get(timeout=1)),
                             {"kind": "cousin-status",
                              "data": {"slug": "testa", "status": "starting"}})
        sse.unsubscribe(a)
        sse.emit("x", 1)
        self.assertTrue(a.empty())
        self.assertEqual(json.loads(b.get(timeout=1))["kind"], "x")

    def test_a_full_subscriber_is_dropped_not_blocked(self):
        q = sse.subscribe(maxsize=1)
        sse.emit("x", 1)
        sse.emit("x", 2)  # would block a put(); must not
        self.assertEqual(sse.subscriber_count(), 0)


class TestStream(EventsCase):
    def test_snapshot_first_then_events_then_ping(self):
        sse.configure(cousins=lambda: [{"slug": "testa"}],
                      loops=lambda: [{"cousin": "testa", "name": "n"}])
        stream = sse.events_stream(ping_after=0.05, root=self.root)
        it = iter(stream)
        kind, data = _frames([next(it)])[0]
        self.assertEqual(kind, "snapshot")
        self.assertEqual(data["cousins"], [{"slug": "testa"}])
        self.assertEqual(data["loops"], [{"cousin": "testa", "name": "n"}])
        self.assertEqual(data["jobs"], [])
        self.assertIn("daemon", data)
        self.assertFalse(data["daemon"]["ok"])
        sse.emit("job-add", {"id": 7})
        self.assertEqual(_frames([next(it)])[0], ("job-add", {"id": 7}))
        self.assertEqual(next(it), sse.PING)
        stream.close()

    def test_close_unsubscribes(self):
        stream = sse.events_stream(ping_after=0.05, root=self.root)
        it = iter(stream)
        next(it)
        self.assertEqual(sse.subscriber_count(), 1)
        stream.close()
        self.assertEqual(sse.subscriber_count(), 0)
        with self.assertRaises(StopIteration):
            next(it)

    def test_write_failure_mid_stream_unsubscribes(self):
        # app.py stops iterating on a broken pipe and calls close(); the
        # generator's finally must release the queue either way.
        stream = sse.events_stream(ping_after=0.05, root=self.root)
        it = iter(stream)
        next(it)
        del it
        stream.close()
        self.assertEqual(sse.subscriber_count(), 0)

    def test_route_returns_a_stream_with_sse_headers(self):
        req = SimpleNamespace(root=self.root, query={}, body={})
        status, body = router.dispatch("GET", "/api/events", req=req)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, sse.Stream)
        self.assertEqual(body.content_type, "text/event-stream")
        headers = {k.lower(): v for k, v in body.headers}
        self.assertEqual(headers["cache-control"], "no-cache")
        body.close()


if __name__ == "__main__":
    unittest.main()


class TestRegistryRows(EventsCase):
    """The bare registry rows the snapshot carries before the fleet
    routes plug in must name the same model and effort the fleet rows
    do, or a tab that connects before the wiring shows a blank."""

    def test_rows_carry_model_and_effort_with_the_install_fallback(self):
        rows = {r["slug"]: r for r in sse._registry_rows(self.root)}
        self.assertIsNone(rows["testa"]["model"])
        self.assertIsNone(rows["testa"]["effort"])
        (self.root / "config").mkdir()
        (self.root / "config" / "harness.toml").write_text(
            '[agent]\ndefault_model = "dm"\ndefault_effort = "high"\n')
        (self.root / "cousins" / "testa" / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\n[chat]\nport = 1\n'
            '[runtime]\neffort = "low"\n')
        rows = {r["slug"]: r for r in sse._registry_rows(self.root)}
        self.assertEqual((rows["testa"]["model"], rows["testa"]["effort"]),
                         ("dm", "low"))
