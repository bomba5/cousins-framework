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
PROMPTS = {"should-trigger": "check the regenerated board", "must-not-trigger": "what is for dinner?"}


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

    def draft(self, home, name="board-check", context="personal", scope="own", prompts=True):
        skills.write_draft(home, name, description="Check a board. Use after a regen.",
                           body=BODY, context=context, side_effect="read-only", scope=scope,
                           metadata=PROMPTS if prompts else None)

    def hand_draft(self, home, name="board-check", context="personal"):
        """A SKILL.md the cousin wrote with its own file tools."""
        folder = home / "skill-drafts" / name
        folder.mkdir(parents=True, exist_ok=True)
        front = {"name": name, "description": "Check a board.",
                 "metadata": dict(PROMPTS, context=context, **{"side-effect": "read-only"})}
        (folder / "SKILL.md").write_text(skills.render(front, BODY))

    def run_trigger(self, slug, name="board-check"):
        """The trigger test, the model stood in for: the route's child
        process would call the real one."""
        expect = "draft:%s" % name
        return skills.trigger_test(self.root, self.root / "cousins" / slug, slug, name,
                                   checker=lambda prompt, dirs: (
                                       expect if "board" in prompt else None, "ok"))

    def approve(self, slug, name="board-check", trigger=True):
        if trigger:
            self.run_trigger(slug, name)
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
        status, body = self.approve("wren", trigger=False)
        self.assertEqual(status, 409, body)
        self.assertIn("no trigger test on this version", body["error"])
        self.run_trigger("wren")
        status, d = self.get("/api/cousins/wren/skills/drafts/board-check")
        self.assertEqual((d["trigger"]["pass"], d["trigger"]["diff_sha256"]),
                         (True, d["diff_sha256"]))
        status, body = self.approve("wren")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["record"]["user"], body["record"]["action"],
                          body["record"]["trigger"]), ("_open", "approve", "pass"))
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
        self.draft(self.wren, prompts=False)
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
        self.hand_draft(self.kit, context="personal")
        self.serve()
        status, body = self.approve("kit")
        self.assertEqual(status, 409, body)
        self.assertIn("does not load", body["error"])

    def test_nor_when_it_also_loads_personal_for_shared(self):
        self.cousin("lux", operator="Priya",
                    extra=RUNNER + '\n[skills]\nrepos = ["personal", "private"]\n')
        self.hand_draft(self.root / "cousins" / "lux", context="personal")
        self.serve()
        status, mine = self.get("/api/cousins/lux/skills")
        self.assertIn("keeps its own skills there", " ".join(mine["drafts"][0]["problems"]))
        status, body = self.approve("lux")
        self.assertEqual(status, 409, body)
        self.assertIn("keeps its own skills there", body["error"])
        self.assertFalse(skills.repo_dir(self.root, "personal").exists())


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


class TestFromOutside(SkillsRoutesCase):
    """A draft imported from outside: the review shows where it came from
    and its licence, fetches the source again, takes the operator's allow
    for a dry run, and approves only with the dry run recorded. The source
    is an https URL; the fetch reads a local repository instead."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from tests.test_skills import make_upstream
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.upstream, self.commit = make_upstream(tmp.name)
        local, real = "file://%s" % self.upstream, skills.fetch_folder
        patcher = mock.patch.object(skills, "fetch_folder", lambda _url, commit, folder, **_kw:
                                    real(local, commit, folder, schemes=("file",)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = "https://example.test/wren-lab/skills@%s:skills/board-check" % self.commit
        skills.import_draft(self.wren, "wren", self.source, context="personal",
                            side_effect="read-only",
                            metadata=dict(PROMPTS, **{"dry-run-task": "run scripts/check.py"}))

    def test_review_allow_a_dry_run_then_approve(self):
        self.serve()
        base = "/api/cousins/wren/skills/drafts/board-check"
        status, d = self.get(base)
        self.assertEqual(status, 200, d)
        o = d["outside"]
        self.assertEqual((o["license"], o["folder"], o["task"], o["commit"]),
                         ("MIT", "skills/board-check", "run scripts/check.py", self.commit))
        self.assertIn("Permission is hereby granted", o["license_text"])
        self.assertEqual([s["path"] for s in o["scripts"]], ["scripts/check.py"])
        self.assertIsNone(o["dry_run"])
        status, up = self.get(base + "/upstream")
        self.assertEqual(status, 200, up)
        self.assertEqual(up["differ"], [])
        self.assertEqual(up["not_kept"], ["allowed-tools", "license", "metadata"])
        self.assertEqual(up["upstream"]["allowed-tools"], "Bash(git:*) Read")
        self.run_trigger("wren")
        d = self.get(base)[1]
        status, body = self.post(base + "/approve", {"diff_sha256": d["diff_sha256"]})
        self.assertEqual(status, 409, body)
        self.assertIn("no dry run on this version", body["error"])
        status, body = self.post(base + "/dry-run/allow", {"diff_sha256": "0" * 64})
        self.assertEqual(status, 409, body)
        status, body = self.post(base + "/dry-run/allow", {"diff_sha256": d["diff_sha256"]})
        self.assertEqual(status, 200, body)
        self.assertIn("told", body)
        allowed = body["dry_run"]
        self.assertTrue(allowed["allowed_by"])
        self.assertEqual(self.get(base)[1]["outside"]["dry_run"]["allowed_at"],
                         allowed["allowed_at"])
        skills.record_dry_run(self.root, self.wren, "wren", "board-check", 9, job={
            "spawned_by": "wren", "status": "done", "exit_code": 0,
            "started_at": allowed["allowed_at"]})
        d = self.get(base)[1]
        self.assertEqual((d["outside"]["dry_run"]["job"], d["outside"]["dry_run"]["pass"]),
                         (9, True))
        status, body = self.post(base + "/approve", {"diff_sha256": d["diff_sha256"]})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["record"]["source"], body["record"]["dry_run"]),
                         (self.source, "pass"))

    def test_a_source_that_cannot_be_fetched_says_why(self):
        from unittest import mock

        def gone(*_a, **_kw):
            raise skills.SkillError("fetching https://example.test/wren-lab/skills: not found")
        self.serve()
        with mock.patch.object(skills, "fetch_folder", gone):
            status, body = self.get("/api/cousins/wren/skills/drafts/board-check/upstream")
        self.assertEqual(status, 409, body)
        self.assertIn("not found", body["error"])

    def test_a_draft_written_here_has_no_outside_part_and_no_dry_run(self):
        self.draft(self.wren, name="local-check")
        self.serve()
        base = "/api/cousins/wren/skills/drafts/local-check"
        d = self.get(base)[1]
        self.assertIsNone(d["outside"])
        status, body = self.post(base + "/dry-run/allow", {"diff_sha256": d["diff_sha256"]})
        self.assertEqual(status, 409, body)
        self.assertIn("only a skill from outside", body["error"])
