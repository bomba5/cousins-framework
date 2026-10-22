"""Skills after approval (docs/reference/skills.md, "Life after approval";
cousin_lib/skills_check.py): baselines, the staleness check per kind of
item, upstream updates, use and the unused flag, the scheduled run, and
what the CLI, the doctor and the console show. Invented cast only; real
git in a temporary directory, a local upstream behind the fetch seam."""
import json
import os
import pathlib
import subprocess
import tempfile
import time
from unittest import mock

from cousin_lib import doctor, loops, memory, recording, skills, skills_check
from cousin_lib.skills import SkillError
from tests.test_skills import BODY, PROMPTS, SkillsCase


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class CheckCase(SkillsCase):
    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(self.root).parent
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        os.environ["PATH"] = "%s:%s" % (self.bin, os.environ.get("PATH", ""))

    def install(self, folder, name, metadata, repo="personal"):
        """An approved skill, written straight into the repository with its
        approve record: what an approval leaves, without its trigger test."""
        path = skills.ensure_repo(self.root, repo, user="ana")
        dest = path / folder / name
        dest.mkdir(parents=True, exist_ok=True)
        front = {"name": name, "description": "Check a board. Use after a regen.",
                 "metadata": dict(PROMPTS, context=repo, **{"side-effect": "read-only"},
                                  **metadata)}
        (dest / "SKILL.md").write_text(skills.render(front, BODY))
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "feat(skills): approve %s/%s" % (folder, name))
        skills.record(self.root, repo, user="ana", action="approve", commit=_git(path, "rev-parse",
                                                                                 "HEAD"),
                      skill=name, folder=folder, diff_sha="0" * 64, trigger="pass")

    def tool(self, name, version):
        """A tool on PATH whose --version says `version`."""
        script = self.bin / name
        script.write_text("#!/bin/sh\necho '%s'\n" % version)
        script.chmod(0o755)
        return script

    def states(self, result):
        return {r["item"]: r["state"] for r in result["items"]}


class TestClassify(CheckCase):
    def test_each_kind_of_item(self):
        pin = "a" * 40
        self.tool("boardhash", "boardhash 1.0")
        cases = [
            ("reads", "tools/hash.py#sha256:%s" % ("b" * 64), "file-pin"),
            ("reads", "wren-lab/widget@%s:tools/hash.py" % pin, "repo-pin"),
            ("reads", "nothing pinned here", "unknown"),
            ("rests-on", "kicad-cli --version", "tool-version"),
            ("rests-on", "reference_board-rules", "shared-ref"),
            ("rests-on", "wren-lab/widget:tools/hash.py", "repo-file"),
            ("rests-on", "scripts/check.py", "file"),
            ("rests-on", "notes.md", "file"),
            ("rests-on", "boardhash", "tool"),
            ("rests-on", "rule: board revisions", "memory"),
            ("source", "https://example.org/skills@%s:skills/board-check" % pin, "upstream"),
            ("source", "not a source", "unknown"),
        ]
        for of, item, kind in cases:
            with self.subTest(item=item):
                self.assertEqual(skills_check.classify(of, item)[0], kind)


class TestLocalItems(CheckCase):
    def setUp(self):
        super().setUp()
        (self.wren / "scripts").mkdir()
        self.script = self.wren / "scripts" / "check.py"
        self.script.write_text("print('v1')\n")
        self.tool("boardhash", "boardhash 1.0")
        (self.root / "shared").mkdir()
        (self.root / "shared" / "reference_board-rules.md").write_text("Rule one.\n")
        memory._append_raw(self.wren, {"topic": "rule: board revisions", "content": "rev A",
                                       "truth_level": "L0_OPERATOR"})
        digest = __import__("hashlib").sha256(self.script.read_bytes()).hexdigest()
        self.install("wren", "board-check", {
            "reads": "scripts/check.py#sha256:%s" % digest,
            "rests-on": "boardhash --version, reference_board-rules, scripts/check.py,"
                        " rule: board revisions, a vague thing nobody can check"})

    def check(self):
        return skills_check.check_skill(self.root, "personal", "wren", "board-check")

    def test_first_check_is_all_same_and_the_vague_item_is_unknown(self):
        result = self.check()
        self.assertFalse(result["stale"])
        states = self.states(result)
        self.assertEqual(states.pop("a vague thing nobody can check"), "unknown")
        self.assertEqual(set(states.values()), {"same"})

    def test_a_changed_file_makes_it_stale_and_says_which(self):
        self.check()
        self.script.write_text("print('v2')\n")
        result = self.check()
        self.assertTrue(result["stale"])
        states = self.states(result)
        self.assertEqual(states["scripts/check.py"], "changed")
        self.assertEqual([k for k, v in states.items() if v == "changed"],
                         [k for k in states if "check.py" in k])

    def test_a_new_tool_version_a_new_rule_and_a_changed_reference_each_flag_it(self):
        self.check()
        self.tool("boardhash", "boardhash 2.0")
        self.assertEqual(self.states(self.check())["boardhash --version"], "changed")
        memory._append_raw(self.wren, {"topic": "rule: board revisions", "content": "rev B",
                                       "truth_level": "L0_OPERATOR"})
        self.assertEqual(self.states(self.check())["rule: board revisions"], "changed")
        (self.root / "shared" / "reference_board-rules.md").write_text("Rule two.\n")
        self.assertEqual(self.states(self.check())["reference_board-rules"], "changed")

    def test_a_deleted_file_is_missing_and_stale(self):
        self.check()
        self.script.unlink()
        result = self.check()
        self.assertTrue(result["stale"])
        self.assertEqual(self.states(result)["scripts/check.py"], "missing")

    def test_something_never_found_is_unknown_never_stale(self):
        self.install("wren", "ghost", {"rests-on": "scripts/never-written.py"})
        result = skills_check.check_skill(self.root, "personal", "wren", "ghost")
        self.assertEqual(self.states(result), {"scripts/never-written.py": "unknown"})
        self.assertFalse(result["stale"])

    def test_the_baseline_is_taken_at_approval(self):
        self.draft(self.wren, name="fresh")
        self.approve(self.wren, "wren", "fresh")
        base = json.loads(skills_check.baseline_path(self.root, "personal", "wren", "fresh")
                          .read_text())
        self.assertEqual(base["from"], "approval")
        self.assertEqual(base["commit"], skills.state(self.root, "personal")["approved"])

    def test_another_skills_approval_keeps_this_ones_staleness(self):
        """Review M1: the baseline is the skill's own, so an approval of
        another skill in the same repository never wipes a change."""
        self.check()
        self.script.write_text("print('v2')\n")
        self.assertTrue(self.check()["stale"])
        self.install("wren", "other-skill", {})
        result = self.check()
        self.assertTrue(result["stale"])
        self.assertEqual(self.states(result)["scripts/check.py"], "changed")

    def test_an_edit_of_the_skill_itself_takes_a_new_baseline(self):
        self.check()
        self.script.write_text("print('v2')\n")
        self.assertTrue(self.check()["stale"])
        self.install("wren", "board-check", {"rests-on": "scripts/check.py"})
        result = self.check()
        self.assertFalse(result["stale"])
        self.assertEqual(result["baseline"]["from"], "skill changed")

    def test_a_bare_tool_stays_a_tool_where_path_does_not_find_it(self):
        """Review S1: a word the baseline read as a tool is not re-read as a
        memory topic by a process whose PATH lacks it."""
        self.install("wren", "tooly", {"rests-on": "boardhash"})
        first = skills_check.check_skill(self.root, "personal", "wren", "tooly")
        self.assertEqual(first["items"][0]["kind"], "tool")
        os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]   # without self.bin
        row = skills_check.check_skill(self.root, "personal", "wren", "tooly")["items"][0]
        self.assertEqual((row["kind"], row["state"]), ("tool", "same"))

    def test_a_tool_is_checked_at_the_path_its_baseline_found(self):
        self.check()
        other = self.tmp / "elsewhere"
        other.mkdir()
        (other / "boardhash").write_text("#!/bin/sh\necho 'boardhash 9.9'\n")
        (other / "boardhash").chmod(0o755)
        os.environ["PATH"] = "%s:%s" % (other, os.environ["PATH"])
        # another PATH finds another binary first; the check reads the same one
        self.assertEqual(self.states(self.check())["boardhash --version"], "same")


def make_lib(base):
    """A library repository with one tool file, two commits: the second
    changes tools/hash.py and keeps tools/keep.py. Returns (path, first, second)."""
    repo = pathlib.Path(base) / "lib"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "hash.py").write_text("v1\n")
    (repo / "tools" / "keep.py").write_text("same\n")
    _git(base, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "tools" / "hash.py").write_text("v2\n")
    _git(repo, "commit", "-q", "-am", "two")
    return repo, first, _git(repo, "rev-parse", "HEAD")


def local_fetch(urls):
    """The fetch seam: an https URL in `urls` is served from its local
    repository over file://, the rest refused like an unreachable host."""
    def fetch(remote, url, what):
        if url not in urls:
            raise SkillError("unreachable: %s" % url)
        repo = remote._repo()
        subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", repo, "fetch", "-q",
                        "--depth", "1", "--no-tags", "file://%s" % urls[url], what],
                       check=True, capture_output=True)
        return _git(repo, "rev-parse", "FETCH_HEAD^{commit}")
    return fetch


class TestRepositories(CheckCase):
    def setUp(self):
        super().setUp()
        self.lib, self.first, self.second = make_lib(self.tmp)

    def test_a_local_pin_is_stale_when_its_file_changed_after_the_pin(self):
        self.install("wren", "board-check", {
            "reads": "%s@%s:tools/hash.py, %s@%s:tools/keep.py" % (self.lib, self.first,
                                                                  self.lib, self.first)})
        result = skills_check.check_skill(self.root, "personal", "wren", "board-check")
        states = self.states(result)
        self.assertEqual(states["%s@%s:tools/hash.py" % (self.lib, self.first)], "changed")
        self.assertEqual(states["%s@%s:tools/keep.py" % (self.lib, self.first)], "same")
        self.assertTrue(result["stale"])

    def test_a_remote_pin_is_fetched_and_an_unreachable_one_is_unknown(self):
        url = "https://example.org/wren-lab/lib"
        self.install("wren", "board-check", {
            "reads": "example.org/wren-lab/lib@%s:tools/keep.py, gone.example/x/y@%s:a.py"
                     % (self.first, self.first)})
        with skills_check.Remote(fetch=local_fetch({url: self.lib})) as remote:
            result = skills_check.check_skill(self.root, "personal", "wren", "board-check",
                                              remote=remote)
        states = self.states(result)
        self.assertEqual(states["example.org/wren-lab/lib@%s:tools/keep.py" % self.first], "same")
        self.assertEqual(states["gone.example/x/y@%s:a.py" % self.first], "unknown")
        self.assertFalse(result["stale"])

    def test_upstream_moving_on_is_an_update_not_staleness(self):
        url = "https://example.org/wren-lab/lib"
        self.install("wren", "board-check", {"source": "%s@%s:tools" % (url, self.first)})
        with skills_check.Remote(fetch=local_fetch({url: self.lib})) as remote:
            result = skills_check.check_skill(self.root, "personal", "wren", "board-check",
                                              remote=remote)
        self.assertTrue(result["update"])
        self.assertFalse(result["stale"])

    def test_only_https_is_fetched(self):
        with skills_check.Remote() as remote:
            with self.assertRaisesRegex(SkillError, "only https"):
                remote._fetch("ssh://example.org/x", "HEAD")

    def test_the_user_agent_names_the_framework(self):
        self.assertIn("cousins-framework/", skills_check.user_agent())
        self.assertIn("skills check", skills_check.user_agent())


class TestUse(CheckCase):
    def test_the_hook_records_a_skill_use_in_the_cousins_home(self):
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Skill",
                   "tool_input": {"skill": "personal:board-check"}, "tool_response": "loaded"}
        recording.handle(payload, self.wren, self.root, slug="wren")
        recording.handle(payload, self.wren, self.root, slug="wren")
        row = skills_check.usage(self.wren)["personal:board-check"]
        self.assertEqual((row["count"], row["ok"]), (2, True))
        self.assertEqual(skills_check.usage(self.otto), {})

    def test_last_use_spans_the_homes_given_and_the_unused_flag_counts_from_approval(self):
        now = time.time()
        old = now - 40 * 86400
        skills_check.record_use(self.wren, "shared:commit-gate", now=old)
        skills_check.record_use(self.otto, "shared:commit-gate", now=now - 3600)
        last, count = skills_check.last_use("personal", "shared", "commit-gate",
                                            [self.wren, self.otto])
        self.assertEqual(count, 2)
        self.assertEqual(skills_check._when(last), int(now - 3600))
        approved = skills_check._now_iso(old)
        self.assertFalse(skills_check.unused(approved, last, now=now))
        self.assertTrue(skills_check.unused(approved, None, now=now))
        self.assertFalse(skills_check.unused(skills_check._now_iso(now - 86400), None, now=now))

    def test_use_counts_from_the_skills_own_cousin_or_every_cousin_for_shared(self):
        """Review N1: two cousins' own skills of one name never share a use."""
        both = self.home("ines", repos=["personal", "private"])
        self.home("kit", repos=["private"])
        self.assertEqual(skills_check.usage_homes(self.root, "wren"), [self.wren])
        self.assertEqual(skills_check.usage_homes(self.root, "shared"), [self.otto, self.wren])
        self.assertEqual(skills_check.usage_homes(self.root, "shared", include_private=True),
                         [both, self.otto, self.wren])

    def test_annotate_adds_check_use_and_the_flag(self):
        self.install("wren", "board-check", {})
        skills_check.check_skill(self.root, "personal", "wren", "board-check")
        skills_check.record_use(self.wren, "personal:board-check")
        item = skills.loaded(self.root, self.wren, "wren")[0]
        seen = skills_check.annotate(self.root, item, homes=[self.wren])
        self.assertEqual(seen["usage"]["count"], 1)
        self.assertFalse(seen["check"]["stale"])
        self.assertFalse(seen["unused"])


class TestRuns(CheckCase):
    def test_check_all_checks_every_repository_and_keeps_counts_only(self):
        self.install("wren", "board-check", {"rests-on": "scripts/x.py"})
        (self.wren / "scripts").mkdir()
        (self.wren / "scripts" / "x.py").write_text("1\n")
        self.install("otto", "ghost", {}, repo="private")
        summary = skills_check.check_all(self.root)
        self.assertEqual((summary["checked"], summary["stale"], summary["errors"]), (2, 0, 0))
        self.assertTrue(skills_check.check_path(self.root, "private", "otto", "ghost").is_file())
        self.assertIn("/private/", str(skills_check.check_path(self.root, "private", "otto",
                                                                "ghost")))
        self.assertNotIn("otto", json.dumps(skills_check.last_run(self.root)))

    def test_a_run_is_due_after_check_every_and_an_asked_run_respects_the_gap(self):
        now = time.time()
        self.assertTrue(skills_check.due(self.root, now=now))
        skills_check.check_all(self.root, now=now)
        self.assertFalse(skills_check.due(self.root, now=now + 60))
        self.assertTrue(skills_check.due(self.root, now=now + skills_check.CHECK_EVERY))
        with self.assertRaisesRegex(SkillError, "the last check ran"):
            skills_check.check_all(self.root, now=now + 10, gap=skills_check.NOW_GAP)

    def test_runs_never_overlap(self):
        """Review N2: one run at a time; the loops do not start a second."""
        self.assertFalse(skills_check.running(self.root))
        with skills_check._run_lock(self.root):
            self.assertTrue(skills_check.running(self.root))
            with self.assertRaisesRegex(SkillError, "already in progress"):
                skills_check.check_all(self.root)
            ran = []
            loops.schedule_skills_check(time.time(), {"errors": []}, root=self.root,
                                        run=lambda: ran.append(1), background=False)
            self.assertEqual(ran, [])
        self.assertFalse(skills_check.running(self.root))

    def test_the_loops_tick_starts_one_when_due(self):
        ran = []
        report = {"errors": []}
        loops.schedule_skills_check(time.time(), report, root=self.root,
                                    run=lambda: ran.append(1) or {"checked": 0},
                                    background=False)
        self.assertEqual(ran, [1])
        self.assertEqual(report["skills_checked"], [{"checked": 0}])

    def test_the_module_run_prints_the_summary(self):
        out = subprocess.run([os.sys.executable, "-m", "cousin_lib.skills_check", "--root",
                              str(self.root), "run"], capture_output=True, text=True,
                             cwd=str(self.tmp), env=dict(os.environ, PYTHONPATH=str(
                                 pathlib.Path(skills.__file__).resolve().parents[1])))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["checked"], 0)


class TestSurfaces(CheckCase):
    def setUp(self):
        super().setUp()
        (self.wren / "scripts").mkdir()
        self.script = self.wren / "scripts" / "x.py"
        self.script.write_text("1\n")
        self.install("wren", "board-check", {"rests-on": "scripts/x.py"})
        skills_check.check_skill(self.root, "personal", "wren", "board-check")
        self.script.write_text("2\n")
        skills_check.check_skill(self.root, "personal", "wren", "board-check")

    def test_the_list_flags_a_stale_skill(self):
        text = skills.text_list(self.root, self.wren, "wren")
        self.assertIn("personal:board-check", text)
        self.assertIn("[stale: scripts/x.py]", text)

    def test_cousin_skill_check_names_what_changed(self):
        with mock.patch("sys.stdout") as out:
            os.environ["COUSIN_HOME"] = str(self.wren)
            os.environ["FRAMEWORK_ROOT"] = str(self.root)
            self.assertEqual(skills.main(["check", "board-check"]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("personal:board-check - stale", printed)
        self.assertIn("rests-on scripts/x.py: changed", printed)

    def test_the_doctor_warns_about_a_stale_skill_and_counts_a_private_one(self):
        self.install("otto", "ghost", {"rests-on": "notes/n.md"}, repo="private")
        otto_notes = self.otto / "notes"
        otto_notes.mkdir()
        (otto_notes / "n.md").write_text("1\n")
        skills_check.check_skill(self.root, "private", "otto", "ghost")
        (otto_notes / "n.md").write_text("2\n")
        skills_check.check_skill(self.root, "private", "otto", "ghost")
        result = doctor.check_skills(self.root)
        self.assertFalse(result["ok"])
        text = "\n".join(result["notes"])
        self.assertIn("personal:wren/board-check: stale: scripts/x.py", text)
        self.assertIn("1 stale skill in the private repository", text)
        self.assertNotIn("otto", text)
