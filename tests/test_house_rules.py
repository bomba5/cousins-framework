"""House rules: the generic operator rules a fresh install ships with.

docs/house-rules.md is the specification: the active rules under
templates/shared/ are seeded into <root>/shared/ when the supervisor
starts, an existing file is never overwritten, a seeded file the
operator deleted stays deleted, and the Scrum example under
templates/shared/examples/ is never active until copied in.
"""
import contextlib
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from cousin_lib import boot, shared_tier, supervisor
from tests.runner.test_prompt import PromptCase

REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "templates" / "shared"
ACTIVE = sorted(p.name for p in TEMPLATES.glob("*.md"))
EXAMPLE = TEMPLATES / "examples" / "reference_scrum-team.md"
EXPECTED = ["reference_boundary-discards.md", "reference_first-principles.md",
            "reference_framework-semver.md", "reference_process-hygiene.md",
            "reference_requirement-levels-rfc2119.md", "reference_state-hygiene.md",
            "reference_verify-it-fires.md"]


class RootCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)})
        p.start()
        self.addCleanup(p.stop)

    def canonical(self):
        return sorted(p.name for p in (self.root / "shared").glob("*.md"))

    def audit(self):
        path = self.root / "shared" / "audit.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class TestTheShippedFiles(unittest.TestCase):
    def test_the_active_rules_are_the_seven_house_rules(self):
        self.assertEqual(ACTIVE, EXPECTED)

    def test_every_file_is_a_rule_with_the_source_frontmatter(self):
        for path in [TEMPLATES / n for n in ACTIVE] + [EXAMPLE]:
            fields, body = boot._frontmatter(path.read_text())
            self.assertEqual(fields.get("name"), path.stem, path.name)
            self.assertTrue(fields.get("description"), path.name)
            self.assertEqual(fields.get("shareable"), "true", path.name)
            self.assertEqual(fields.get("kind"), "rule", path.name)
            self.assertTrue(body.strip(), path.name)

    def test_plain_ascii_no_em_dash(self):
        for path in [TEMPLATES / n for n in ACTIVE] + [EXAMPLE]:
            text = path.read_text()
            self.assertTrue(text.isascii(), "%s is not plain ASCII" % path.name)

    def test_the_active_rules_fit_the_shared_layer_with_room(self):
        # The rules and the index, measured against the shared layer's
        # budget: they must leave at least a sixth of it for the
        # install's own rules and the index, and fit whole even with the
        # example on.
        budget = boot.LAYER_BUDGETS["shared"][1]

        def size(root):
            rules, index = boot.shared_parts(root)
            return len("\n\n".join(rules + index))
        with tempfile.TemporaryDirectory() as tmp:
            shared_tier.seed_house_rules(tmp)
            active = size(tmp)
            (pathlib.Path(tmp) / "shared" / EXAMPLE.name).write_text(EXAMPLE.read_text())
            with_example = size(tmp)
        self.assertLessEqual(active, budget * 5 // 6, "%d of %d" % (active, budget))
        self.assertLess(with_example, budget, "%d of %d" % (with_example, budget))


class TestSeeding(RootCase):
    def test_a_fresh_root_gets_exactly_the_active_rules(self):
        written = shared_tier.seed_house_rules(self.root)
        self.assertEqual(sorted(written), EXPECTED)
        self.assertEqual(self.canonical(), EXPECTED)
        for name in EXPECTED:
            self.assertEqual((self.root / "shared" / name).read_text(),
                             (TEMPLATES / name).read_text())
        self.assertFalse((self.root / "shared" / "proposed").exists())

    def test_the_scrum_example_is_not_active(self):
        shared_tier.seed_house_rules(self.root)
        self.assertNotIn(EXAMPLE.name, self.canonical())
        self.assertFalse((self.root / "shared" / "examples").exists())
        rules, _index = boot.shared_parts(self.root)
        self.assertFalse(any("Scrum" in r for r in rules))

    def test_each_seed_is_an_audit_row(self):
        shared_tier.seed_house_rules(self.root)
        rows = [r for r in self.audit() if r["kind"] == "seed"]
        self.assertEqual(sorted(r["file"] for r in rows), EXPECTED)
        self.assertTrue(all(r["action"] == "written" for r in rows))

    def test_an_existing_file_is_never_overwritten(self):
        (self.root / "shared").mkdir()
        mine = self.root / "shared" / "reference_first-principles.md"
        mine.write_text("---\nkind: rule\n---\nThe operator's own wording.\n")
        written = shared_tier.seed_house_rules(self.root)
        self.assertNotIn(mine.name, written)
        self.assertEqual(mine.read_text(), "---\nkind: rule\n---\nThe operator's own wording.\n")
        self.assertEqual(self.canonical(), EXPECTED)
        kept = [r for r in self.audit() if r["file"] == mine.name]
        self.assertEqual([r["action"] for r in kept], ["kept"])

    def test_a_deleted_rule_stays_deleted(self):
        shared_tier.seed_house_rules(self.root)
        (self.root / "shared" / "reference_process-hygiene.md").unlink()
        self.assertEqual(shared_tier.seed_house_rules(self.root), [])
        self.assertNotIn("reference_process-hygiene.md", self.canonical())

    def test_a_kept_file_deleted_later_stays_deleted(self):
        (self.root / "shared").mkdir()
        mine = self.root / "shared" / "reference_state-hygiene.md"
        mine.write_text("mine\n")
        shared_tier.seed_house_rules(self.root)
        mine.unlink()
        shared_tier.seed_house_rules(self.root)
        self.assertFalse(mine.exists())

    def test_a_rule_a_later_release_adds_is_seeded_once(self):
        source = self.root / "src"
        source.mkdir()
        (source / "a.md").write_text("---\nkind: rule\n---\nA.\n")
        self.assertEqual(shared_tier.seed_house_rules(self.root, source=source), ["a.md"])
        (source / "b.md").write_text("---\nkind: rule\n---\nB.\n")
        self.assertEqual(shared_tier.seed_house_rules(self.root, source=source), ["b.md"])
        self.assertEqual(shared_tier.seed_house_rules(self.root, source=source), [])

    def test_seeded_rules_are_canonical_with_nothing_to_review(self):
        shared_tier.seed_house_rules(self.root)
        state = shared_tier.list_shared()
        self.assertEqual(state["canonical"], EXPECTED)
        self.assertEqual(state["pending"], [])


class TestTheSupervisorSeeds(RootCase):
    def test_run_seeds_before_it_serves(self):
        seen = []

        def serve(sup):
            seen.append(self.canonical())
            return 0

        with mock.patch.object(supervisor.Supervisor, "serve", serve), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = supervisor.supervisor_main(["run", "--root", str(self.root),
                                               "--no-console", "--no-loops"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [EXPECTED])
        self.assertIn("seeded house rules", out.getvalue())

    def test_a_seeding_failure_does_not_stop_the_start(self):
        with mock.patch.object(supervisor.Supervisor, "serve", lambda sup: 0), \
                mock.patch.object(shared_tier, "seed_house_rules", side_effect=OSError("ro")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = supervisor.supervisor_main(["run", "--root", str(self.root),
                                               "--no-console", "--no-loops"])
        self.assertEqual(code, 0)
        self.assertIn("house rules not seeded: ro", err.getvalue())


class TestAFreshCousinCarriesThem(PromptCase):
    def setUp(self):
        super().setUp()
        for stray in ("rule_short.md", "ref_map.md"):
            (self.root / "shared" / stray).unlink()
        shared_tier.seed_house_rules(self.root)

    def test_the_system_prompt_quotes_every_active_rule(self):
        text = self.compose()
        self.assertIn("# Operator rules every cousin follows", text)
        for name in EXPECTED:
            _fields, body = boot._frontmatter((TEMPLATES / name).read_text())
            self.assertIn("### %s\n%s" % (name[:-3], body.strip()), text)
        self.assertNotIn("Scrum", text)


class TestTheLaw(RootCase):
    """The Framework Law ships too (templates/law.md) and is seeded once
    into config/law.md, by the same rule as the house rules."""

    def law(self):
        return self.root / "config" / "law.md"

    def test_a_fresh_root_gets_the_shipped_law(self):
        self.assertTrue(shared_tier.seed_law(self.root))
        self.assertEqual(self.law().read_text(), shared_tier.LAW_TEMPLATE.read_text())
        rows = [r for r in self.audit() if r.get("file") == "config/law.md"]
        self.assertEqual([r["kind"] for r in rows], ["seed"])

    def test_an_existing_law_is_never_overwritten(self):
        self.law().write_text("# my own law\n")
        self.assertFalse(shared_tier.seed_law(self.root))
        self.assertEqual(self.law().read_text(), "# my own law\n")

    def test_a_deleted_law_stays_deleted(self):
        shared_tier.seed_law(self.root)
        self.law().unlink()
        self.assertFalse(shared_tier.seed_law(self.root))
        self.assertFalse(self.law().exists())

    def test_the_shipped_law_is_generic_ascii_and_short(self):
        text = shared_tier.LAW_TEMPLATE.read_text()
        self.assertTrue(text.isascii())
        self.assertNotIn("\u2014", text)
        self.assertLess(len(text), 6000)
        for word in ("--confirm", "Juno", "2026-"):
            self.assertNotIn(word, text)

    def test_the_boot_reads_the_seeded_law(self):
        shared_tier.seed_law(self.root)
        self.assertIn("Framework Law", boot.law_text(self.root))

    def test_the_supervisor_seeds_the_law(self):
        with mock.patch.object(supervisor.Supervisor, "serve", lambda sup: 0), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = supervisor.supervisor_main(["run", "--root", str(self.root),
                                               "--no-console", "--no-loops"])
        self.assertEqual(code, 0)
        self.assertTrue(self.law().exists())
        self.assertIn("seeded the Framework Law", out.getvalue())


class TestTheTemplateComparison(RootCase):
    """An upgrade never touches a seeded file; compare_templates and
    `cousin-shared templates` say where the install and what ships part,
    and write nothing."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ship = pathlib.Path(tmp.name)
        (self.ship / "shared").mkdir()
        (self.ship / "law.md").write_text("# Law\n\nOne.\nTwo.\n")
        for name in ("a_same.md", "b_edited.md", "c_missing.md"):
            (self.ship / "shared" / name).write_text("rule %s\n" % name)
        shared_tier.seed_law(self.root, source=self.ship / "law.md")
        shared_tier.seed_house_rules(self.root, source=self.ship / "shared")
        (self.root / "config" / "law.md").write_text("# Law\n\nOne.\nThree.\n")
        (self.root / "shared" / "b_edited.md").write_text("rule mine\n")
        (self.root / "shared" / "c_missing.md").unlink()
        (self.root / "shared" / "mine.md").write_text("my own rule\n")

    def compare(self):
        return shared_tier.compare_templates(
            self.root, law_source=self.ship / "law.md",
            rules_source=self.ship / "shared")

    def snapshot(self):
        return {p: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def test_each_seeded_file_has_its_status(self):
        rows = {r["path"]: r["status"] for r in self.compare()}
        self.assertEqual(rows, {
            "config/law.md": "differs",
            "shared/a_same.md": "same",
            "shared/b_edited.md": "differs",
            "shared/c_missing.md": "missing in install",
        })

    def test_the_law_comes_first(self):
        self.assertEqual(self.compare()[0]["path"], "config/law.md")

    def test_a_difference_is_a_unified_diff_from_the_install_to_what_ships(self):
        rows = {r["path"]: r["diff"] for r in self.compare()}
        self.assertEqual(rows["config/law.md"], (
            "--- config/law.md (install)\n"
            "+++ config/law.md (shipped)\n"
            "@@ -1,4 +1,4 @@\n"
            " # Law\n"
            " \n"
            " One.\n"
            "-Three.\n"
            "+Two.\n"))
        self.assertIn("-rule mine\n+rule b_edited.md\n", rows["shared/b_edited.md"])
        self.assertEqual(rows["shared/a_same.md"], "")
        self.assertEqual(rows["shared/c_missing.md"], "")

    def test_a_missing_final_newline_is_marked(self):
        (self.root / "shared" / "b_edited.md").write_text("rule mine")
        diff = {r["path"]: r["diff"] for r in self.compare()}["shared/b_edited.md"]
        self.assertIn("-rule mine\n\\ No newline at end of file\n+rule b_edited.md\n",
                      diff)

    def test_a_seeded_rule_no_longer_shipped_is_named(self):
        (self.ship / "shared" / "a_same.md").unlink()
        rows = {r["path"]: r["status"] for r in self.compare()}
        self.assertEqual(rows["shared/a_same.md"], "not shipped any more")

    def test_nothing_is_written(self):
        before = self.snapshot()
        self.compare()
        with mock.patch.object(shared_tier, "LAW_TEMPLATE", self.ship / "law.md"), \
                mock.patch.object(shared_tier, "HOUSE_RULES", self.ship / "shared"), \
                contextlib.redirect_stdout(io.StringIO()):
            shared_tier.shared_main(["templates", "--full"])
        self.assertEqual(self.snapshot(), before)

    def run_cli(self, *argv):
        with mock.patch.object(shared_tier, "LAW_TEMPLATE", self.ship / "law.md"), \
                mock.patch.object(shared_tier, "HOUSE_RULES", self.ship / "shared"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = shared_tier.shared_main(["templates", *argv])
        return code, out.getvalue()

    def test_the_cli_prints_a_line_per_file_and_exits_1_on_a_difference(self):
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            "differs                config/law.md",
            "same                   shared/a_same.md",
            "differs                shared/b_edited.md",
            "missing in install     shared/c_missing.md",
        ])

    def test_the_cli_prints_the_diff_with_full(self):
        code, out = self.run_cli("--full")
        self.assertEqual(code, 1)
        self.assertIn("differs                config/law.md\n"
                      "--- config/law.md (install)\n", out)
        self.assertIn("-Three.\n+Two.\n", out)
        self.assertNotIn("---", self.run_cli()[1])

    def test_the_cli_exits_0_when_everything_matches(self):
        shutil.copy(self.ship / "law.md", self.root / "config" / "law.md")
        for name in ("b_edited.md", "c_missing.md"):
            shutil.copy(self.ship / "shared" / name, self.root / "shared" / name)
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual({l.split()[0] for l in out.splitlines()}, {"same"})


class TestTheDocs(unittest.TestCase):
    def test_the_page_names_every_shipped_file(self):
        page = (REPO / "docs" / "house-rules.md").read_text()
        for name in EXPECTED + [EXAMPLE.name]:
            self.assertIn(name[:-3], page)


if __name__ == "__main__":
    unittest.main()
