"""Tracker routes over the phase 4 store: list open-first, create,
update, delete, with the store's errors mapped to 400 and 404 and a
tracker-change event per mutation."""
import unittest

from tests.console._harness import ConsoleCase


class TestTracker(ConsoleCase):
    def test_crud_and_events(self):
        server = self.serve()
        seen = []
        server.listeners.append(lambda k, d: seen.append((k, d)))
        status, body = self.post("/api/tracker", {
            "title": "migrate", "domain": "infra", "tags": ["q4"],
            "owner": "wren", "notes": "n"})
        self.assertEqual(status, 200, body)
        item = body["item"]
        self.assertEqual((item["title"], item["state"], item["tags"],
                          item["owner"]), ("migrate", "open", ["q4"], "wren"))
        self.assertEqual(self.post("/api/tracker", {"domain": "x"})[0], 400)
        self.assertEqual(self.post("/api/tracker",
                                   {"title": "t", "state": "odd"})[0], 400)
        self.post("/api/tracker", {"title": "second"})
        status, body = self.post("/api/tracker/%d" % item["id"],
                                 {"state": "done"})
        self.assertEqual(body["item"]["state"], "done")
        self.assertEqual(self.post("/api/tracker/%d" % item["id"], {})[0],
                         400)
        self.assertEqual(self.post("/api/tracker/999", {"title": "x"})[0],
                         404)
        _, body = self.get("/api/tracker")
        self.assertEqual([i["title"] for i in body["items"]],
                         ["second", "migrate"])
        _, body = self.get("/api/tracker?state=done")
        self.assertEqual([i["title"] for i in body["items"]], ["migrate"])
        _, body = self.get("/api/tracker?owner=wren&domain=infra")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(self.get("/api/tracker?state=odd")[0], 400)
        status, body = self.delete("/api/tracker/%d" % item["id"])
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.delete("/api/tracker/%d" % item["id"])[0], 404)
        self.assertEqual(self.get("/api/tracker/x")[0], 404)
        ops = [d["op"] for k, d in seen if k == "tracker-change"]
        self.assertEqual(ops, ["add", "add", "update", "delete"])


if __name__ == "__main__":
    unittest.main()
