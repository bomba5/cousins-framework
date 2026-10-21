"""Skills (docs/reference/skills.md): the SKILL.md format, drafts, the
git repositories with their approval records, the per-start export the
runner loads, and the gate's refusal for writes to them. Invented cast
only; real git in a temporary directory."""
import json
import os
import pathlib
import stat
import subprocess
import tempfile
from unittest import mock

from cousin_lib import skills
from cousin_lib.skills import SkillError
from tests._hermetic import HermeticCase

BODY = "# Steps\n\n1. Read the board.\n2. Check it.\n"
PROMPTS = {"should-trigger": "check the regenerated board", "must-not-trigger": "what is for dinner?"}


class SkillsCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "install"
        (self.root / "config").mkdir(parents=True)
        empty = pathlib.Path(tmp.name) / "gitconfig"
        empty.write_text("")
        os.environ.update({"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
                           "GIT_AUTHOR_NAME": "Ana Test", "GIT_AUTHOR_EMAIL": "ana@example.org",
                           "GIT_COMMITTER_NAME": "Ana Test",
                           "GIT_COMMITTER_EMAIL": "ana@example.org"})
        self.wren = self.home("wren")
        self.otto = self.home("otto")

    def home(self, slug, repos=None):
        home = self.root / "cousins" / slug
        home.mkdir(parents=True)
        text = '[cousin]\nslug = "%s"\n' % slug
        if repos is not None:
            text += "\n[skills]\nrepos = %s\n" % json.dumps(repos)
        (home / "cousin.toml").write_text(text)
        return home

    def draft(self, home, name="board-check", context="personal", scope="own",
              side_effect="read-only", body=BODY, prompts=True):
        return skills.write_draft(home, name, description="Check a board. Use after a regen.",
                                  body=body, context=context, side_effect=side_effect,
                                  scope=scope, metadata=PROMPTS if prompts else None)

    def hand_draft(self, home, name="board-check", context="personal", scope="own"):
        """A SKILL.md the cousin wrote with its own file tools, which
        write_draft's checks never saw."""
        folder = home / "skill-drafts" / name
        folder.mkdir(parents=True, exist_ok=True)
        front = {"name": name, "description": "Check a board.",
                 "metadata": dict(PROMPTS, context=context, scope=scope,
                                  **{"side-effect": "read-only"})}
        (folder / "SKILL.md").write_text(skills.render(front, BODY))

    def run_trigger(self, home, slug, name="board-check", fires=True):
        """The trigger test with a stand-in for the model: the draft fires
        on a prompt about a board, when `fires`."""
        expect = "draft:%s" % name
        return skills.trigger_test(
            self.root, home, slug, name,
            checker=lambda prompt, dirs: ((expect if fires and "board" in prompt else None), "ok"))

    def approve(self, home, slug, name="board-check"):
        self.run_trigger(home, slug, name)
        _text, sha = skills.diff_draft(self.root, home, slug, name)
        return skills.approve_draft(self.root, home, slug, name, user="ana", diff_sha=sha)


class TestFormat(SkillsCase):
    def test_render_then_parse_round_trips_with_string_metadata(self):
        front = {"name": "a-b", "description": "Does x. Use when y.",
                 "metadata": {"context": "personal", "side-effect": "read-only",
                              "hosts": "a.example, b.example", "reads": "x@1:p: q"}}
        parsed, body = skills.parse(skills.render(front, BODY))
        self.assertEqual(parsed, front)
        self.assertEqual(body.strip(), BODY.strip())

    def test_a_missing_or_broken_frontmatter_is_an_error(self):
        for text in ("no frontmatter", "---\nname: x\n", "---\njust words\n---\n",
                     "---\n  indented: x\n---\n"):
            with self.assertRaises(SkillError, msg=text):
                skills.parse(text)

    def test_problems_name_the_format_rules(self):
        good = {"name": "board-check", "description": "x",
                "metadata": {"context": "personal", "side-effect": "read-only"}}
        self.assertEqual(skills.problems("board-check", good), [])
        for name in ("Board", "-a", "a-", "a--b", "a" * 65):
            self.assertTrue(skills.problems(name, dict(good, name=name)), name)
        self.assertTrue(skills.problems("board-check", dict(good, name="other")))
        self.assertTrue(skills.problems("board-check", dict(good, description="")))
        self.assertTrue(skills.problems("board-check", dict(good, description="x" * 1025)))
        for meta in ({"context": "employer", "side-effect": "read-only"},
                     {"context": "personal", "side-effect": "writes"},
                     {"context": "work", "side-effect": "read-only", "scope": "shared"},
                     {"context": "personal", "side-effect": "read-only", "scope": "fleet"}):
            self.assertTrue(skills.problems("board-check", dict(good, metadata=meta)), meta)


class TestDraftsAndApproval(SkillsCase):
    def test_a_draft_is_a_file_that_loads_nothing_until_approved(self):
        self.draft(self.wren)
        found = skills.drafts(self.wren, "wren")
        self.assertEqual([(d["name"], d["repo"], d["folder"], d["problems"]) for d in found],
                         [("board-check", "personal", "wren", [])])
        self.assertEqual(skills.loaded(self.root, self.wren, "wren"), [])
        self.assertFalse(skills.repo_dir(self.root, "personal").exists())

    def test_approving_commits_on_main_records_it_and_drops_the_draft(self):
        self.draft(self.wren)
        entry = self.approve(self.wren, "wren")
        st = skills.state(self.root, "personal")
        self.assertTrue(st["trusted"])
        self.assertEqual(entry["commit"], st["head"])
        self.assertEqual((entry["action"], entry["user"], entry["folder"], entry["skill"],
                          entry["trigger"]),
                         ("approve", "ana", "wren", "board-check", "pass"))
        self.assertEqual([r["action"] for r in skills.records(self.root, "personal")],
                         ["init", "approve"])
        self.assertFalse((self.wren / "skill-drafts" / "board-check").exists())
        loaded = skills.loaded(self.root, self.wren, "wren")
        self.assertEqual([(i["plugin"], i["name"]) for i in loaded], [("personal", "board-check")])
        # another cousin never loads it
        self.assertEqual(skills.loaded(self.root, self.otto, "otto"), [])
        log = subprocess.run(["git", "-C", str(skills.repo_dir(self.root, "personal")), "log",
                              "--format=%an|%s", "-1"], capture_output=True, text=True).stdout
        self.assertEqual(log.strip(), "Ana Test|feat(skills): approve wren/board-check")

    def test_only_the_diff_that_was_shown_is_approved(self):
        self.draft(self.wren)
        _text, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        self.draft(self.wren, body=BODY + "3. A step added after the look.\n")
        with self.assertRaisesRegex(SkillError, "changed since it was shown"):
            skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                 diff_sha=sha)

    def test_a_shared_skill_goes_to_personal_shared_and_every_cousin_loads_it(self):
        self.draft(self.wren, name="commit-gate", scope="shared", side_effect="outward")
        self.approve(self.wren, "wren", "commit-gate")
        for home, slug in ((self.wren, "wren"), (self.otto, "otto")):
            self.assertEqual([(i["plugin"], i["name"]) for i in skills.loaded(self.root, home, slug)],
                             [("shared", "commit-gate")])

    def test_a_repository_moved_past_its_approval_loads_nothing(self):
        self.draft(self.wren)
        self.approve(self.wren, "wren")
        path = skills.repo_dir(self.root, "personal")
        (path / "wren" / "sneaky").mkdir()
        (path / "wren" / "sneaky" / "SKILL.md").write_text("---\nname: sneaky\n---\n")
        subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "x"], check=True)
        self.assertFalse(skills.state(self.root, "personal")["trusted"])
        self.assertEqual(skills.loaded(self.root, self.wren, "wren"), [])
        paths, report = skills.export_plugins(self.root, self.wren, "wren")
        self.assertEqual(paths, [])
        self.assertEqual([s["what"] for s in report["skipped"]], ["personal"])
        self.draft(self.wren, name="later")
        _t, sha = skills.diff_draft(self.root, self.wren, "wren", "later")
        with self.assertRaisesRegex(SkillError, "moved past its last approval"):
            skills.approve_draft(self.root, self.wren, "wren", "later", user="ana", diff_sha=sha)

    def test_a_draft_never_lands_in_a_repository_the_cousin_does_not_load(self):
        home = self.home("kit", repos=["private"])
        with self.assertRaisesRegex(SkillError, "does not load"):
            self.draft(home, context="personal")
        self.hand_draft(home, context="personal")
        self.assertIn("does not load", " ".join(skills.drafts(home, "kit")[0]["problems"]))
        self.run_trigger(home, "kit")
        _t, sha = skills.diff_draft(self.root, home, "kit", "board-check")
        with self.assertRaisesRegex(SkillError, "does not load"):
            skills.approve_draft(self.root, home, "kit", "board-check", user="ana", diff_sha=sha)
        self.assertFalse(skills.repo_dir(self.root, "personal").exists())

    def test_a_cousin_that_loads_private_keeps_its_own_skills_there(self):
        # it lists personal too, for shared/: its own folder, its name,
        # still never lands in personal (law rule 11)
        home = self.home("kit", repos=["personal", "private"])
        with self.assertRaisesRegex(SkillError, "keeps its own skills there"):
            self.draft(home, context="personal")
        self.hand_draft(home, context="personal")
        self.assertIn("keeps its own skills there",
                      " ".join(skills.drafts(home, "kit")[0]["problems"]))
        self.run_trigger(home, "kit")
        _t, sha = skills.diff_draft(self.root, home, "kit", "board-check")
        with self.assertRaisesRegex(SkillError, "keeps its own skills there"):
            skills.approve_draft(self.root, home, "kit", "board-check", user="ana", diff_sha=sha)
        self.assertFalse(skills.repo_dir(self.root, "personal").exists())
        # its own skill goes to private, a general one to shared/
        self.draft(home, context="private")
        self.approve(home, "kit")
        self.draft(home, name="commit-gate", scope="shared")
        entry = self.approve(home, "kit", "commit-gate")
        self.assertEqual(entry["folder"], "shared")
        log = subprocess.run(["git", "-C", str(skills.repo_dir(self.root, "personal")), "log",
                              "--format=%s", "--name-only"], capture_output=True, text=True).stdout
        self.assertIn("approve shared/commit-gate", log)
        self.assertNotIn("kit", log)
        self.assertNotIn("kit", (self.root / "data" / "skills" / "approvals.jsonl").read_text())
        self.assertFalse((self.root / "data" / "skills" / "trigger-tests").exists())
        self.assertEqual([(i["plugin"], i["name"]) for i in skills.loaded(self.root, home, "kit")],
                         [("shared", "commit-gate"), ("private", "board-check")])

    def test_a_cousin_named_shared_has_no_folder_of_its_own(self):
        home = self.home("shared")
        self.draft(home)
        self.assertIn("no folder of its own", " ".join(skills.drafts(home, "shared")[0]["problems"]))
        with self.assertRaisesRegex(SkillError, "no folder of its own"):
            skills.approve_draft(self.root, home, "shared", "board-check", user="ana",
                                 diff_sha="0" * 64)

    def test_approval_needs_a_trigger_test_on_the_diff_shown_and_records_a_miss(self):
        self.draft(self.wren)
        _t, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        with self.assertRaisesRegex(SkillError, "no trigger test on this version"):
            skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                 diff_sha=sha)
        # a result on an earlier version does not count
        self.run_trigger(self.wren, "wren")
        self.draft(self.wren, body=BODY + "3. A step added after the test.\n")
        _t, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        with self.assertRaisesRegex(SkillError, "no trigger test on this version"):
            skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                 diff_sha=sha)
        # a failed test is the operator's call: approved, and recorded as a miss
        self.assertFalse(self.run_trigger(self.wren, "wren", fires=False)["pass"])
        entry = skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                     diff_sha=sha)
        self.assertEqual(entry["trigger"], "fail")

    def test_a_draft_without_its_trigger_prompts_lists_why_and_is_not_approved(self):
        self.draft(self.wren, prompts=False)
        found = skills.drafts(self.wren, "wren")[0]["problems"]
        self.assertEqual(found, ["the trigger test needs should-trigger and must-not-trigger"
                                 " in metadata"])
        with self.assertRaisesRegex(SkillError, "cannot be approved: the trigger test needs"):
            skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                 diff_sha="0" * 64)

    def test_reject_moves_the_draft_aside_with_who_and_why(self):
        self.draft(self.wren)
        dest = skills.reject_draft(self.wren, "board-check", user="ana", reason="too vague")
        self.assertFalse((self.wren / "skill-drafts" / "board-check").exists())
        self.assertIn("rejected by ana", (dest / "REJECTED.txt").read_text())
        self.assertIn("too vague", (dest / "REJECTED.txt").read_text())
        self.assertEqual(skills.drafts(self.wren, "wren"), [])

    def test_removing_an_approved_skill_is_shown_then_recorded(self):
        self.draft(self.wren)
        self.approve(self.wren, "wren")
        text, sha = skills.removal_diff(self.root, "personal", "wren", "board-check")
        self.assertIn("-1. Read the board.", text)
        entry = skills.remove_skill(self.root, "personal", "wren", "board-check", user="ana",
                                    diff_sha=sha)
        self.assertEqual(entry["action"], "remove")
        self.assertEqual(skills.loaded(self.root, self.wren, "wren"), [])

    def test_private_records_keep_a_file_of_their_own(self):
        home = self.home("kit", repos=["private"])
        self.draft(home, context="private")
        self.approve(home, "kit")
        shared_file = self.root / "data" / "skills" / "approvals.jsonl"
        private_file = self.root / "data" / "skills" / "private" / "approvals.jsonl"
        self.assertFalse(shared_file.exists() and "kit" in shared_file.read_text())
        self.assertIn('"folder": "kit"', private_file.read_text())
        self.assertEqual(stat.S_IMODE(private_file.stat().st_mode), 0o600)
        # and so does its trigger test result, whose path names it
        self.assertEqual(skills.trigger_path(self.root, home, "kit", "board-check"),
                         self.root / "data" / "skills" / "private" / "trigger-tests" / "kit"
                         / "board-check.json")


class TestExport(SkillsCase):
    def test_the_export_is_read_only_plugins_named_for_the_repository(self):
        self.draft(self.wren)
        self.approve(self.wren, "wren")
        self.draft(self.wren, name="commit-gate", scope="shared", side_effect="outward")
        self.approve(self.wren, "wren", "commit-gate")
        paths, report = skills.export_plugins(self.root, self.wren, "wren")
        self.assertEqual([p.name for p in paths], ["shared", "personal"])
        self.assertEqual(sorted(report["loaded"]), ["personal:board-check", "shared:commit-gate"])
        manifest = json.loads((paths[1] / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "personal")
        skill = paths[1] / "skills" / "board-check" / "SKILL.md"
        self.assertEqual(stat.S_IMODE(skill.stat().st_mode), 0o444)
        with self.assertRaises(PermissionError):
            skill.write_text("tampered")
        # a fresh export at the next start replaces it
        paths2, _ = skills.export_plugins(self.root, self.wren, "wren")
        self.assertEqual([p.name for p in paths2], ["shared", "personal"])

    def test_a_cousin_on_two_repositories_gets_both_and_never_anothers(self):
        home = self.home("ines", repos=["personal", "work"])
        self.draft(home, name="build-proof", context="work")
        self.approve(home, "ines", "build-proof")
        self.draft(self.wren)
        self.approve(self.wren, "wren")
        paths, report = skills.export_plugins(self.root, home, "ines")
        self.assertEqual(report["loaded"], ["work:build-proof"])
        self.assertEqual([p.name for p in paths], ["work"])

    def test_a_bad_cousin_toml_list_loads_nothing_and_says_why(self):
        home = self.home("bad", repos=["employer"])
        paths, report = skills.export_plugins(self.root, home, "bad")
        self.assertEqual(paths, [])
        self.assertIn("unknown employer", report["skipped"][0]["why"])

    def test_the_tool_text_lists_loaded_skills_and_drafts(self):
        self.draft(self.wren)
        self.approve(self.wren, "wren")
        self.draft(self.wren, name="next-one")
        text = skills.text_list(self.root, self.wren, "wren")
        self.assertIn("personal:board-check - Check a board.", text)
        self.assertIn("next-one -> personal:wren", text)
        self.assertEqual(skills.text_list(self.root, self.wren, "wren", lane="opencode"),
                         "none: this lane loads no skills")
        self.assertIn("1. Read the board.", skills.text_show(self.root, self.wren, "wren",
                                                            "personal:board-check"))
        self.assertIn("draft next-one", skills.text_show(self.root, self.wren, "wren", "next-one"))
        with self.assertRaises(SkillError):
            skills.text_show(self.root, self.otto, "otto", "personal:board-check")


class TestGuard(SkillsCase):
    def refused(self, tool, payload):
        return skills.guard_tool(tool, payload, root=self.root, home=self.wren, cwd=self.wren)

    def test_writes_to_the_repositories_exports_and_records_are_refused(self):
        repo = skills.repo_dir(self.root, "personal")
        for tool, payload in (
                ("Write", {"file_path": str(repo / "wren" / "x" / "SKILL.md")}),
                ("Edit", {"file_path": str(self.wren / "run" / "skills" / "personal" / "a")}),
                ("Write", {"file_path": "run/skills/shared/skills/a/SKILL.md"}),
                ("Bash", {"command": "echo hi > %s/wren/x.md" % repo}),
                ("Bash", {"command": "rm -rf %s/data/skills" % self.root}),
                ("Bash", {"command": "git -C %s commit -am x" % repo}),
                ("Bash", {"command": "cd %s && git reset --hard HEAD~1" % repo}),
                ("Bash", {"command": "git -C %s/wren update-ref refs/heads/main HEAD" % repo}),
                ("Bash", {"command": "git --git-dir=%s/.git commit -am x" % repo}),
                ("Bash", {"command": "git --work-tree=%s add -A" % repo}),
                ("Bash", {"command": "GIT_DIR=%s/.git git commit -am x" % repo}),
                ("Bash", {"command": "env GIT_DIR=%s/.git git update-ref refs/heads/main HEAD"
                                     % repo}),
                ("Bash", {"command": "env -C %s git reset --hard" % repo}),
                ("Bash", {"command": "export GIT_DIR=%s/.git; git commit -am x" % repo}),
                ("Bash", {"command": "nice -n 5 git -C %s commit -am x" % repo}),
                ("Write", {"file_path": str(skills.trigger_path(self.root, self.wren, "wren",
                                                                "board-check"))})):
            self.assertIsNotNone(self.refused(tool, payload), (tool, payload))

    def test_reads_drafts_and_unrelated_work_pass(self):
        repo = skills.repo_dir(self.root, "personal")
        for tool, payload in (
                ("Bash", {"command": "git -C %s log --oneline" % repo}),
                ("Bash", {"command": "cd %s && git diff HEAD~1" % repo}),
                ("Bash", {"command": "cat %s/wren/x/SKILL.md" % repo}),
                ("Write", {"file_path": str(self.wren / "skill-drafts" / "a" / "SKILL.md")}),
                ("Bash", {"command": "git commit -am 'own repo work'"}),
                ("Bash", {"command": "GIT_DIR=%s/.git git log --oneline" % repo}),
                ("Bash", {"command": "env GIT_PAGER=cat git --git-dir=%s/.git show" % repo}),
                ("Bash", {"command": "GIT_DIR=/tmp/elsewhere/.git git commit -am x"}),
                ("Read", {"file_path": str(repo / "wren" / "x" / "SKILL.md")})):
            self.assertIsNone(self.refused(tool, payload), (tool, payload))


class TestCli(SkillsCase):
    def run_cli(self, *args, stdin=""):
        out, err = [], []
        with mock.patch("sys.stdin", __import__("io").StringIO(stdin)), \
                mock.patch("builtins.print", lambda *a, **k: (err if k.get("file") else out)
                           .append(" ".join(str(x) for x in a))):
            os.environ["FRAMEWORK_ROOT"] = str(self.root)
            code = skills.main(["--home", str(self.wren), *args])
        return code, "\n".join(out), "\n".join(err)

    def test_draft_list_show_diff_from_a_terminal(self):
        code, out, _ = self.run_cli("draft", "board-check", "--description", "Check a board.",
                                    "--context", "personal", "--side-effect", "read-only",
                                    "--should-trigger", "check the regenerated board",
                                    stdin=BODY)
        self.assertEqual(code, 0, out)
        self.assertIn("draft written", out)
        self.assertIn("before approval: the trigger test needs must-not-trigger in metadata", out)
        _, out, _ = self.run_cli("list")
        self.assertIn("board-check -> personal:wren", out)
        _, out, _ = self.run_cli("show", "board-check")
        self.assertIn("should-trigger: check the regenerated board", out)
        _, out, _ = self.run_cli("diff", "board-check")
        self.assertIn("+1. Read the board.", out)
        code, _, err = self.run_cli("diff", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no draft nope", err)


class TestTriggerTest(SkillsCase):
    def test_both_prompts_run_with_the_draft_beside_what_loads(self):
        skills.write_draft(self.wren, "board-check", description="Check a board.", body=BODY,
                           context="personal", side_effect="read-only",
                           metadata={"should-trigger": "check the regenerated board",
                                     "must-not-trigger": "what is for dinner?"})
        seen = []

        def checker(prompt, plugin_dirs):
            seen.append((prompt, sorted(p.name for p in plugin_dirs)))
            return ("draft:board-check" if "board" in prompt else None), "ok"
        result = skills.trigger_test(self.root, self.wren, "wren", "board-check", checker=checker)
        self.assertTrue(result["pass"])
        self.assertEqual([s[1] for s in seen], [["draft"], ["draft"]])
        _t, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        self.assertEqual(skills.last_trigger_test(self.root, self.wren, "wren", "board-check",
                                                  sha)["pass"], True)
        # the result sits under the install's data, which tools may not
        # write, never in the draft, so the draft's diff is unchanged
        self.assertTrue(skills.trigger_path(self.root, self.wren, "wren", "board-check")
                        .is_relative_to(self.root / "data" / "skills" / "trigger-tests"))
        self.assertEqual(skills.diff_draft(self.root, self.wren, "wren", "board-check")[1], sha)
        self.assertEqual([d["name"] for d in skills.drafts(self.wren, "wren")], ["board-check"])
        # an edit makes the old result stale
        skills.write_draft(self.wren, "board-check", description="Check a board, carefully.",
                           body=BODY, context="personal", side_effect="read-only",
                           metadata={"should-trigger": "check the regenerated board",
                                     "must-not-trigger": "what is for dinner?"})
        _t, sha2 = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        self.assertIsNone(skills.last_trigger_test(self.root, self.wren, "wren", "board-check",
                                                   sha2))

    def test_a_skill_that_fires_on_the_wrong_prompt_fails(self):
        skills.write_draft(self.wren, "board-check", description="Check a board.", body=BODY,
                           context="personal", side_effect="read-only",
                           metadata={"should-trigger": "check the board",
                                     "must-not-trigger": "what is for dinner?"})
        result = skills.trigger_test(self.root, self.wren, "wren", "board-check",
                                     checker=lambda prompt, dirs: ("draft:board-check", "ok"))
        self.assertFalse(result["pass"])
        self.assertFalse(result["must-not-trigger"]["pass"])

    def test_a_draft_without_its_prompts_cannot_be_tested(self):
        self.draft(self.wren, prompts=False)
        with self.assertRaisesRegex(SkillError, "should-trigger or must-not-trigger"):
            skills.trigger_test(self.root, self.wren, "wren", "board-check",
                                checker=lambda p, d: (None, "ok"))
