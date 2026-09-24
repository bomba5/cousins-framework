"""The shared-tier review: listings with hashes, content strictly
inside each scope, diffs, the audit tail, and promote/reject with the
reviewer taken from the session or, unconfigured, from the body."""
import hashlib
import json
import unittest

from cousin_lib import shared_tier
from cousin_lib.console import auth
from tests.console._harness import ConsoleCase


class SharedCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.cousin("wren")
        self.cousin("toki")
        (self.root / "shared").mkdir()
        (self.root / "shared" / "project_x.md").write_text("old\n")
        shared_tier.propose("project_x.md", "new\n", slug="wren",
                            reason="better")
        (self.root / "config" / "shared-reviewers.json").write_text(
            json.dumps({"reviewers": ["ana", "toki"]}))


class TestReads(SharedCase):
    def test_list_content_diff_audit(self):
        self.serve()
        _, body = self.get("/api/shared/list")
        self.assertEqual(body["canonical"][0]["name"], "project_x.md")
        self.assertEqual(body["canonical"][0]["size"], 4)
        self.assertEqual(body["canonical"][0]["sha"],
                         hashlib.sha256(b"old\n").hexdigest()[:12])
        pending = body["pending"][0]
        self.assertEqual((pending["name"], pending["slug"], pending["origin"]),
                         ("wren__project_x.md", "wren", "project_x.md"))
        _, body = self.get("/api/shared/content?scope=canonical"
                           "&name=project_x.md")
        self.assertEqual(body["content"], "old\n")
        _, body = self.get("/api/shared/content?scope=pending"
                           "&name=wren__project_x.md")
        self.assertEqual(body["content"], "new\n")
        self.assertEqual(self.get("/api/shared/content?scope=pending")[0],
                         400)
        self.assertEqual(self.get("/api/shared/content?scope=canonical"
                                  "&name=../cousins/wren/CLAUDE.md")[0], 404)
        self.assertEqual(self.get("/api/shared/content?scope=canonical"
                                  "&name=proposed/wren__project_x.md")[0],
                         404)
        _, body = self.get("/api/shared/diff?file=project_x.md&slug=wren")
        self.assertIn("-old", body["diff"])
        self.assertIn("+new", body["diff"])
        self.assertEqual(self.get("/api/shared/diff?file=project_x.md")[0],
                         400)
        self.assertEqual(self.get("/api/shared/diff?file=project_x.md"
                                  "&slug=toki")[0], 404)
        _, body = self.get("/api/shared/audit?n=5")
        self.assertEqual(body["entries"][0]["kind"], "propose")
        self.assertEqual(body["entries"][0]["actor"], "wren")
        self.assertEqual(body["entries"][0]["reason"], "better")


class TestReview(SharedCase):
    def test_unconfigured_needs_by_and_maps_refusals(self):
        self.serve()
        status, body = self.post("/api/shared/approve",
                                 {"slug": "wren", "file": "project_x.md"})
        self.assertEqual(status, 400)
        status, body = self.post("/api/shared/approve",
                                 {"slug": "wren", "file": "project_x.md",
                                  "by": "wren"})
        self.assertEqual(status, 403)
        status, body = self.post("/api/shared/approve",
                                 {"slug": "wren", "file": "project_x.md",
                                  "by": "stranger"})
        self.assertEqual(status, 403)
        status, body = self.post("/api/shared/approve",
                                 {"slug": "toki", "file": "project_x.md",
                                  "by": "ana"})
        self.assertEqual(status, 404)
        status, body = self.post("/api/shared/approve",
                                 {"slug": "wren", "file": "project_x.md",
                                  "by": "ana"})
        self.assertEqual(body, {"ok": True, "file": "project_x.md"})
        self.assertEqual((self.root / "shared" / "project_x.md").read_text(),
                         "new\n")
        _, body = self.get("/api/shared/audit")
        self.assertEqual(body["entries"][0]["kind"], "promote")
        self.assertEqual(body["entries"][0]["actor"], "ana")

    def test_configured_uses_the_session_user_and_reject_records_reason(self):
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.serve()
        self.post("/api/auth/login", {"user": "ana",
                                      "password": "correct horse"})
        status, body = self.post("/api/shared/reject",
                                 {"slug": "wren", "file": "project_x.md",
                                  "reason": "stale", "by": "wren"})
        self.assertEqual(body, {"ok": True, "file": "project_x.md"})
        _, body = self.get("/api/shared/audit")
        self.assertEqual(body["entries"][0]["kind"], "reject")
        self.assertEqual(body["entries"][0]["actor"], "ana")
        self.assertEqual(body["entries"][0]["reason"], "stale")
        self.assertEqual(self.get("/api/shared/list")[1]["pending"], [])



class TestReviewers(SharedCase):
    """The reviewer list (config/shared-reviewers.json): read by anyone
    the console admits, written only by a logged-in user, audited."""

    def login(self):
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.serve()
        self.post("/api/auth/login", {"user": "ana",
                                      "password": "correct horse"})

    def test_read_says_whether_you_review(self):
        self.login()
        status, body = self.get("/api/shared/reviewers")
        self.assertEqual(status, 200)
        self.assertEqual(body["reviewers"], ["ana", "toki"])
        self.assertTrue(body["configured"])
        self.assertTrue(body["you_review"])

    def test_write_replaces_the_list_keeps_other_keys_and_audits(self):
        path = self.root / "config" / "shared-reviewers.json"
        path.write_text(json.dumps({"reviewers": ["ana"], "note": "kept"}))
        self.login()
        status, body = self.post("/api/shared/reviewers",
                                 {"reviewers": [" ana ", "Toki", "ANA"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["reviewers"], ["ana", "Toki"])
        self.assertEqual(json.loads(path.read_text()),
                         {"reviewers": ["ana", "Toki"], "note": "kept"})
        _, body = self.get("/api/shared/audit")
        self.assertEqual(body["entries"][0]["kind"], "reviewers")
        self.assertEqual(body["entries"][0]["actor"], "ana")
        self.assertEqual(body["entries"][0]["reviewers"], ["ana", "Toki"])

    def test_write_refusals(self):
        self.serve()
        self.assertEqual(self.post("/api/shared/reviewers",
                                   {"reviewers": ["ana"]})[0], 403)
        self.login()
        for payload in ({}, {"reviewers": "ana"}, {"reviewers": [3]},
                        {"reviewers": [""]}, {"reviewers": ["a\nb"]},
                        {"reviewers": ["x" * 65]}):
            self.assertEqual(self.post("/api/shared/reviewers", payload)[0],
                             400, payload)
        self.assertEqual(json.loads((self.root / "config"
                                     / "shared-reviewers.json").read_text()),
                         {"reviewers": ["ana", "toki"]})

    def test_an_unreadable_file_is_said_not_overwritten(self):
        path = self.root / "config" / "shared-reviewers.json"
        path.write_text("{not json")
        self.login()
        _, body = self.get("/api/shared/reviewers")
        self.assertFalse(body["configured"])
        self.assertIn("not valid JSON", body["error"])
        status, _ = self.post("/api/shared/reviewers", {"reviewers": ["ana"]})
        self.assertEqual(status, 409)
        self.assertEqual(path.read_text(), "{not json")


if __name__ == "__main__":
    unittest.main()
