"""The memory explorer over HTTP: every route sits behind the console
login, reads stay inside the home, `.secrets/` is invisible, and
removing a memory is a trash move with a restore."""
import json
import unittest

from cousin_lib.console import auth
from tests.console._harness import ConsoleCase


def _raw(home, name, rows):
    path = home / "memory" / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


class ExplorerCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.home = self.cousin("wren")
        _raw(self.home, "2026-09-01.jsonl", [
            {"topic": "kestrel", "content": "operator said so",
             "truth_level": "L0_OPERATOR",
             "timestamp": "2026-09-01T10:00:00+00:00"},
            {"topic": "stale", "content": "forget me",
             "truth_level": "L5_OBSOLETE",
             "timestamp": "2026-09-01T11:00:00+00:00"},
        ])
        (self.home / "notes").mkdir()
        (self.home / "notes" / "long.md").write_text(
            "# Long\n\n" + "".join("para %d\n\n" % i for i in range(3000))
            + "THE END\n")
        (self.home / ".secrets").mkdir()
        (self.home / ".secrets" / "key").write_text("s3cret")
        (self.home / "data" / "decisions.jsonl").write_text(json.dumps(
            {"timestamp": "2026-09-01T09:00:00+00:00", "topic": "wire",
             "decision": "red", "reasoning": "live"}) + "\n")


class Reads(ExplorerCase):
    def test_overview_lists_the_layers(self):
        self.serve()
        status, body = self.get("/api/memory/wren/overview")
        self.assertEqual(status, 200)
        layers = {l["id"]: l for l in body["layers"]}
        self.assertEqual(layers["raw"]["count"], 2)
        self.assertEqual(layers["notes"]["count"], 1)
        self.assertEqual(body["insights"]["obsolete"], 1)

    def test_raw_filters_by_level(self):
        self.serve()
        status, body = self.get("/api/memory/wren/raw?level=L5_OBSOLETE")
        self.assertEqual(status, 200)
        self.assertEqual([e["topic"] for e in body["entries"]], ["stale"])
        status, body = self.get("/api/memory/wren/raw?tier=bogus")
        self.assertEqual(status, 400)

    def test_a_long_markdown_file_comes_back_whole(self):
        self.serve()
        status, body = self.get("/api/memory/wren/file?path=notes/long.md")
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "markdown")
        self.assertTrue(body["text"].endswith("THE END\n"))
        self.assertGreater(len(body["text"]), 20000)

    def test_decisions_and_layer_files(self):
        self.serve()
        _, body = self.get("/api/memory/wren/decisions")
        self.assertEqual(body["entries"][0]["decision"], "red")
        _, body = self.get("/api/memory/wren/files?layer=notes")
        self.assertEqual([f["path"] for f in body["files"]],
                         ["notes/long.md"])
        status, _ = self.get("/api/memory/wren/files?layer=bogus")
        self.assertEqual(status, 400)

    def test_secrets_and_traversal_are_refused(self):
        self.serve()
        for path in (".secrets/key", "notes/../.secrets/key",
                     "../../config/console-users.json", "/etc/passwd"):
            status, body = self.get("/api/memory/wren/file?path=" + path)
            self.assertIn(status, (400, 403, 404), path)
            self.assertNotIn("s3cret", json.dumps(body))

    def test_unknown_cousin_is_404(self):
        self.serve()
        self.assertEqual(self.get("/api/memory/nobody/overview")[0], 404)


class RemoveAndRestore(ExplorerCase):
    def test_round_trip_of_a_raw_entry(self):
        self.serve()
        _, body = self.get("/api/memory/wren/raw?level=L5_OBSOLETE")
        ref = body["entries"][0]["ref"]
        status, body = self.post("/api/memory/wren/delete",
                                 {"kind": "entry", **ref})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertTrue(body["effects"]["distilled"])
        trash_id = body["trash"]["id"]
        _, body = self.get("/api/memory/wren/raw")
        self.assertEqual([e["topic"] for e in body["entries"]], ["kestrel"])
        _, body = self.get("/api/memory/wren/trash")
        self.assertEqual(body["batches"][0]["id"], trash_id)
        status, body = self.post("/api/memory/wren/restore",
                                 {"id": trash_id})
        self.assertEqual(status, 200, body)
        _, body = self.get("/api/memory/wren/raw")
        self.assertEqual(sorted(e["topic"] for e in body["entries"]),
                         ["kestrel", "stale"])

    def test_a_decision_goes_with_its_raw_mirror_when_asked(self):
        _raw(self.home, "2026-09-02.jsonl", [
            {"topic": "wire", "content": "red - why: live",
             "source": "decision", "truth_level": "cousin-conclusion",
             "timestamp": "2026-09-02T00:00:00+00:00"}])
        self.serve()
        _, body = self.get("/api/memory/wren/decisions")
        dec = body["entries"][0]
        self.assertEqual(len(dec["mirrors"]), 1)
        status, body = self.post("/api/memory/wren/delete", {
            "kind": "decision", **dec["ref"], "mirrors": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["trash"]["items"]), 2)
        self.assertEqual((self.home / "data" / "decisions.jsonl")
                         .read_text(), "")
        self.assertEqual((self.home / "memory" / "raw" / "2026-09-02.jsonl")
                         .read_text(), "")

    def test_a_file_round_trip(self):
        self.serve()
        status, body = self.post("/api/memory/wren/delete",
                                 {"kind": "file", "path": "notes/long.md"})
        self.assertEqual(status, 200, body)
        self.assertFalse((self.home / "notes" / "long.md").exists())
        status, _ = self.post("/api/memory/wren/restore",
                              {"id": body["trash"]["id"]})
        self.assertEqual(status, 200)
        self.assertTrue((self.home / "notes" / "long.md").exists())

    def test_refusals(self):
        (self.home / "legacy").mkdir()
        (self.home / "legacy" / "old.tar.gz").write_bytes(b"x")
        self.serve()
        for payload in ({"kind": "file", "path": ".secrets/key"},
                        {"kind": "file", "path": "cousin.toml"},
                        {"kind": "file", "path": "legacy/old.tar.gz"},
                        {"kind": "entry", "path": "memory/raw/2026-09-01.jsonl",
                         "line_no": 1, "sha": "000000000000"},
                        {"kind": "entry", "path": "cousin.toml", "line_no": 1},
                        {"kind": "bogus"}):
            status, body = self.post("/api/memory/wren/delete", payload)
            self.assertIn(status, (400, 403, 404, 409), payload)
            self.assertFalse(body.get("ok"), payload)
        self.assertTrue((self.home / ".secrets" / "key").exists())
        self.assertTrue((self.home / "legacy" / "old.tar.gz").exists())
        status, _ = self.post("/api/memory/wren/delete", {
            "kind": "file", "path": "legacy/old.tar.gz", "legacy": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.post("/api/memory/wren/restore",
                                   {"id": "nope"})[0], 404)


class BehindTheLogin(ExplorerCase):
    def test_every_new_route_is_401_without_a_session(self):
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.serve()
        for path in ("/api/memory/wren/overview", "/api/memory/wren/raw",
                     "/api/memory/wren/decisions",
                     "/api/memory/wren/files?layer=notes",
                     "/api/memory/wren/file?path=notes/long.md",
                     "/api/memory/wren/trash"):
            self.assertEqual(self.get(path)[0], 401, path)
        for path, payload in (("/api/memory/wren/delete",
                               {"kind": "file", "path": "notes/long.md"}),
                              ("/api/memory/wren/restore", {"id": "x"})):
            self.assertEqual(self.post(path, payload)[0], 401, path)
        self.assertTrue((self.home / "notes" / "long.md").exists())


if __name__ == "__main__":
    unittest.main()
