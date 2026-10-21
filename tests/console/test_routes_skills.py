"""The console's skills routes (docs/reference/console-api.md, "Skills"):
inspect, approve, reject and remove, each approval for the diff the page
showed; System never shows a cousin that loads the private repository.
Invented cast only; real git in the temporary root."""
import json
import os
import pathlib
import tempfile

from cousin_lib import skills
from tests.console._harness import ConsoleCase

RUNNER = '\n[agent]\nrunner = "fake"\n'
BODY = "# Steps\n\n1. Read the board.\n"


class SkillsRoutesCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        cfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".gitconfig")
        cfg.close()
        self.addCleanup(os.unlink, cfg.name)
        os.environ.update({"GIT_CONFIG_GLOBAL": cfg.name, "GIT_CONFIG_NOSYSTEM": "1",
                           "GIT_AUTHOR_NAME": "Ana Test", "GIT_AUTHOR_EMAIL": "ana@example.org",
                           "GIT_COMMITTER_NAME": "Ana Test",
                           "GIT_COMMITTER_EMAIL": "ana@example.org"})
        self.wren = self.cousin("wren", operator="Priya", extra=RUNNER)
        self.kit = self.cousin("kit", operator="Priya",
                               extra=RUNNER + '\n[skills]\nrepos = ["private"]\n')

    def draft(self, home, name="board-check", context="personal", scope="own"):
        skills.write_draft(home, name, description="Check a board. Use after a regen.",
                           body=BODY, context=context, side_effect="read-only", scope=scope)

    def approve(self, slug, name="board-check"):
        base = "/api/cousins/%s/skills/drafts/%s" % (slug, name)
        status, d = self.get(base)
        self.assertEqual(status, 200, d)
        return self.post(base + "/approve", {"diff_sha256": d["diff_sha256"]})


class TestApproveAndInspect(SkillsRoutesCase):
    def test_a_draft_is_shown_with_its_diff_and_approved_for_that_diff(self):
        self.draft(self.wren)
        self.serve()
        status, overview = self.get("/api/skills")
        self.assertEqual(status, 200, overview)
        self.assertEqual([(d["cousin"], d["name"], d["repo"], d["folder"]) for d in overview["drafts"]],
                         [("wren", "board-check", "personal", "wren")])
        status, d = self.get("/api/cousins/wren/skills/drafts/board-check")
        self.assertIn("+1. Read the board.", d["diff"])
        self.assertIsNone(d["trigger"])
        status, body = self.post("/api/cousins/wren/skills/drafts/board-check/approve",
                                 {"diff_sha256": "0" * 64})
        self.assertEqual(status, 409, body)
        self.assertIn("changed since it was shown", body["error"])
        status, body = self.approve("wren")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["record"]["user"], body["record"]["action"]), ("_open", "approve"))
        self.assertIn("next start of wren", body["note"])
        status, mine = self.get("/api/cousins/wren/skills")
        self.assertEqual([(s["plugin"], s["name"]) for s in mine["loaded"]],
                         [("personal", "board-check")])
        self.assertEqual(mine["drafts"], [])
        status, shown = self.get("/api/cousins/wren/skills/approved/personal/board-check")
        self.assertIn("1. Read the board.", shown["text"])
        status, overview = self.get("/api/skills")
        personal = next(r for r in overview["repos"] if r["repo"] == "personal")
        self.assertTrue(personal["trusted"])
        self.assertEqual([(f["folder"], [s["name"] for s in f["skills"]]) for f in personal["folders"]],
                         [("wren", ["board-check"])])

    def test_a_shared_skill_is_shown_and_removed_from_system(self):
        self.draft(self.wren, name="commit-gate", scope="shared")
        self.serve()
        self.assertEqual(self.approve("wren", "commit-gate")[0], 200)
        status, shown = self.get("/api/skills/shared/commit-gate")
        self.assertEqual(status, 200, shown)
        status, removal = self.get("/api/skills/shared/commit-gate/removal")
        self.assertIn("-1. Read the board.", removal["diff"])
        status, body = self.post("/api/skills/shared/commit-gate/remove",
                                 {"diff_sha256": removal["diff_sha256"]})
        self.assertEqual(status, 200, body)
        self.assertIn("every cousin", body["note"])
        self.assertEqual(self.get("/api/skills/shared/commit-gate")[0], 404)

    def test_reject_moves_the_draft_aside(self):
        self.draft(self.wren)
        self.serve()
        status, body = self.post("/api/cousins/wren/skills/drafts/board-check/reject",
                                 {"reason": "too vague"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.get("/api/cousins/wren/skills")[1]["drafts"], [])
        self.assertEqual(self.get("/api/cousins/wren/skills/drafts/board-check")[0], 404)

    def test_the_trigger_test_needs_both_prompts_before_it_spends_a_turn(self):
        self.draft(self.wren)
        self.serve()
        status, body = self.post("/api/cousins/wren/skills/drafts/board-check/trigger-test")
        self.assertEqual(status, 409, body)
        self.assertIn("should-trigger or must-not-trigger", body["error"])

    def test_bad_names_and_unknown_cousins(self):
        self.serve()
        self.assertEqual(self.get("/api/cousins/wren/skills/drafts/Bad_Name")[0], 400)
        self.assertEqual(self.get("/api/cousins/nobody/skills")[0], 404)
        self.assertEqual(self.get("/api/cousins/wren/skills/approved/private/x")[0], 404)


class TestPrivacy(SkillsRoutesCase):
    def test_system_never_shows_a_private_cousin_but_its_own_routes_do(self):
        self.draft(self.kit, context="private")
        self.serve()
        status, overview = self.get("/api/skills")
        self.assertNotIn("kit", json.dumps(overview))
        status, mine = self.get("/api/cousins/kit/skills")
        self.assertEqual([d["name"] for d in mine["drafts"]], ["board-check"])
        self.assertEqual(self.approve("kit")[0], 200)
        status, overview = self.get("/api/skills")
        self.assertNotIn("kit", json.dumps(overview))
        status, mine = self.get("/api/cousins/kit/skills")
        self.assertEqual([(s["plugin"], s["name"]) for s in mine["loaded"]],
                         [("private", "board-check")])
        private_records = self.root / "data" / "skills" / "private" / "approvals.jsonl"
        self.assertIn('"folder": "kit"', private_records.read_text())

    def test_a_private_cousins_draft_cannot_land_in_personal(self):
        self.draft(self.kit, context="personal")
        self.serve()
        status, body = self.approve("kit")
        self.assertEqual(status, 409, body)
        self.assertIn("does not load", body["error"])


class TestLoggedInApprover(SkillsRoutesCase):
    def test_the_record_names_the_console_user(self):
        from cousin_lib.console import auth
        users = auth.Users(self.root / "config" / "console-users.json")
        users.set_password("ana", "correct horse")
        self.draft(self.wren)
        self.serve()
        self.assertEqual(self.get("/api/skills")[0], 401)
        self.post("/api/auth/login", {"user": "ana", "password": "correct horse"})
        status, body = self.approve("wren")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["record"]["user"], "ana")
