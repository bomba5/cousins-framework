"""cousin-upgrade (cousin_lib/upgrade.py): the plan, every text from
git, each home planned by template_sync's structural sync, nothing
written; and --apply-homes, which applies the homes' part with a backup,
a strict check and a recorded state."""
import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

from cousin_lib import (mcp_server, shared_tier, template_sync, upgrade,
                        upgrade_switch)

MARKER = template_sync.MARKER

OLD_REGISTRY = (
    'operators = []\n\n'
    '[tools.memory]\n'
    'command = "cousin-memory"\n'
    'description = "memory, as shipped first"\n\n'
    '[tools.memory.properties]\n'
    'topic = { type = "string", description = "the topic" }\n\n'
    '[tools.memory.commands.search]\n'
    'argv = ["search", "{topic}"]\n\n'
    '[tools.legacy]\n'
    'command = "cousin-legacy"\n\n'
    '[tools.legacy.commands.run]\n'
    'argv = ["run"]\n')

NEW_REGISTRY = (
    'operators = []\n\n'
    '[tools.memory]\n'
    'command = "cousin-memory"\n'
    'description = "memory, as shipped now"\n\n'
    '[tools.memory.properties]\n'
    'topic = { type = "string", description = "the topic, retired or not" }\n'
    'why = { type = "string", description = "what superseded it" }\n\n'
    '[tools.memory.commands.search]\n'
    'argv = ["search", "{topic}"]\n')

MIGRATIONS = {
    ("tools.memory", "description"): (("memory, as shipped first", None),),
    ("tools.memory.properties", "topic.description"): (
        ("the topic", "the topic, retired or not"),),
}


def _template(chat):
    return ("# {{NAME}} - {{ROLE_ONE_LINE}}\n\n## Identity\n\n"
            "{{ROLE_PARAGRAPH}}\n\n## Chat\n\n%s\n\n%s\n" % (chat, MARKER))


def _pyproject(version, deps):
    return ('[project]\nname = "cousins-framework"\nversion = "%s"\n'
            'dependencies = %s\n' % (version, json.dumps(deps)))


CHANGELOG = (
    "# Changelog\n\n"
    "## 3.11.0 - 2026-10-03\n\n### Added\n\n- **Not released yet.**\n\n"
    "## 3.10.0 - 2026-10-02\n\n### Added\n\n"
    "- **A registry key for the reason a\n  topic was retired.** More.\n\n"
    "### Fixed\n\n- **The memory wording.** More.\n\n"
    "## 3.9.0 - 2026-10-01\n\n### Added\n\n- **The first one.**\n")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Wren",
                    "-c", "user.email=wren@example.invalid",
                    "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                    *args], check=True, capture_output=True, text=True)


def _release(repo, version, *, registry, chat, law, rule, deps, tag=True,
             extra=None):
    files = {"pyproject.toml": _pyproject(version, deps),
             upgrade.REGISTRY_EXAMPLE: registry,
             upgrade.TEMPLATE: _template(chat),
             upgrade.LAW: law,
             upgrade.RULES + "/reference_wren-rule.md": rule,
             upgrade.RULES + "/examples/reference_not-seeded.md": "x\n",
             "CHANGELOG.md": CHANGELOG, **(extra or {})}
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release %s" % version)
    if tag:
        _git(repo, "tag", "v%s" % version)


def _snapshot(top):
    """{relative path: (bytes, mtime_ns)} of every file under `top`."""
    out = {}
    for dirpath, _dirs, files in os.walk(top):
        for name in files:
            path = pathlib.Path(dirpath) / name
            st = path.stat()
            out[str(path.relative_to(top))] = (path.read_bytes(),
                                               st.st_mtime_ns)
    return out


class _Install(unittest.TestCase):
    """A checkout with releases v3.9.0, v3.9.5 and v3.10.0, and an install
    whose homes were rendered from v3.9.0."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = pathlib.Path(tmp.name)
        self.repo = self.top / "checkout"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _release(self.repo, "3.9.0", registry=OLD_REGISTRY,
                 chat="old chat wording", law="the law\n",
                 rule="a rule\n", deps=["tomli>=1"])
        _git(self.repo, "tag", "v3.9.5")
        _git(self.repo, "tag", "v3.10.0-rc1")
        _release(self.repo, "3.10.0", registry=NEW_REGISTRY,
                 chat="new chat wording", law="the law, amended\n",
                 rule="a rule\n", deps=["tomli>=2"])
        self.root = self.top / "install"
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "law.md").write_text("the law\n")
        (self.root / "shared").mkdir()
        (self.root / "shared" / "reference_wren-rule.md").write_text(
            "a rule\n")
        rendered = mcp_server.render_registry(OLD_REGISTRY, ["ana"])
        self.homes = {}
        for slug, registry in (
                ("wren", rendered),
                ("kestrel", rendered.replace(
                    '"memory, as shipped first"', '"kestrel\'s own words"')),
                ("testa", rendered),
                ("sam", None),
                ("toki", rendered)):
            home = self.root / "cousins" / slug
            (home / "data").mkdir(parents=True)
            agent = '[agent]\nrunner = "sdk"\n' if slug in ("wren", "toki") \
                else ""
            (home / "cousin.toml").write_text(
                '[cousin]\nslug = "%s"\nname = "%s"\nrole = "notes"\n%s'
                % (slug, slug.capitalize(), agent))
            (home / "CLAUDE.md").write_text(template_sync._render(
                _template("old chat wording"),
                {"NAME": slug.capitalize(), "ROLE_ONE_LINE": "notes",
                 "ROLE_PARAGRAPH": "My own role."}) + "\n## Mine\n\nkept\n")
            if registry is not None:
                (home / "mcp-registry.toml").write_text(registry)
            self.homes[slug] = home
        (self.homes["toki"] / "data" / "template-sync.json").write_text(
            json.dumps({"to": "3.10.0", "ref": "v3.10.0", "at": 1,
                        "registry": "applied"}))
        patch = mock.patch.dict(template_sync._MIGRATIONS, MIGRATIONS)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("COUSIN_HOME", None)

    def plan(self, **kw):
        kw.setdefault("running", "3.9.0")
        return upgrade.build_plan(self.root, self.repo, **kw)

    def home(self, plan, slug):
        return next(h for h in plan["homes"] if h["slug"] == slug)


class TestResolve(_Install):

    def test_the_newest_tag_is_by_version_not_by_text(self):
        self.assertEqual(upgrade.newest_tag(self.repo), "v3.10.0")
        plain = subprocess.run(["git", "-C", str(self.repo), "tag"],
                               capture_output=True, text=True).stdout
        self.assertEqual(plain.split()[-1], "v3.9.5")   # what tail -1 says
        self.assertEqual(self.plan()["to"]["ref"], "v3.10.0")

    def test_a_downgrade_is_refused_unless_named(self):
        with self.assertRaises(upgrade.Refused):
            self.plan(running="3.11.0")
        plan = self.plan(running="3.11.0", to="v3.9.0")
        self.assertEqual(plan["direction"], "downgrade")
        self.assertTrue(plan["to"]["explicit"])

    def test_a_ref_that_names_nothing_is_refused(self):
        with self.assertRaises(upgrade.Refused):
            self.plan(to="v9.9.9")

    def test_from_is_the_running_tag(self):
        plan = self.plan()
        self.assertEqual(plan["from"]["ref"], "v3.9.0")
        self.assertEqual(plan["direction"], "upgrade")

    def test_from_falls_back_to_head_without_a_tag(self):
        plan = self.plan(running="3.9.7")
        self.assertEqual(plan["from"]["ref"], "HEAD")
        self.assertTrue(any("no tag v3.9.7" in n for n in plan["notes"]))


class TestPlan(_Install):

    def test_changelog_and_dependencies_between_from_and_to(self):
        plan = self.plan()
        self.assertEqual([s["version"] for s in plan["changelog"]],
                         ["3.10.0"])
        groups = plan["changelog"][0]["groups"]
        self.assertEqual(groups[0]["title"], "Added")
        self.assertEqual(groups[0]["leads"],
                         ["A registry key for the reason a topic was"
                          " retired."])
        self.assertEqual(groups[1]["leads"], ["The memory wording."])
        self.assertEqual(len(plan["dependencies"]), 1)
        self.assertIn("tomli>=2", plan["dependencies"][0])

    def test_a_missing_key_is_added_and_framework_text_migrated(self):
        reg = self.home(self.plan(), "wren")["registry"]
        self.assertEqual(reg["status"], "changes")
        self.assertEqual(reg["added"], ["tools.memory.properties.why"])
        self.assertEqual(sorted(reg["migrated"]),
                         ["tools.memory.description",
                          "tools.memory.properties.topic"])

    def test_an_operator_edited_value_is_not_migrated(self):
        reg = self.home(self.plan(), "kestrel")["registry"]
        self.assertNotIn("tools.memory.description", reg["migrated"])
        self.assertIn("tools.memory.properties.why", reg["added"])

    def test_a_retired_entry_is_reported_never_pruned(self):
        before = (self.homes["testa"] / "mcp-registry.toml").read_text()
        reg = self.home(self.plan(), "testa")["registry"]
        self.assertEqual(reg["retired"], ["tools.legacy"])
        self.assertEqual(
            (self.homes["testa"] / "mcp-registry.toml").read_text(), before)

    def test_a_home_with_no_registry_is_listed(self):
        plan = self.plan()
        self.assertEqual(self.home(plan, "sam")["registry"],
                         {"status": "none"})
        self.assertIn("no mcp-registry.toml", upgrade.render(plan))

    def test_from_per_home_comes_from_template_sync_json(self):
        toki = self.home(self.plan(), "toki")
        self.assertEqual(toki["from"], {"ref": "v3.10.0", "version": "3.10.0",
                                        "source": upgrade.STATE})
        self.assertEqual(toki["registry"]["retired"], [])

    def test_claude_md_is_diffed_against_the_target_template(self):
        c = self.home(self.plan(), "wren")["claude_md"]
        self.assertEqual(c["status"], "differs")
        self.assertIn("+new chat wording", c["diff"])
        self.assertIn("My own role.", (self.homes["wren"]
                                        / "CLAUDE.md").read_text())

    def test_seeded_files_are_compared_with_the_target(self):
        rows = {r["path"]: r for r in self.plan()["seeded"]}
        self.assertEqual(rows["config/law.md"]["status"], "differs")
        self.assertIn("+the law, amended", rows["config/law.md"]["diff"])
        self.assertEqual(rows["shared/reference_wren-rule.md"]["status"],
                         "same")
        self.assertNotIn("shared/reference_not-seeded.md", rows)

    def test_mcp_json_and_policy_are_reported(self):
        wren = self.homes["wren"]
        (wren / ".mcp.json").write_text(json.dumps(mcp_server.mcp_json(
            wren, "wren", self.root), indent=2) + "\n")
        (wren / "policy.toml").write_text("[policy]\n")
        h = self.home(self.plan(), "wren")
        self.assertEqual(h["mcp_json"], {"status": "in step"})
        self.assertTrue(h["policy"]["present"])
        self.assertEqual(self.home(self.plan(), "sam")["mcp_json"],
                         {"status": "absent"})

    def test_restarts_put_the_callers_runner_last_and_detached(self):
        os.environ["COUSIN_HOME"] = str(self.homes["wren"])
        r = self.plan()["restarts"]
        self.assertIsNotNone(r["supervisor"])       # none runs here
        names = [row["name"] for row in r["order"]]
        self.assertEqual(names, ["loops", "console", "runner:toki",
                                 "runner:wren"])
        self.assertTrue(r["order"][-1]["detached"])
        self.assertFalse(r["order"][-2]["detached"])

    def test_nothing_is_written(self):
        before = _snapshot(self.top)
        plan = self.plan()
        upgrade.render(plan, full=True)
        json.dumps(plan)
        self.assertEqual(_snapshot(self.top), before)


class TestCLI(_Install):

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = upgrade.upgrade_main(["--root", str(self.root),
                                         "--checkout", str(self.repo),
                                         *argv])
        return code, out.getvalue(), err.getvalue()

    def test_without_a_mode_it_refuses(self):
        code, out, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("--apply-homes", err)
        code, out, err = self.run_cli("--dry-run", "--yes")
        self.assertEqual(code, 2)
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            self.run_cli("--dry-run", "--apply-homes")
        self.assertEqual(cm.exception.code, 2)

    def test_a_dry_run_prints_the_plan_and_writes_nothing(self):
        _release(self.repo, "3.9.0", registry=OLD_REGISTRY, chat="x",
                 law="the law\n", rule="a rule\n", deps=["tomli>=1"],
                 tag=False)                     # HEAD runs 3.9.0 again
        before = _snapshot(self.top)
        code, out, err = self.run_cli("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("to        3.10.0 (v3.10.0", out)
        self.assertIn("dependencies changed", out)
        self.assertIn("retired, kept 1 (tools.legacy)", out)
        code, out, err = self.run_cli("--dry-run", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["to"]["version"], "3.10.0")
        self.assertEqual(_snapshot(self.top), before)

    def test_a_downgrade_from_the_cli_is_refused_with_2(self):
        _release(self.repo, "3.11.0", registry=NEW_REGISTRY, chat="x",
                 law="the law\n", rule="a rule\n", deps=["tomli>=2"],
                 tag=False)
        code, _out, err = self.run_cli("--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("--to v3.10.0 plans the downgrade", err)
        code, out, err = self.run_cli("--dry-run", "--to", "v3.10.0")
        self.assertEqual(code, 0, err)
        self.assertIn("[downgrade]", out)
        self.assertIn("changelog (rolled back)", out)

    def test_not_a_checkout_is_exit_1(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = upgrade.upgrade_main(["--root", str(self.root),
                                         "--checkout", str(self.root),
                                         "--dry-run"])
        self.assertEqual(code, 1)


class _Terminal(io.StringIO):
    """A stdin that says it is a terminal, with the answer typed."""

    def isatty(self):
        return True


class _Pipe(io.StringIO):
    def isatty(self):
        return False


def _sha(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse",
                           "%s^{commit}" % ref], capture_output=True,
                          text=True, check=True).stdout.strip()


class TestApplyHomes(_Install):
    """--apply-homes: the registry plan applied per home, a backup first,
    the result checked strictly, the state recorded."""

    BACKUP = "data/mcp-registry.toml.pre-3.10.0"

    def setUp(self):
        super().setUp()
        self.original = {s: (h / "mcp-registry.toml").read_text()
                         for s, h in self.homes.items() if s != "sam"}
        wren = self.homes["wren"]
        stale = mcp_server.mcp_json(wren, "wren", self.root)
        stale["mcpServers"]["cousin"]["args"] = ["--registry", "/elsewhere"]
        stale["mcpServers"]["other"] = {"command": "kept"}
        (wren / ".mcp.json").write_text(json.dumps(stale, indent=2) + "\n")

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(upgrade.sys, "stdin", stdin or _Pipe()):
            code = upgrade.upgrade_main(["--root", str(self.root),
                                         "--checkout", str(self.repo),
                                         *argv])
        return code, out.getvalue(), err.getvalue()

    def apply(self, *argv):
        with mock.patch.object(upgrade.version, "version",
                               return_value="3.9.0"), \
                mock.patch.object(upgrade.version, "CHECKOUT", self.repo):
            return self.run_cli("--apply-homes", "--yes", *argv)

    def registry(self, slug):
        return tomllib.loads((self.homes[slug]
                              / "mcp-registry.toml").read_text())

    def state(self, slug):
        return json.loads((self.homes[slug] / upgrade.STATE).read_text())

    def test_apply_adds_migrates_and_keeps_the_homes_own_values(self):
        code, out, err = self.apply()
        self.assertEqual(code, 0, out + err)
        wren = self.registry("wren")["tools"]
        self.assertEqual(wren["memory"]["description"],
                         "memory, as shipped now")
        self.assertEqual(wren["memory"]["properties"]["topic"]["description"],
                         "the topic, retired or not")
        self.assertIn("why", wren["memory"]["properties"])
        self.assertIn("legacy", wren)                   # reported, kept
        kestrel = self.registry("kestrel")["tools"]["memory"]
        self.assertEqual(kestrel["description"], "kestrel's own words")
        self.assertIn("why", kestrel["properties"])
        self.assertIn("wren           applied: added 1, migrated 2,"
                      " retired 1 reported (tools.legacy), backup "
                      + self.BACKUP, out)
        self.assertIn("sam            skipped: no mcp-registry.toml", out)
        self.assertIn("CLAUDE.md is reported only", out)

    def test_a_backup_first_and_the_state_recorded(self):
        self.apply()
        wren = self.homes["wren"]
        self.assertEqual((wren / self.BACKUP).read_text(),
                         self.original["wren"])
        self.assertEqual(self.state("wren"), {
            "to": "3.10.0", "ref": _sha(self.repo, "v3.10.0"),
            "at": self.state("wren")["at"], "registry": "applied"})
        self.assertFalse((self.homes["sam"] / upgrade.STATE).exists())
        mcp = json.loads((wren / ".mcp.json").read_text())["mcpServers"]
        self.assertEqual(mcp["cousin"]["args"][1],
                         str(wren / "mcp-registry.toml"))
        self.assertEqual(mcp["other"], {"command": "kept"})

    def test_a_second_run_is_in_step_and_keeps_the_backup(self):
        self.apply()
        applied = self.registry("wren")
        for h in self.plan()["homes"]:
            if h["slug"] != "sam":
                self.assertEqual(h["registry"]["status"], "in step",
                                 h["slug"])
                self.assertEqual(h["from"]["source"], upgrade.STATE)
        code, out, err = self.apply()
        self.assertEqual(code, 0, out + err)
        self.assertIn("wren           in step\n", out)
        self.assertEqual(self.registry("wren"), applied)
        self.assertEqual(self.state("wren")["registry"], "in-step")
        self.assertFalse((self.homes["wren"] / (self.BACKUP + ".2"))
                         .exists())

    def test_an_existing_backup_is_never_overwritten(self):
        older = self.homes["wren"] / self.BACKUP
        older.write_text("an older copy\n")
        self.apply("--home", "wren")
        self.assertEqual(older.read_text(), "an older copy\n")
        self.assertEqual((self.homes["wren"] / (self.BACKUP + ".2"))
                         .read_text(), self.original["wren"])

    def test_a_failed_check_restores_the_copy_and_exits_1(self):
        with mock.patch.object(upgrade.mcp_server, "list_tools",
                               side_effect=mcp_server.RegistryError(
                                   "tools.memory: broken")):
            code, out, err = self.apply("--home", "wren")
        self.assertEqual(code, 1, out + err)
        self.assertIn("FAILED: RegistryError: tools.memory: broken; backup"
                      " restored", out)
        wren = self.homes["wren"]
        self.assertEqual((wren / "mcp-registry.toml").read_text(),
                         self.original["wren"])
        self.assertEqual((wren / self.BACKUP).read_text(),
                         self.original["wren"])
        state = self.state("wren")
        self.assertTrue(state["registry"].startswith("failed: "))
        self.assertEqual(state["from"], _sha(self.repo, "v3.9.0"))
        again = self.home(self.plan(), "wren")
        self.assertEqual(again["from"], {"ref": _sha(self.repo, "v3.9.0"),
                                         "source": upgrade.STATE})
        self.assertEqual(again["registry"]["status"], "changes")
        self.assertEqual(again["registry"]["retired"], ["tools.legacy"])
        self.assertTrue(any("planned again" in n for n in again["notes"]))
        self.assertIn("/elsewhere", (wren / ".mcp.json").read_text())

    def test_home_limits_the_set(self):
        before = _snapshot(self.homes["kestrel"])
        code, out, err = self.apply("--home", "wren", "--home", "testa")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("kestrel", out)
        self.assertEqual(_snapshot(self.homes["kestrel"]), before)
        self.assertEqual(self.state("testa")["registry"], "applied")
        code, out, err = self.apply("--home", "mallory")
        self.assertEqual(code, 2)
        self.assertIn("--home mallory: no such home", err)

    def test_prune_retired_removes_only_the_retired(self):
        code, out, err = self.apply("--home", "testa", "--prune-retired")
        self.assertEqual(code, 0, out + err)
        tools = self.registry("testa")["tools"]
        self.assertNotIn("legacy", tools)
        self.assertEqual(tools["memory"]["commands"]["search"]["argv"],
                         ["search", "{topic}"])
        self.assertNotIn("[tools.legacy",
                         (self.homes["testa"] / "mcp-registry.toml")
                         .read_text())
        self.assertIn("pruned 1 (tools.legacy)", out)
        self.assertEqual((self.homes["testa"] / self.BACKUP).read_text(),
                         self.original["testa"])

    def test_nothing_else_is_written(self):
        before = _snapshot(self.top)
        code, out, err = self.apply("--prune-retired")
        self.assertEqual(code, 0, out + err)
        after = _snapshot(self.top)
        allowed = re.compile(r"^install/cousins/[a-z]+/(mcp-registry\.toml"
                             r"|\.mcp\.json|data/template-sync\.json"
                             r"|data/mcp-registry\.toml\.pre-3\.10\.0)$")
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        self.assertTrue(changed)
        self.assertEqual([k for k in changed if not allowed.match(k)], [])

    def test_without_yes_and_no_terminal_it_refuses(self):
        before = _snapshot(self.top)
        code, out, err = self.run_cli("--apply-homes")
        self.assertEqual(code, 2)
        self.assertIn("pass --yes", err)
        self.assertEqual(_snapshot(self.top), before)

    def test_the_prompt_takes_y_and_anything_else_is_no(self):
        before = _snapshot(self.top)
        code, out, err = self.run_cli("--apply-homes", "--home", "wren",
                                      stdin=_Terminal("n\n"))
        self.assertEqual(code, 2)
        self.assertIn("apply to 1 home? [y/N]", err)
        self.assertEqual(_snapshot(self.top), before)
        code, out, err = self.run_cli("--apply-homes", "--home", "wren",
                                      stdin=_Terminal("y\n"))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.state("wren")["registry"], "applied")

    def test_json_reports_what_was_applied(self):
        code, out, err = self.apply("--home", "sam", "--home", "wren",
                                    "--json")
        self.assertEqual(code, 0, err)
        rows = {r["slug"]: r for r in json.loads(out)["applied"]}
        self.assertEqual(rows["sam"]["registry"], "skipped")
        self.assertEqual(rows["wren"]["registry"], "applied")
        self.assertEqual(rows["wren"]["mcp_json"], "refreshed")


class _FakeOps:
    """upgrade_switch.Ops with nothing real behind it: git, pip, the
    probe, the supervisor, the runners' turn states and what each
    process says it runs are tables; the clock moves only when slept."""

    def __init__(self, root, checkout, versions, head):
        self.root, self.checkout = pathlib.Path(root), pathlib.Path(checkout)
        self.python = "/opt/venv/bin/python3"
        self.versions = versions            # commit -> pyproject version
        self.head_sha = head
        self.calls = []
        self.git_rc = {}                    # first arg -> returncode
        self.pip_rc = 0
        self.probe_checkout = str(checkout)
        self.probe_version = None           # else the version at head
        self.children = {"loops": {"state": "running", "pid": 101},
                         "console": {"state": "running", "pid": 102},
                         "runner:toki": {"state": "running", "pid": 103},
                         "runner:wren": {"state": "running", "pid": 104}}
        self.no_supervisor = False
        self.says = {}                      # name -> what it announced
        self.wrong = {}                     # name -> the version it comes back on
        self.silent = set()                 # comes back saying nothing
        self.down = set()                   # never comes back up
        self.turns = {}                     # slug -> [states], the last repeats
        self.pids = iter(range(201, 300))
        self.clock = 0.0
        self.detached = []

    def git(self, *args, timeout=120):
        self.calls.append(("git",) + args)
        rc = self.git_rc.get(args[0], 0)
        if rc:
            return rc, "", "%s failed" % args[0]
        if args[0] == "checkout":
            self.head_sha = args[-1]
        if args[:2] == ("rev-parse", "HEAD"):
            return 0, self.head_sha + "\n", ""
        return 0, "", ""

    def pip_install(self, deps):
        self.calls.append(("pip", deps))
        return self.pip_rc, "pip said no" if self.pip_rc else ""

    def probe(self):
        self.calls.append(("probe",))
        return {"version": self.probe_version or self.versions[self.head_sha],
                "checkout": self.probe_checkout, "commit": self.head_sha[:7]}

    def supervisor(self, op, timeout=10.0, **args):
        from cousin_lib.supervisor import SupervisorUnavailable
        if self.no_supervisor:
            raise SupervisorUnavailable("no cousin-supervisor answers")
        if op == "status":
            return {"ok": True, "children": json.loads(json.dumps(
                self.children))}
        name = ("runner:%s" % args["slug"]) if "slug" in args \
            else args["name"]
        self.calls.append((op, name))
        child = self.children[name]
        if op == "stop":
            child.update(state="stopped", pid=None)
            return {"ok": True, "name": name, "state": "stopped"}
        if name in self.down:
            child.update(state="backoff", pid=None, reason="exit 1")
            return {"ok": False, "name": name, "state": "backoff",
                    "error": "exit 1"}
        pid = next(self.pids)
        child.update(state="running", pid=pid)
        if name not in self.silent:
            self.says[name] = {"pid": pid, "checkout": str(self.checkout),
                               "version": self.wrong.get(
                                   name, self.versions[self.head_sha]),
                               "commit": self.head_sha[:7]}
        return {"ok": True, "name": name, "state": "running"}

    def runner_state(self, home):
        states = self.turns.get(pathlib.Path(home).name) or ["idle"]
        return states.pop(0) if len(states) > 1 else states[0]

    def head(self):
        return self.head_sha, self.versions[self.head_sha]

    def announced(self, name):
        return self.says.get(name)

    def alive(self, pid):
        return any(c.get("pid") == pid for c in self.children.values())

    def spawn_detached(self, argv, env, unit):
        self.detached.append((argv, env, unit))
        return "a fake unit"

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def wall(self):
        return 1000

    def restarts(self):
        return [c for c in self.calls if c[0] in ("stop", "start")]


class _Switch(_Install):
    """The install of _Install, with a v3.11.0 whose processes say which
    release they run and which can restart its caller detached, and the
    checkout on a branch `work` at v3.9.0."""

    def setUp(self):
        super().setUp()
        _release(self.repo, "3.11.0", registry=NEW_REGISTRY,
                 chat="new chat wording", law="the law, amended\n",
                 rule="a rule\n", deps=["tomli>=2"], extra={
                     "cousin_lib/version.py": "def announce(root, name):\n",
                     "cousin_lib/upgrade_switch.py":
                         "RESTART_PROTOCOL = 1\n"})
        _git(self.repo, "checkout", "-q", "-B", "work", "v3.9.0")
        self.sha = {v: _sha(self.repo, "v" + v)
                    for v in ("3.9.0", "3.10.0", "3.11.0")}
        os.environ["COUSIN_HOME"] = str(self.homes["wren"])
        self.ops = _FakeOps(self.root, self.repo,
                            {sha: v for v, sha in self.sha.items()},
                            self.sha["3.9.0"])
        self.ops.says = {"loops": {"pid": 101, "version": "3.9.0"},
                         "console": {"pid": 102, "version": "3.9.0"}}

    def plan(self, **kw):
        return super().plan(**kw)

    def switch(self, plan=None, **kw):
        kw.setdefault("deps", True)
        kw.setdefault("idle_timeout", 30)
        kw.setdefault("verify_timeout", 10)
        return upgrade_switch.run_switch(plan or self.plan(), self.ops, **kw)

    def state(self):
        return json.loads((self.root / upgrade_switch.STATE).read_text())

    def statuses(self, result):
        return [(r["name"], r["status"]) for r in result["restarts"]]


class TestSwitchPlan(_Switch):

    def test_the_dry_run_shows_the_switch_the_order_and_the_rollback(self):
        plan = self.plan()
        out = upgrade.render(plan)
        self.assertEqual(plan["from"]["branch"], "work")
        self.assertEqual(plan["from"]["sha"], self.sha["3.9.0"])
        self.assertEqual(plan["switch"]["verify"], "version")
        self.assertTrue(plan["switch"]["self_restart"])
        self.assertIn("git checkout --detach v3.11.0 (%s)"
                      % self.sha["3.11.0"][:12], out)
        self.assertIn("pip install --quiet --no-deps -e %s" % self.repo, out)
        self.assertIn("[refused now: dependencies changed, --deps installs"
                      " them]", out)
        self.assertIn("rollback: cousin-upgrade --switch --to %s --deps --yes"
                      % self.sha["3.9.0"][:12], out)
        self.assertIn("git -C %s checkout work" % self.repo, out)
        self.assertIn("each checked by the release it comes back on", out)
        self.assertRegex(out, r"4\. runner:wren .*the caller's own: last,"
                              r" detached after this call")

    def test_a_target_that_does_not_say_its_version_is_checked_by_pid(self):
        plan = self.plan(to="v3.10.0")
        self.assertEqual(plan["switch"], {"python": upgrade.sys.executable,
                                          "verify": "pid",
                                          "self_restart": False})
        out = upgrade.render(plan)
        self.assertIn("each checked by its new pid only", out)
        self.assertIn("the caller's own: by hand", out)

    def test_the_plan_still_writes_nothing(self):
        before = _snapshot(self.top)
        upgrade.render(self.plan())
        self.assertEqual(_snapshot(self.top), before)


class TestPreflight(_Switch):

    def test_tracked_changes_are_refused(self):
        (self.repo / "CHANGELOG.md").write_text("mine\n")
        why = upgrade_switch.preflight(self.plan(), self.ops, deps=True)
        self.assertIn("tracked changes (CHANGELOG.md)", why)
        self.assertEqual(self.ops.calls, [])

    def test_changed_dependencies_need_deps(self):
        why = upgrade_switch.preflight(self.plan(), self.ops)
        self.assertIn("the dependencies changed", why)
        self.assertIsNone(upgrade_switch.preflight(self.plan(), self.ops,
                                                   deps=True))

    def test_a_venv_that_imports_another_checkout_is_refused(self):
        self.ops.probe_checkout = "/elsewhere/cousins-framework"
        why = upgrade_switch.preflight(self.plan(), self.ops, deps=True)
        self.assertIn("imports cousin_lib from /elsewhere", why)


class TestSwitch(_Switch):

    def test_checkout_install_check_then_restarts_in_order(self):
        result = self.switch()
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual([(s["step"], s["status"]) for s in result["steps"]],
                         [("checkout", "done"), ("install", "done"),
                          ("check", "done")])
        self.assertIn(("git", "checkout", "--quiet", "--detach",
                       self.sha["3.11.0"]), self.ops.calls)
        self.assertIn(("pip", True), self.ops.calls)
        self.assertEqual(self.ops.restarts(), [
            ("stop", "loops"), ("start", "loops"),
            ("stop", "console"), ("start", "console"),
            ("stop", "runner:toki"), ("start", "runner:toki")])
        self.assertEqual(self.statuses(result), [
            ("loops", "done"), ("console", "done"), ("runner:toki", "done"),
            ("runner:wren", "detached")])
        self.assertIn("runs 3.11.0", result["restarts"][0]["detail"])
        self.assertIn("the branch work stays at", result["steps"][0]["detail"])

    def test_the_callers_runner_restarts_detached_with_its_environment(self):
        self.switch()
        [(argv, env, unit)] = self.ops.detached
        self.assertEqual(argv[:3], ["/opt/venv/bin/python3", "-m",
                                    "cousin_lib.upgrade"])
        self.assertEqual(argv[3:7], ["--restart", "--only", "runner:wren",
                                     "--yes"])
        self.assertIn(str(self.root), argv)
        self.assertTrue(all(os.path.isabs(a) for a in argv
                            if a.startswith("/") or "/" in a))
        self.assertEqual(env["FRAMEWORK_ROOT"], str(self.root))
        self.assertTrue(env["PATH"].startswith("/opt/venv/bin" + os.pathsep))
        self.assertEqual(env["COUSIN_HOME"], "")
        self.assertEqual(unit, "cousin-upgrade-restart-wren")
        self.assertNotIn(("stop", "runner:wren"), self.ops.calls)

    def test_the_from_ref_and_each_restart_are_recorded(self):
        self.switch()
        state = self.state()
        self.assertEqual(state["from"], {"version": "3.9.0", "ref": "v3.9.0",
                                         "sha": self.sha["3.9.0"],
                                         "branch": "work"})
        self.assertEqual(state["to"]["sha"], self.sha["3.11.0"])
        self.assertEqual(state["stage"], "done")
        self.assertEqual(state["restarts"]["runner:toki"]["status"], "done")
        self.assertEqual(state["restarts"]["runner:wren"]["status"],
                         "detached")
        self.assertIn("git -C %s checkout work" % self.repo,
                      state["rollback"]["by_hand"])

    def test_a_runner_mid_turn_is_waited_for(self):
        self.ops.turns["toki"] = ["running", "running", "idle"]
        result = self.switch()
        self.assertEqual(result["outcome"], "done")
        self.assertIn(("start", "runner:toki"), self.ops.calls)
        self.assertEqual(self.ops.clock, 2.0)

    def test_a_runner_still_busy_is_left_pending_and_recorded(self):
        self.ops.turns["toki"] = ["waiting_permission"]
        result = self.switch(idle_timeout=5)
        self.assertEqual(result["outcome"], "left")
        self.assertEqual(self.statuses(result)[2], ("runner:toki", "pending"))
        self.assertIn("mid-turn (waiting_permission)",
                      result["restarts"][2]["detail"])
        self.assertNotIn(("stop", "runner:toki"), self.ops.calls)
        self.assertEqual(self.statuses(result)[3], ("runner:wren", "detached"))
        self.assertEqual(self.state()["restarts"]["runner:toki"]["status"],
                         "pending")
        # later, once idle: --restart finishes it and leaves the rest
        self.ops.turns["toki"] = ["idle"]
        os.environ["COUSIN_HOME"] = ""
        order = upgrade.restart_plan(self.root)["order"]
        again = upgrade_switch.run_restarts(self.root, self.repo, order,
                                            self.ops, verify_timeout=10)
        self.assertEqual(again["outcome"], "done", again)
        self.assertEqual([(r["name"], r["status"]) for r in again["restarts"]],
                         [("loops", "already"), ("console", "already"),
                          ("runner:toki", "done"), ("runner:wren", "done")])

    def test_the_first_failure_stops_the_run_and_says_how_to_roll_back(self):
        self.ops.wrong["console"] = "3.9.0"
        result = self.switch()
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(self.statuses(result), [
            ("loops", "done"), ("console", "failed"),
            ("runner:toki", "not reached"), ("runner:wren", "not reached")])
        self.assertIn("came back on version 3.9.0, not 3.11.0",
                      result["restarts"][1]["detail"])
        self.assertNotIn(("stop", "runner:toki"), self.ops.calls)
        self.assertEqual(self.ops.detached, [])
        out = upgrade_switch.render_result(self.plan(), result)
        self.assertIn("FAILED. The rest was not done. To roll back:", out)
        self.assertIn("cousin-upgrade --switch --to %s --deps --yes"
                      % self.sha["3.9.0"][:12], out)
        self.assertIn("git -C %s checkout work" % self.repo, out)
        self.assertEqual(self.state()["stage"], "failed")

    def test_a_process_that_does_not_come_back_fails_in_time(self):
        self.ops.silent.add("loops")
        result = self.switch(verify_timeout=4)
        self.assertEqual(self.statuses(result)[0], ("loops", "failed"))
        self.assertIn("not back on 3.11.0 within 4s (pid 201 has not said"
                      " which release it runs)", result["restarts"][0]["detail"])
        self.ops.down.add("console")
        self.ops.silent.clear()
        result = self.switch()
        self.assertEqual(self.statuses(result)[1], ("console", "failed"))
        self.assertIn("cousin-supervisor start --name console",
                      result["restarts"][1]["detail"])

    def test_a_held_runner_is_left_and_one_we_stopped_is_started(self):
        self.ops.children["runner:toki"].update(state="stopped", pid=None,
                                                reason="stopped by request")
        result = self.switch()
        self.assertEqual(self.statuses(result)[2], ("runner:toki", "skipped"))
        self.assertNotIn(("start", "runner:toki"), self.ops.calls)
        state = self.state()
        state["restarts"]["runner:toki"] = {"status": "stopping"}
        upgrade_switch.write_state(self.root, state)
        result = self.switch(plan=self.plan())
        self.assertEqual(self.statuses(result)[2], ("runner:toki", "done"))

    def test_without_a_supervisor_the_restarts_are_by_hand(self):
        self.ops.no_supervisor = True
        result = self.switch()
        self.assertEqual(result["outcome"], "left")
        self.assertEqual([r["status"] for r in result["restarts"]],
                         ["by hand", "by hand", "by hand", "detached"])

    def test_a_process_already_on_the_target_is_not_restarted(self):
        self.ops.head_sha = self.sha["3.11.0"]
        self.ops.says["loops"] = {"pid": 101, "version": "3.11.0",
                                  "commit": self.sha["3.11.0"][:7],
                                  "checkout": str(self.repo)}
        self.switch()
        self.assertNotIn(("stop", "loops"), self.ops.calls)
        self.assertIn(("stop", "console"), self.ops.calls)

    def test_a_failed_checkout_moves_nothing_and_installs_nothing(self):
        self.ops.git_rc["checkout"] = 1
        result = self.switch()
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["steps"][0]["status"], "failed")
        self.assertIn("nothing moved", result["steps"][0]["detail"])
        self.assertNotIn(("pip", True), self.ops.calls)
        self.assertEqual(self.ops.restarts(), [])
        self.assertTrue(self.state()["stage"].startswith("failed: checkout"))

    def test_a_failed_install_or_check_stops_before_the_restarts(self):
        self.ops.pip_rc = 1
        result = self.switch()
        self.assertEqual(result["steps"][-1], {"step": "install",
                                               "status": "failed",
                                               "detail": "pip: pip said no"})
        self.assertEqual(self.ops.restarts(), [])
        self.ops.pip_rc = 0
        self.ops.head_sha = self.sha["3.9.0"]
        self.ops.probe_version = "3.9.0"
        result = self.switch()
        self.assertEqual(result["steps"][-1]["status"], "failed")
        self.assertIn("version 3.9.0, not 3.11.0",
                      result["steps"][-1]["detail"])
        self.assertEqual(self.ops.restarts(), [])

    def test_no_restart_moves_the_code_only(self):
        result = self.switch(restart=False)
        self.assertEqual(result["outcome"], "left")
        self.assertEqual(self.ops.restarts(), [])
        self.assertIn("--no-restart", self.state()["stage"])

    def test_a_target_without_announce_is_checked_by_its_new_pid(self):
        result = self.switch(plan=self.plan(to="v3.10.0"))
        self.assertEqual(self.statuses(result), [
            ("loops", "done"), ("console", "done"), ("runner:toki", "done"),
            ("runner:wren", "by hand")])
        self.assertIn("checked by its new pid only",
                      result["restarts"][0]["detail"])
        self.assertIn("cousin-supervisor stop wren",
                      result["restarts"][3]["detail"])
        self.assertEqual(self.ops.detached, [])


class TestDetachedSpawn(unittest.TestCase):
    """How the caller's own restart leaves the process tree: nothing is
    started here, subprocess is replaced."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = upgrade_switch.Ops(tmp.name, "/srv/checkout",
                                      python="/opt/venv/bin/python3")
        self.env = upgrade_switch.detached_env(self.ops)

    def test_a_user_unit_gets_every_variable_set_explicitly(self):
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(upgrade_switch.shutil, "which",
                               return_value="/usr/bin/systemd-run"), \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1"}), \
                mock.patch.object(upgrade_switch.subprocess, "run",
                                  return_value=done) as run, \
                mock.patch.object(upgrade_switch.subprocess, "Popen") as popen:
            how = self.ops.spawn_detached(["/opt/venv/bin/python3", "-m", "x"],
                                          self.env, "cousin-upgrade-restart-wren")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["systemd-run", "--user", "--unit",
                                   "cousin-upgrade-restart-wren"])
        self.assertIn("--setenv=FRAMEWORK_ROOT=%s" % self.ops.root, cmd)
        self.assertIn("--setenv=COUSIN_HOME=", cmd)
        self.assertTrue(any(c.startswith("--setenv=PATH=/opt/venv/bin" + os.pathsep)
                            for c in cmd))
        self.assertEqual(cmd[-3:], ["/opt/venv/bin/python3", "-m", "x"])
        popen.assert_not_called()
        self.assertIn("cousin-upgrade-restart-wren", how)

    def test_without_a_user_manager_it_is_a_new_session(self):
        with mock.patch.object(upgrade_switch.shutil, "which",
                               return_value=None), \
                mock.patch.object(upgrade_switch.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            how = self.ops.spawn_detached(["/opt/venv/bin/python3"], self.env,
                                          "u")
        kw = popen.call_args[1]
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["env"]["FRAMEWORK_ROOT"], str(self.ops.root))
        self.assertEqual(kw["env"]["COUSIN_HOME"], "")
        self.assertIn("process 4242", how)


class TestSwitchCLI(_Switch):

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(upgrade.sys, "stdin", stdin or _Pipe()), \
                mock.patch.object(upgrade_switch, "Ops",
                                  return_value=self.ops), \
                mock.patch.object(upgrade.version, "version",
                                  return_value="3.9.0"), \
                mock.patch.object(upgrade.version, "CHECKOUT", self.repo):
            code = upgrade.upgrade_main(["--root", str(self.root),
                                         "--checkout", str(self.repo),
                                         "--verify-timeout", "5", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_switch_asks_and_without_an_answer_does_nothing(self):
        code, out, err = self.run_cli("--switch", "--deps")
        self.assertEqual(code, 2)
        self.assertIn("pass --yes", err)
        self.assertIn("1. git checkout --detach v3.11.0", out)
        self.assertNotIn("checkout", [c[1] for c in self.ops.calls
                                      if c[0] == "git"])
        code, out, err = self.run_cli("--switch", "--deps",
                                      stdin=_Terminal("n\n"))
        self.assertEqual(code, 2)
        self.assertIn("and restart 4 processes? [y/N]", err)
        self.assertEqual(self.ops.restarts(), [])

    def test_switch_with_yes_fetches_switches_and_restarts(self):
        code, out, err = self.run_cli("--switch", "--deps", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.ops.calls[0], ("git", "fetch", "--tags",
                                             "--quiet"))
        self.assertIn("0. git fetch --tags: done", out)
        self.assertRegex(out, r"3\. runner:toki +done +pid \d+ runs 3\.11\.0")
        self.assertIn("note: 4 homes not in step with v3.11.0", out)
        self.assertIn("done; to roll back: cousin-upgrade --switch --to", out)

    def test_switch_refusals_are_exit_2(self):
        code, _out, err = self.run_cli("--switch", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("--deps", err)
        self.ops.git_rc["fetch"] = 128
        code, _out, err = self.run_cli("--switch", "--deps", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("--no-fetch", err)
        code, _out, _err = self.run_cli("--switch", "--deps", "--yes",
                                        "--no-fetch")
        self.assertEqual(code, 0, _out + _err)

    def test_a_failure_is_exit_1_and_a_busy_runner_too(self):
        self.ops.turns["toki"] = ["running"]
        code, out, err = self.run_cli("--switch", "--deps", "--yes",
                                      "--idle-timeout", "2")
        self.assertEqual(code, 1, out + err)
        self.assertIn("left for a person", out)

    def test_flags_that_go_with_another_mode(self):
        for argv, word in ((("--dry-run", "--no-fetch"), "--switch"),
                           (("--apply-homes", "--deps"), "--switch"),
                           (("--switch", "--only", "loops"), "--restart"),
                           (("--dry-run", "--yes"), "--yes goes with")):
            code, _out, err = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(word, err)

    def test_restart_only_one_process(self):
        os.environ["COUSIN_HOME"] = ""
        self.ops.head_sha = self.sha["3.11.0"]
        code, out, err = self.run_cli("--restart", "--only", "console",
                                      "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.ops.restarts(), [("stop", "console"),
                                               ("start", "console")])
        code, out, err = self.run_cli("--restart", "--only", "nope", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("--only nope: not in the restart order", err)


class TestTemplateSyncText(unittest.TestCase):
    """template_sync takes the shipped registry and template as text."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        self.home.mkdir(parents=True)
        self.reg = self.home / "mcp-registry.toml"
        patch = mock.patch.dict(template_sync._MIGRATIONS, MIGRATIONS)
        patch.start()
        self.addCleanup(patch.stop)

    def test_theirs_and_base_as_text(self):
        self.reg.write_text(OLD_REGISTRY)
        out = template_sync._registry_sync(self.home, None, apply=True,
                                           theirs=NEW_REGISTRY,
                                           base=OLD_REGISTRY)
        self.assertEqual(out["retired"], ["tools.legacy"])
        data = tomllib.loads(self.reg.read_text())
        self.assertEqual(data["tools"]["memory"]["description"],
                         "memory, as shipped now")
        self.assertEqual(
            data["tools"]["memory"]["properties"]["topic"],
            {"type": "string", "description": "the topic, retired or not"})
        self.assertIn("legacy", data["tools"])          # never pruned

    def test_a_migration_with_a_new_value_needs_theirs_to_ship_it(self):
        self.reg.write_text(OLD_REGISTRY)
        theirs = NEW_REGISTRY.replace('"the topic, retired or not"',
                                      '"something else"')
        out = template_sync._registry_sync(self.home, None, theirs=theirs)
        self.assertNotIn("tools.memory.properties.topic", out["corrected"])
        self.assertEqual(self.reg.read_text(), OLD_REGISTRY)

    def test_prune_removes_retired_tables_and_keys_only(self):
        base = OLD_REGISTRY.replace(
            'description = "memory, as shipped first"\n',
            'description = "memory, as shipped first"\nold = "gone"\n')
        self.reg.write_text(base.replace('argv = ["search", "{topic}"]\n',
                                         'argv = ["search", "{topic}"]\n'
                                         'mine = true\n'))
        out = template_sync._registry_sync(self.home, None, apply=True,
                                           theirs=NEW_REGISTRY, base=base,
                                           prune=True)
        self.assertEqual(out["pruned"], ["tools.legacy",
                                         "tools.memory.old"])
        data = tomllib.loads(self.reg.read_text())
        self.assertNotIn("legacy", data["tools"])
        self.assertNotIn("old", data["tools"]["memory"])
        self.assertTrue(data["tools"]["memory"]["commands"]["search"]
                        ["mine"])

    def test_no_registry_says_so(self):
        out = template_sync._registry_sync(self.home, None,
                                           theirs=NEW_REGISTRY)
        self.assertIsNone(out["path"])
        self.assertEqual(out["retired"], [])

    def test_plan_takes_the_template_as_text(self):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n')
        (self.home / "CLAUDE.md").write_text(
            "# Wren\n\n## Chat\n\nold\n\n%s\n" % MARKER)
        old, new, _ = template_sync.plan(
            self.home, self.home, template="# x\n\n## Chat\n\nnew for"
            " {{NAME}}\n\n%s\n" % MARKER)
        self.assertIn("new for Wren", new)
        self.assertNotIn("new for", old)


class TestPureHelpers(unittest.TestCase):

    def test_compare_templates_takes_texts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "law.md").write_text("old\n")
            rows = shared_tier.compare_templates(
                root, shipped={"law": "new\n", "rules": {}})
        self.assertEqual(rows[0]["status"], "differs")

    def test_refreshed_mcp_json_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "cousins" / "wren"
            home.mkdir(parents=True)
            (home / ".mcp.json").write_text('{"mcpServers": {}}\n')
            path, text, changed = mcp_server.refreshed_mcp_json(
                home, root=tmp, slug="wren")
            self.assertTrue(changed)
            self.assertEqual(path.read_text(), '{"mcpServers": {}}\n')
            self.assertIn('"cousin"', text)


if __name__ == "__main__":
    unittest.main()
