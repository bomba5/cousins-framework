"""cousin-upgrade's plan (cousin_lib/upgrade.py): every text from git,
each home planned by template_sync's structural sync, nothing written."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

from cousin_lib import mcp_server, shared_tier, template_sync, upgrade

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


def _release(repo, version, *, registry, chat, law, rule, deps, tag=True):
    files = {"pyproject.toml": _pyproject(version, deps),
             upgrade.REGISTRY_EXAMPLE: registry,
             upgrade.TEMPLATE: _template(chat),
             upgrade.LAW: law,
             upgrade.RULES + "/reference_wren-rule.md": rule,
             upgrade.RULES + "/examples/reference_not-seeded.md": "x\n",
             "CHANGELOG.md": CHANGELOG}
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
            json.dumps({"registry_to": "v3.10.0", "at": 1}))
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
        self.assertEqual(toki["from"], {"ref": "v3.10.0",
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

    def test_without_dry_run_it_refuses(self):
        code, out, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("apply path", err)

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
