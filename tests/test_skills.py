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

from cousin_lib import memory, skills
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

    def test_quotes_colons_and_non_ascii_round_trip_as_utf8_json_strings(self):
        """Every value but the name is written as a JSON string, which a
        YAML reader takes as the same text: a prompt with a quote or a
        Danish letter reaches the trigger test as written, and a
        description with ': ' or a leading '[' stays one string."""
        prompt = 'hvad skal vi have: æbler "nu" \\ ok'
        front = {"name": "a-b", "description": "[wip] Plan: dinner #1",
                 "metadata": {"context": "personal", "should-trigger": prompt, "hosts": "",
                              "odd": "del\x7fc1\x85\ufffe"}}
        text = skills.render(front, BODY)
        self.assertIn('description: "[wip] Plan: dinner #1"\n', text)
        self.assertIn("æbler", text)
        self.assertNotIn("\\u00e6", text)
        self.assertIn('  hosts: ""\n', text)
        self.assertNotIn("\x7f", text)
        self.assertNotIn("\ufffe", text)              # escaped: YAML refuses it raw
        for line in text.split("---")[1].strip().split("\n"):   # not splitlines: NEL
            key, _, value = line.strip().partition(": ")
            if key not in ("name", "metadata:"):
                json.loads(value)           # each one a JSON (so YAML) string
        parsed, _ = skills.parse(text)
        self.assertEqual(parsed, front)

    def test_hand_written_values_read_as_yaml_would(self):
        text = ("---\nname: a\ndescription: 'it''s plain'\nlicense:\nmetadata:\n"
                '  context: personal\n  reads: "x@abc1234:p"\n  hosts:\n  source: ""\n'
                "---\nsteps\n")
        front, _ = skills.parse(text)
        self.assertEqual(front["description"], "it's plain")
        self.assertNotIn("license", front)                  # an empty plain value is null
        meta = front["metadata"]
        self.assertEqual((meta["context"], meta["reads"]), ("personal", "x@abc1234:p"))
        self.assertNotIn("hosts", meta)                     # blank: not declared
        self.assertEqual(meta["source"], "")                # "": declared none

    def test_an_escape_only_yaml_reads_is_refused_not_kept(self):
        """Kept as written, the approve view and the trigger test would see
        other text than the agent's YAML reader loads."""
        for value in ('"C:\\path"', '"a\\x41b"', '"a" and "b"'):
            text = "---\nname: a\ndescription: %s\n---\nsteps\n" % value
            with self.assertRaisesRegex(SkillError, "takes JSON's escapes only", msg=value):
                skills.parse(text)

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


SHA = "ab" * 32
COMMIT = "0123456789abcdef" * 2 + "01234567"      # a full one


class TestDeclaredKeys(SkillsCase):
    """hosts, reads, rests-on, source and license: what a skill reaches and
    where it came from, set by the draft tool and checked before approval,
    never at load."""

    def write(self, home=None, **declared):
        license = declared.pop("license", None)
        meta = dict(PROMPTS, **declared)
        path = skills.write_draft(home or self.wren, "board-check",
                                  description="Check a board.", body=BODY,
                                  context="personal", side_effect="read-only",
                                  metadata=meta, license=license)
        return skills.parse(path.read_text())[0]

    def test_written_normalized_and_an_empty_value_declares_none(self):
        memory._append_raw(self.wren, {"topic": "board-notes", "content": "rev A",
                                       "truth_level": "L3_COUSIN_CONCLUSION"})
        front = self.write(hosts=" a.example ,b.example,, ",
                           reads="wren-lab/widget@%s:tools/x.py, notes/a.md#sha256:%s" % (COMMIT, SHA),
                           **{"rests-on": "kicad-cli --version,  board-notes"})
        meta = front["metadata"]
        self.assertEqual(meta["hosts"], "a.example, b.example")
        self.assertEqual(meta["reads"],
                         "wren-lab/widget@%s:tools/x.py, notes/a.md#sha256:%s" % (COMMIT, SHA))
        self.assertEqual(meta["rests-on"], "kicad-cli --version, board-notes")
        self.assertNotIn("source", meta)
        self.assertNotIn("license", front)
        meta = self.write(hosts="", source=None)["metadata"]
        self.assertEqual(meta["hosts"], "")             # declared: contacts no host
        self.assertNotIn("source", meta)                # not declared
        self.assertNotIn("reads", meta)
        meta = skills.parse(skills.write_draft(
            self.wren, "board-check", description="Check a board.", body=BODY,
            context="personal", side_effect="read-only",
            metadata={"should-trigger": "", "must-not-trigger": None}).read_text())[0]["metadata"]
        self.assertNotIn("should-trigger", meta)        # an empty prompt is no prompt

    def test_malformed_values_are_refused_when_written(self):
        for declared, word in (({"hosts": "https://a.example/x"}, "metadata.hosts"),
                               ({"hosts": "a b"}, "metadata.hosts"),
                               ({"hosts": "-a.example"}, "metadata.hosts"),
                               ({"reads": "tools/x.py"}, "metadata.reads"),
                               ({"reads": "w@main:tools/x.py"}, "metadata.reads"),
                               ({"reads": "notes/a.md#sha256:abc"}, "metadata.reads"),
                               ({"source": "github.com/o/r@" + COMMIT, "license": "MIT"},
                                "metadata.source"),
                               ({"source": "https://g.example/o/r@v1.0", "license": "MIT"},
                                "metadata.source"),
                               ({"license": "MIT; rm -rf"}, "license takes"),
                               ({"reads": "w@abc1234:tools/x.py"}, "metadata.reads"),
                               ({"source": ":@" + COMMIT, "license": "MIT"}, "metadata.source"),
                               ({"source": "https://@" + COMMIT, "license": "MIT"},
                                "metadata.source"),
                               ({"source": "https://g.example/o/r@abc1234", "license": "MIT"},
                                "metadata.source"),
                               ({"source": "https://g.example/o/r.git@" + COMMIT},
                                "needs a license")):
            with self.assertRaisesRegex(SkillError, word, msg=declared):
                self.write(**declared)
        self.assertFalse((self.wren / "skill-drafts").exists())

    def test_a_source_makes_it_a_skill_from_outside_with_what_that_needs(self):
        """Written by hand rather than imported, it still answers to the
        rules for a skill from outside (TestImport has the import)."""
        front = self.write(source="git@g.example:o/r.git@" + COMMIT, license="MIT OR Apache-2.0")
        self.assertEqual(front["metadata"]["source"], "git@g.example:o/r.git@" + COMMIT)
        self.assertEqual(front["license"], "MIT OR Apache-2.0")
        found = skills.drafts(self.wren, "wren")[0]["problems"]
        for word in ("names its folder", "not MIT OR Apache-2.0", "keeps its licence file",
                     "needs metadata.dry-run-task"):
            self.assertTrue(any(word in f for f in found), (word, found))

    def test_a_hand_edited_draft_lists_them_and_is_not_approved_but_would_load(self):
        folder = self.wren / "skill-drafts" / "board-check"
        folder.mkdir(parents=True)
        front = {"name": "board-check", "description": "Check a board.",
                 "metadata": dict(PROMPTS, context="personal", hosts="https://a.example",
                                  **{"side-effect": "read-only"})}
        (folder / "SKILL.md").write_text(skills.render(front, BODY))
        found = skills.drafts(self.wren, "wren")[0]["problems"]
        self.assertEqual(len(found), 1)
        self.assertIn("metadata.hosts takes host names", found[0])
        self.run_trigger(self.wren, "wren")
        _text, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        with self.assertRaisesRegex(SkillError, "cannot be approved: metadata.hosts"):
            skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                 diff_sha=sha)
        self.assertEqual(skills.problems("board-check", front), [])     # the loader's rules

    def test_the_runner_tool_passes_them_and_tells_absent_from_empty(self):
        from types import SimpleNamespace
        from cousin_lib.runner import tools
        ctx = SimpleNamespace(home=self.wren, slug="wren", root=self.root)
        text = tools._sk_draft(ctx, {"command": "draft", "name": "board-check",
                                     "description": "Check a board.", "context": "personal",
                                     "side_effect": "read-only", "body": BODY,
                                     "should_trigger": PROMPTS["should-trigger"],
                                     "must_not_trigger": PROMPTS["must-not-trigger"],
                                     "hosts": "", "rests_on": "kicad-cli --version",
                                     "license": "MIT"})
        self.assertIn("draft written", text)
        self.assertNotIn("before approval", text)
        front = skills.parse((self.wren / "skill-drafts" / "board-check" / "SKILL.md")
                             .read_text())[0]
        self.assertEqual(front["metadata"]["hosts"], "")
        self.assertEqual(front["metadata"]["rests-on"], "kicad-cli --version")
        self.assertNotIn("reads", front["metadata"])
        self.assertEqual(front["license"], "MIT")


MIT = ("MIT License\n\nCopyright (c) 2026 Wren Lab\n\nPermission is hereby granted, free of"
       " charge, to any person obtaining a copy of this software, to deal in it without"
       " restriction.\n")
PROPRIETARY = "(c) 2026 Wren Lab. All rights reserved. No copies outside our service.\n"
UPSTREAM_SKILL = ("---\nname: board-check\ndescription: >\n  Check a board.\n  Use after a regen.\n"
                  "license: Complete terms in LICENSE.txt\nallowed-tools: Bash(git:*) Read\n"
                  "metadata:\n  author: wren-lab\n---\n\n# Steps\n\n1. Read the board.\n"
                  "2. Run scripts/check.py as a job.\n")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_upstream(base):
    """A published skills repository: a sound skill, one with a symlink,
    one with a submodule, one whose licence is only at the root, and one
    under a proprietary licence. Returns (path, commit)."""
    repo = pathlib.Path(base) / "upstream"
    skills_dir = repo / "skills"
    sound = skills_dir / "board-check"
    (sound / "scripts").mkdir(parents=True)
    (sound / "SKILL.md").write_text(UPSTREAM_SKILL)
    (sound / "LICENSE.txt").write_text(MIT)
    (sound / "scripts" / "check.py").write_text("#!/usr/bin/env python3\nprint('ok')\n")
    os.chmod(sound / "scripts" / "check.py", 0o755)
    (sound / "notes.md").write_text("Plain notes.\n")
    for name in ("linked", "sub", "rootlic", "closed"):
        (skills_dir / name).mkdir()
        (skills_dir / name / "SKILL.md").write_text(UPSTREAM_SKILL.replace("board-check", name))
    os.symlink("SKILL.md", skills_dir / "linked" / "alias.md")
    (skills_dir / "linked" / "LICENSE").write_text(MIT)
    (skills_dir / "closed" / "LICENSE.txt").write_text(PROPRIETARY)
    (repo / "LICENSE").write_text(MIT)
    _git(base, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--add", "--cacheinfo", "160000,%s,skills/sub/mod" % ("1" * 40))
    _git(repo, "commit", "-q", "-m", "skills")
    return repo, _git(repo, "rev-parse", "HEAD")


def _git_in(cwd, data, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], input=data, check=True,
                          capture_output=True).stdout.decode().strip()


def make_hostile(base, parts):
    """A repository no honest host serves: its folder `sk` holds a SKILL.md,
    a licence and a file under the literal path parts `parts` (`..` climbs
    out of the folder, `.git` is git's own), written with hash-object
    --literally since git will not make such a tree itself. Returns (path,
    commit)."""
    repo = pathlib.Path(base) / "hostile.git"
    _git(base, "init", "-q", "--bare", str(repo))

    def tree(entries):
        raw = b"".join(b"%s %s\0%s" % (mode, name.encode(), bytes.fromhex(oid))
                       for mode, name, oid in sorted(entries, key=lambda e: e[1]))
        return _git_in(repo, raw, "hash-object", "-t", "tree", "--literally", "-w", "--stdin")

    def blob(text):
        return _git_in(repo, text.encode(), "hash-object", "-w", "--stdin")

    inner = tree([(b"100644", "escaped.txt", blob("out\n"))])
    for part in reversed(parts[1:]):
        inner = tree([(b"40000", part, inner)])
    sk = tree([(b"100644", "SKILL.md", blob(UPSTREAM_SKILL.replace("board-check", "sk"))),
               (b"100644", "LICENSE", blob(MIT)), (b"40000", parts[0], inner)])
    return repo, _git(repo, "commit-tree", tree([(b"40000", "sk", sk)]), "-m", "hostile")


class TestMemoryTopics(SkillsCase):
    """A draft names only memory topics its staleness check can find
    (docs/reference/skills.md, "What is checked"): the cousin's own, each
    by its exact name, one with a comma in double quotes. Anything else is
    refused when it is written, with the topics it likely means."""

    def setUp(self):
        super().setUp()
        for topic in ("rule: board revisions", "rule: plans, then reports"):
            memory._append_raw(self.wren, {"topic": topic, "content": "x",
                                           "truth_level": "L0_OPERATOR"})

    def write(self, rests_on, scope="own"):
        return skills.write_draft(self.wren, "board-check", description="Check a board.",
                                  body=BODY, context="personal", side_effect="read-only",
                                  scope=scope, metadata=dict(PROMPTS, **{"rests-on": rests_on}))

    def test_a_topic_it_has_is_taken_a_comma_one_in_quotes(self):
        path = self.write('rule: board revisions, "rule: plans, then reports",'
                          ' live:rule: board revisions')
        meta = skills.parse(path.read_text())[0]["metadata"]
        self.assertEqual(skills._items(meta["rests-on"]),
                         ["rule: board revisions", "rule: plans, then reports",
                          "live:rule: board revisions"])

    def test_a_topic_it_lacks_is_refused_with_the_one_it_likely_means(self):
        with self.assertRaises(SkillError) as raised:
            self.write("memory topic: rule: board revisions, rule: plans, then reports")
        text = str(raised.exception)
        self.assertIn('"memory topic: rule: board revisions" (yours: "rule: board revisions")', text)
        self.assertIn('"rule: plans" (yours: "rule: plans, then reports")', text)
        self.assertIn('"then reports"', text)
        self.assertIn("in double quotes when it holds a comma", text)
        self.assertFalse((self.wren / "skill-drafts" / "board-check").exists())

    def test_a_shared_skill_names_no_memory_topic(self):
        with self.assertRaisesRegex(SkillError, "a shared skill has no owner"):
            self.write("rule: board revisions", scope="shared")
        self.write("kicad-cli --version, notes/board.md", scope="shared")

    def test_the_other_forms_are_no_topics(self):
        self.write("kicad-cli --version, reference_board-rules, wren-lab/widget:tools/x.py,"
                   " scripts/check.py, live:notes/plan.md")

    def test_an_empty_live_mark_is_refused(self):
        with self.assertRaisesRegex(SkillError, "nothing after the mark"):
            self.write("live:")

    def test_a_list_reads_back_its_quotes_and_splits_the_rest_as_before(self):
        items = ["a", "b, c", '"d"', 'e "f"']
        self.assertEqual(skills._items(skills._joined(items)), items)
        self.assertEqual(skills._items('"a, b'), ['"a', "b"])
        self.assertEqual(skills._items('x "a, b"'), ['x "a', 'b"'])
        self.assertEqual(skills._items(" a.example ,b.example,, "), ["a.example", "b.example"])


class OutsideCase(SkillsCase):
    """A skill from outside: the source names an https URL, and the fetch
    the tests hand in reads a local repository over file:// instead,
    through the real fetch_folder."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.upstream, self.commit = make_upstream(tmp.name)
        local, real = "file://%s" % self.upstream, skills.fetch_folder
        self.fetch = lambda _url, commit, folder: real(local, commit, folder, schemes=("file",))

    def source(self, folder="skills/board-check"):
        return "https://example.test/wren-lab/skills@%s:%s" % (self.commit, folder)

    def imported(self, folder="skills/board-check", **kw):
        kw.setdefault("context", "personal")
        kw.setdefault("side_effect", "read-only")
        kw.setdefault("metadata", dict(PROMPTS, **{"dry-run-task": "run scripts/check.py"}))
        return skills.import_draft(self.wren, "wren", self.source(folder), fetch=self.fetch, **kw)

    def ready(self, name="board-check"):
        """Trigger test and dry run of this version, both passing."""
        self.run_trigger(self.wren, "wren", name)
        _text, sha = skills.diff_draft(self.root, self.wren, "wren", name)
        allowed = skills.allow_dry_run(self.root, self.wren, "wren", name, user="ana", diff_sha=sha)
        skills.record_dry_run(self.root, self.wren, "wren", name, 7, job=self.job(allowed))
        return sha

    def job(self, allowed, **kw):
        row = {"id": 7, "spawned_by": "wren", "status": "done", "exit_code": 0,
               "started_at": allowed["allowed_at"], "finished_at": allowed["allowed_at"],
               "command": "python3 scripts/check.py", "log_path": "/tmp/job-7.log"}
        row.update(kw)
        return row


class TestImport(OutsideCase):
    def test_the_operator_names_only_the_cousins_own_topics_too(self):
        with self.assertRaisesRegex(SkillError, '"board notes", which is no memory topic'):
            self.imported(metadata=dict(PROMPTS, **{"dry-run-task": "run scripts/check.py",
                                                     "rests-on": "board notes"}))
        memory._append_raw(self.wren, {"topic": "board notes", "content": "x",
                                       "truth_level": "L0_OPERATOR"})
        self.imported(metadata=dict(PROMPTS, **{"dry-run-task": "run scripts/check.py",
                                                 "rests-on": "board notes"}))

    def test_the_folder_comes_as_published_under_a_frontmatter_of_ours(self):
        path, report = self.imported()
        draft = self.wren / "skill-drafts" / "board-check"
        self.assertEqual(path, draft / "SKILL.md")
        upstream = self.upstream / "skills" / "board-check"
        for rel in ("LICENSE.txt", "scripts/check.py", "notes.md"):
            self.assertEqual((draft / rel).read_bytes(), (upstream / rel).read_bytes(), rel)
        self.assertTrue(os.access(draft / "scripts" / "check.py", os.X_OK))
        text = path.read_text()
        self.assertTrue(text.startswith("---\n# written by the cousins framework at import"))
        self.assertTrue(text.endswith(UPSTREAM_SKILL.split("---\n", 2)[2]))   # the body, exactly
        front, _ = skills.parse(text)
        self.assertEqual(front["description"], "Check a board. Use after a regen.")
        self.assertEqual(front["license"], "MIT")
        self.assertEqual(front["metadata"]["source"], self.source())
        self.assertNotIn("allowed-tools", front)
        self.assertEqual(report["not_kept"], ["allowed-tools", "license", "metadata"])
        self.assertEqual(skills.drafts(self.wren, "wren")[0]["problems"], [])
        self.assertEqual(skills.upstream_differences(self.wren, "board-check",
                                                     fetch=self.fetch)[1], [])
        self.assertEqual([s["path"] for s in skills.scripts_in(self.wren, "board-check")],
                         ["scripts/check.py"])

    def test_a_licence_only_at_the_root_comes_into_the_folder(self):
        _path, report = self.imported("skills/rootlic")
        self.assertTrue(report["license_from_root"])
        self.assertEqual((self.wren / "skill-drafts" / "rootlic" / "LICENSE").read_text(), MIT)

    def test_refusals(self):
        for folder, kw, word in (
                ("skills/linked", {}, "symlink"),
                ("skills/sub", {}, "submodule"),
                ("skills/closed", {}, "not one personal takes from outside"),
                ("skills/none", {}, "no folder skills/none"),
                ("skills/board-check", {"license": "Apache-2.0"}, "does not read as Apache-2.0"),
                ("skills/board-check", {"context": "work"}, "goes to personal or private"),
                ("skills/board-check", {"side_effect": "outward"}, "acts outward")):
            with self.assertRaisesRegex(SkillError, word, msg=(folder, kw)):
                self.imported(folder, **kw)
        with self.assertRaisesRegex(SkillError, "names the skill's folder"):
            skills.import_draft(self.wren, "wren", self.source().rsplit(":", 1)[0],
                                context="personal", side_effect="read-only", fetch=self.fetch)
        with self.assertRaisesRegex(SkillError, "fetched over https only"):
            skills.import_draft(self.wren, "wren", "http://example.test/o/r@%s:x" % self.commit,
                                context="personal", side_effect="read-only")
        with mock.patch.object(skills, "DRAFT_MAX_BYTES", 10):
            with self.assertRaisesRegex(SkillError, "over 10 bytes"):
                self.imported()
        self.assertFalse((self.wren / "skill-drafts").exists())

    def test_the_targets_are_where_an_import_lands(self):
        """The console's form offers import_targets only: each pair in it
        imports, and each pair outside it is refused."""
        got = self.fetch(None, self.commit, "skills/board-check")
        meta = dict(PROMPTS, **{"dry-run-task": "run scripts/check.py"})
        for slug, repos, want in (
                ("ivo", None, {"own": ["personal"], "shared": ["personal"]}),
                ("ada", ["personal", "private"], {"own": ["private"], "shared": ["personal"]}),
                ("kai", ["private"], {"own": ["private"], "shared": []}),
                ("max", ["work"], {"own": [], "shared": []})):
            home = self.home(slug, repos)
            self.assertEqual(skills.import_targets(home), want, slug)
            for scope in skills.SCOPES:
                for repo in skills.REPOS:
                    with self.subTest(slug=slug, scope=scope, repo=repo):
                        try:
                            skills.import_draft(home, slug, self.source(), context=repo,
                                                side_effect="read-only", scope=scope,
                                                metadata=meta, fetch=lambda *_a: got)
                        except SkillError:
                            took = False
                        else:
                            took = True
                        self.assertEqual(took, repo in want[scope])

    def test_what_a_draft_from_outside_needs_before_approval(self):
        self.imported(metadata=PROMPTS)
        found = skills.drafts(self.wren, "wren")[0]["problems"]
        self.assertEqual(found, ["a skill from outside needs metadata.dry-run-task: the sample"
                                 " task its dry run does"])
        self.imported()
        licence = self.wren / "skill-drafts" / "board-check" / "LICENSE.txt"
        licence.write_text(PROPRIETARY)
        self.assertIn("its licence file LICENSE.txt does not read as MIT",
                      skills.drafts(self.wren, "wren")[0]["problems"])
        licence.unlink()
        self.assertIn("a skill from outside keeps its licence file",
                      skills.drafts(self.wren, "wren")[0]["problems"])


class TestHostileTrees(OutsideCase):
    """Paths no honest host serves: a tree with literal `..` parts once
    wrote outside the draft. git's object check and the path rule each
    refuse them alone."""

    PARTS = (["..", "..", ".."], [".git"], [".GIT", "hooks"])

    def hostile(self, parts):
        """The fetch of a hostile repository (over file://), and its commit."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo, commit = make_hostile(tmp.name, parts)
        local, real = "file://%s" % repo, skills.fetch_folder
        return (lambda _url, commit, folder: real(local, commit, folder, schemes=("file",))), commit

    def import_from(self, parts):
        fetch, commit = self.hostile(parts)
        return skills.import_draft(self.wren, "wren", "https://example.test/o/r@%s:sk" % commit,
                                   context="personal", side_effect="read-only", fetch=fetch)

    def escaped(self):
        return sorted(str(p) for p in self.root.parent.rglob("escaped.txt"))

    def test_git_refuses_a_tree_out_of_the_folder_or_into_git(self):
        for parts in self.PARTS:
            with self.assertRaisesRegex(SkillError, "fetching file://.*has", msg=parts):
                self.import_from(parts)
        self.assertEqual(self.escaped(), [])
        self.assertFalse((self.wren / "skill-drafts").exists())

    def test_with_gits_check_off_the_path_rule_still_refuses_it(self):
        lax = tuple(s for s in skills.FETCH_CONFIG if not s.startswith("fetch.fsckObjects"))
        with mock.patch.object(skills, "FETCH_CONFIG", lax):
            for parts in self.PARTS:
                fetch, commit = self.hostile(parts)
                with self.assertRaisesRegex(SkillError, "sk holds a bad path", msg=parts):
                    fetch(None, commit, "sk")
                with self.assertRaisesRegex(SkillError, "sk holds a bad path", msg=parts):
                    self.import_from(parts)
        self.assertEqual(self.escaped(), [])
        self.assertFalse((self.wren / "skill-drafts").exists())

    def test_an_import_checks_the_paths_whatever_fetched_them(self):
        self.imported()
        draft = self.wren / "skill-drafts" / "board-check"
        before = sorted(str(p) for p in draft.rglob("*"))
        sound = self.fetch(None, self.commit, "skills/board-check")
        for bad in ("../escaped.txt", "scripts/../../escaped.txt", ".git/config", "a\nb"):
            got = dict(sound, files=dict(sound["files"], **{bad: b"out\n"}))
            with self.assertRaisesRegex(SkillError, "holds a bad path", msg=bad):
                skills.import_draft(self.wren, "wren", self.source(), context="personal",
                                    side_effect="read-only", fetch=lambda *_a, got=got: got)
        self.assertEqual(sorted(str(p) for p in draft.rglob("*")), before)    # the draft stands
        self.assertEqual(self.escaped(), [])

    def test_the_path_rule(self):
        for good in ("SKILL.md", "scripts/check.py", ".github/ci.yml", "a..b/c", "..a", "LICENSE.."):
            self.assertTrue(skills._path_ok(good), good)
        for bad in ("", ".", "..", "../x", "a/../b", "a/..", "./a", "/abs", "a//b", "a/", ".git",
                    ".GIT/config", "x/.Git/hooks", "a\\b", "a\nb", "a\tb", "a\x7fb", "\x1b[31m"):
            self.assertFalse(skills._path_ok(bad), repr(bad))

    def test_the_fetch_sees_none_of_our_git_settings(self):
        seen, run = [], subprocess.run

        def spy(*args, **kw):
            seen.append(kw.get("env") or {})
            return run(*args, **kw)
        planted = {"GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url.file:///nowhere/.insteadOf",
                   "GIT_CONFIG_VALUE_0": "file://",
                   "GIT_CONFIG_PARAMETERS": "'http.extraheader'='X-Planted: 1'"}
        with mock.patch.dict(os.environ, planted), mock.patch.object(skills.subprocess, "run", spy):
            got = self.fetch(None, self.commit, "skills/board-check")
        self.assertIn("SKILL.md", got["files"])      # the planted insteadOf sends it nowhere
        self.assertTrue(seen)
        for env in seen:
            self.assertEqual(sorted(k for k in env if k.startswith("GIT_") or k.endswith("ASKPASS")),
                             ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"])
            self.assertEqual(env["HOME"], env["XDG_CONFIG_HOME"])
            self.assertIn("cousin-skill-fetch-", env["HOME"])


class TestOutsideApproval(OutsideCase):
    def test_approval_needs_it_as_published_and_a_dry_run(self):
        self.imported()
        self.run_trigger(self.wren, "wren")
        _text, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        with self.assertRaisesRegex(SkillError, "no dry run on this version"):
            skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                 diff_sha=sha, fetch=self.fetch)
        sha = self.ready()
        entry = skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                     diff_sha=sha, fetch=self.fetch)
        self.assertEqual((entry["source"], entry["dry_run"], entry["trigger"]),
                         (self.source(), "pass", "pass"))
        self.assertEqual([s["name"] for s in skills.loaded(self.root, self.wren, "wren")],
                         ["board-check"])

    def test_an_edit_past_the_frontmatter_is_refused(self):
        self.imported()
        script = self.wren / "skill-drafts" / "board-check" / "scripts" / "check.py"
        script.write_text("#!/usr/bin/env python3\nprint('changed')\n")
        sha = self.ready()
        with self.assertRaisesRegex(SkillError, "not what .* published: scripts/check.py differs"):
            skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                 diff_sha=sha, fetch=self.fetch)

    def test_a_failed_dry_run_is_recorded_and_can_be_approved(self):
        self.imported()
        self.run_trigger(self.wren, "wren")
        _text, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        allowed = skills.allow_dry_run(self.root, self.wren, "wren", "board-check", user="ana",
                                       diff_sha=sha)
        ran = skills.record_dry_run(self.root, self.wren, "wren", "board-check", 7,
                                    job=self.job(allowed, status="failed", exit_code=1))
        self.assertFalse(ran["pass"])
        entry = skills.approve_draft(self.root, self.wren, "wren", "board-check", user="ana",
                                     diff_sha=sha, fetch=self.fetch)
        self.assertEqual(entry["dry_run"], "fail")

    def test_one_allow_takes_one_run(self):
        self.imported()
        _text, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        allowed = skills.allow_dry_run(self.root, self.wren, "wren", "board-check", user="ana",
                                       diff_sha=sha)
        skills.record_dry_run(self.root, self.wren, "wren", "board-check", 7,
                              job=self.job(allowed, status="failed", exit_code=1))
        with self.assertRaisesRegex(SkillError, r"recorded already \(job #7\): another run needs"
                                                r" another allow"):
            skills.record_dry_run(self.root, self.wren, "wren", "board-check", 8,
                                  job=self.job(allowed, id=8))
        record = json.loads(skills.dry_run_path(self.root, self.wren, "wren",
                                                "board-check").read_text())
        self.assertEqual((record["job"], record["pass"]), (7, False))
        again = skills.allow_dry_run(self.root, self.wren, "wren", "board-check", user="ana",
                                     diff_sha=sha)
        ran = skills.record_dry_run(self.root, self.wren, "wren", "board-check", 8,
                                    job=self.job(again, id=8))
        self.assertEqual((ran["job"], ran["pass"]), (8, True))

    def test_a_dry_run_is_the_jobs_and_only_for_the_allowed_version(self):
        self.imported()
        _text, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        with self.assertRaisesRegex(SkillError, "no dry run of this version .* is allowed"):
            skills.record_dry_run(self.root, self.wren, "wren", "board-check", 7, job={})
        allowed = skills.allow_dry_run(self.root, self.wren, "wren", "board-check", user="ana",
                                       diff_sha=sha)
        self.assertEqual(allowed["task"], "run scripts/check.py")
        for kw, word in (({"spawned_by": "otto"}, "otto's, not wren's"),
                         ({"status": "running"}, "still running"),
                         ({"started_at": "2020-01-01T00:00:00+00:00"}, "before the dry run"),
                         ({"exit_code": None}, "no exit code")):
            with self.assertRaisesRegex(SkillError, word, msg=kw):
                skills.record_dry_run(self.root, self.wren, "wren", "board-check", 7,
                                      job=self.job(allowed, **kw))
        with self.assertRaisesRegex(SkillError, "no job #7"):
            skills.record_dry_run(self.root, self.wren, "wren", "board-check", 7, job={})
        (self.wren / "skill-drafts" / "board-check" / "notes.md").write_text("changed\n")
        with self.assertRaisesRegex(SkillError, "no dry run of this version"):
            skills.record_dry_run(self.root, self.wren, "wren", "board-check", 7,
                                  job=self.job(allowed))
        with self.assertRaisesRegex(SkillError, "only a skill from outside"):
            self.draft(self.otto)
            _t, other = skills.diff_draft(self.root, self.otto, "otto", "board-check")
            skills.allow_dry_run(self.root, self.otto, "otto", "board-check", user="ana",
                                 diff_sha=other)


class TestOutsideCli(OutsideCase):
    def run_cli(self, *args, stdin=""):
        out, err = [], []
        with mock.patch("sys.stdin", __import__("io").StringIO(stdin)), \
                mock.patch("builtins.print", lambda *a, **k: (err if k.get("file") else out)
                           .append(" ".join(str(x) for x in a))):
            os.environ["FRAMEWORK_ROOT"] = str(self.root)
            code = skills.main(["--home", str(self.wren), *args])
        return code, "\n".join(out), "\n".join(err)

    def test_the_operator_imports_from_a_terminal_and_the_cousin_records_the_dry_run(self):
        """The operator's `cousin-skill --home <home> import` (no COUSIN_HOME
        in their terminal), then the dry run recorded through the command
        line the opencode and tmux lanes build from the shipped registry,
        which has no import any more."""
        from cousin_lib import jobs, mcp_server
        registry = mcp_server.load_registry(
            pathlib.Path(skills.__file__).resolve().parents[1] / "config"
            / "mcp-registry.toml.example")
        tool = registry["tools"]["skill"]
        self.assertNotIn("import", tool["commands"])
        os.environ.pop("COUSIN_HOME", None)
        with mock.patch.object(skills, "fetch_folder", self.fetch):
            code, out, err = self.run_cli(
                "import", self.source(), "--context", "personal", "--side-effect", "read-only",
                "--should-trigger", PROMPTS["should-trigger"],
                "--must-not-trigger", PROMPTS["must-not-trigger"],
                "--dry-run-task", "run scripts/check.py", "--hosts", "")
        self.assertEqual(code, 0, err)
        self.assertIn("licence MIT (LICENSE.txt)", out)
        self.assertIn("upstream's allowed-tools, license, metadata not kept", out)
        self.assertNotIn("before approval", out)
        _text, sha = skills.diff_draft(self.root, self.wren, "wren", "board-check")
        skills.allow_dry_run(self.root, self.wren, "wren", "board-check", user="ana", diff_sha=sha)
        job_id = jobs.register_job(kind="shell", title="dry run", spawned_by="wren")
        jobs.finish_job(job_id, status="done", exit_code=0)
        argv, _ = mcp_server.build_call(tool, "dry-run", {"name": "board-check", "job": job_id},
                                        resolve=lambda c: c)
        code, out, err = self.run_cli(*argv[1:])
        self.assertEqual(code, 0, err)
        self.assertIn("dry run recorded: job #%d done, exit 0, so a pass" % job_id, out)


class TestImportIsTheOperators(OutsideCase):
    """Importing a skill from outside is the operator's act
    (docs/reference/skills.md, "Who does what"): a cousin's session cannot."""

    def test_the_command_refuses_inside_a_cousins_environment(self):
        os.environ["COUSIN_HOME"] = str(self.wren)
        out, err = [], []
        with mock.patch.object(skills, "fetch_folder", self.fetch), \
                mock.patch("builtins.print", lambda *a, **k: (err if k.get("file") else out)
                           .append(" ".join(str(x) for x in a))):
            code = skills.main(["--home", str(self.wren), "import", self.source(),
                                "--context", "personal", "--side-effect", "read-only"])
        self.assertEqual(code, 2)
        self.assertIn("importing a skill from outside is the operator's act", "\n".join(err))
        self.assertEqual(skills.drafts(self.wren, "wren"), [])

    def test_the_guard_refuses_an_import_from_a_cousins_shell(self):
        refused = ["cousin-skill import %s --context personal --side-effect read-only"
                   % self.source(),
                   "cousin-skill --home /x import %s" % self.source(),
                   "cd /tmp && env FOO=1 cousin-skill import %s" % self.source(),
                   "python3 -I -m cousin_lib.skills import %s" % self.source()]
        for command in refused:
            with self.subTest(command=command):
                why = skills.guard_tool("Bash", {"command": command}, root=self.root,
                                        home=self.wren)
                self.assertIn("operator's act", why or "")
        for command in ("cousin-skill list", "cousin-skill check", "echo import"):
            with self.subTest(command=command):
                self.assertIsNone(skills.guard_tool("Bash", {"command": command},
                                                    root=self.root, home=self.wren))

    def test_the_tool_has_no_import(self):
        from cousin_lib.runner import tools
        self.assertNotIn("import", tools.HANDLERS["skill"])


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

    def test_a_script_stays_executable_and_read_only_in_the_export(self):
        self.draft(self.wren)
        script = self.wren / "skill-drafts" / "board-check" / "scripts" / "check.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\necho ok\n")
        script.chmod(0o755)
        (script.parent / "notes.txt").write_text("plain\n")
        self.approve(self.wren, "wren")
        paths, _ = skills.export_plugins(self.root, self.wren, "wren")
        exported = paths[-1] / "skills" / "board-check" / "scripts"
        self.assertEqual(stat.S_IMODE((exported / "check.sh").stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((exported / "notes.txt").stat().st_mode), 0o444)
        out = subprocess.run([str(exported / "check.sh")], capture_output=True, text=True)
        self.assertEqual(out.stdout, "ok\n")

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
        self.assertIn('should-trigger: "check the regenerated board"', out)
        _, out, _ = self.run_cli("diff", "board-check")
        self.assertIn("+1. Read the board.", out)
        code, _, err = self.run_cli("diff", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no draft nope", err)

    def test_the_declared_keys_from_a_terminal(self):
        base = ("draft", "board-check", "--description", "Check a board.", "--context",
                "personal", "--side-effect", "read-only")
        code, out, _ = self.run_cli(*base, "--hosts", "", "--reads", "w@%s:tools/x.py" % COMMIT,
                                    "--rests-on", "kicad-cli --version", "--source",
                                    "https://g.example/o/r@" + COMMIT, "--license", "MIT",
                                    stdin=BODY)
        self.assertEqual(code, 0, out)
        _, out, _ = self.run_cli("show", "board-check")
        for line in ('license: "MIT"', 'hosts: ""', 'reads: "w@%s:tools/x.py"' % COMMIT,
                     'rests-on: "kicad-cli --version"',
                     'source: "https://g.example/o/r@%s"' % COMMIT):
            self.assertIn(line, out)
        code, _, err = self.run_cli(*base, "--hosts", "https://a.example", stdin=BODY)
        self.assertEqual(code, 2)
        self.assertIn("metadata.hosts takes host names", err)

    def test_the_tool_registry_maps_every_draft_key_to_a_flag_the_command_takes(self):
        """The opencode and tmux lanes run the shipped registry's draft
        command line: each property it passes is a flag cousin-skill
        accepts, and an empty hosts still reaches it."""
        from cousin_lib import mcp_server
        registry = mcp_server.load_registry(
            pathlib.Path(skills.__file__).resolve().parents[1] / "config"
            / "mcp-registry.toml.example")
        tool = registry["tools"]["skill"]
        args = {"name": "board-check", "description": "Check a board.", "context": "personal",
                "side_effect": "read-only", "body": BODY, "scope": "own",
                "should_trigger": PROMPTS["should-trigger"],
                "must_not_trigger": PROMPTS["must-not-trigger"], "hosts": "",
                "reads": "w@%s:tools/x.py" % COMMIT, "rests_on": "kicad-cli --version",
                "source": "https://g.example/o/r@" + COMMIT, "license": "MIT"}
        args["source"] += ":skills/board-check"
        args["dry_run_task"] = "run the check"
        self.assertEqual(set(args), set(tool["properties"]) - {"command", "job"})
        argv, stdin = mcp_server.build_call(tool, "draft", args, resolve=lambda c: c)
        self.assertEqual(argv[0], "cousin-skill")
        self.assertEqual(argv[argv.index("--hosts") + 1], "")
        code, out, _ = self.run_cli(*argv[1:], stdin=stdin.decode())
        self.assertEqual(code, 0, out)
        # the draft tool writes no licence file: the one thing still missing
        self.assertIn("before approval: a skill from outside keeps its licence file", out)
        meta = skills.parse((self.wren / "skill-drafts" / "board-check" / "SKILL.md")
                            .read_text())[0]["metadata"]
        self.assertEqual((meta["hosts"], meta["rests-on"]), ("", "kicad-cli --version"))


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

    def test_a_draft_that_edits_an_approved_skill_is_tested_instead_of_it(self):
        # the approved copy has the same name and description: loaded beside
        # the draft it takes the should-trigger prompt and fails a sound edit
        self.draft(self.wren)
        self.approve(self.wren, "wren")
        self.draft(self.wren, name="board-export")
        self.approve(self.wren, "wren", "board-export")
        self.draft(self.wren, body=BODY + "3. Check the silkscreen too.\n")
        seen = []

        def checker(prompt, plugin_dirs):
            seen.append(sorted("%s:%s" % (d.name, s.name) for d in plugin_dirs
                               for s in (d / "skills").iterdir()))
            return ("draft:board-check" if "board" in prompt else None), "ok"
        result = skills.trigger_test(self.root, self.wren, "wren", "board-check", checker=checker)
        self.assertTrue(result["pass"])
        # the rest of what loads is still there; only the copy being replaced is not
        self.assertEqual(seen, [["draft:board-check", "personal:board-export"]] * 2)
        # and the cousin's own export, outside a test, still has both
        dirs, _ = skills.export_plugins(self.root, self.wren, "wren",
                                        base=self.root / "export-check")
        self.assertEqual(sorted(s.name for d in dirs for s in (d / "skills").iterdir()),
                         ["board-check", "board-export"])

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
