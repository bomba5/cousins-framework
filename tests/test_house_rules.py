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
        # Rendered as the boot packet renders them: they must leave at
        # least a quarter of the shared layer for the install's own
        # rules and the index, and fit whole even with the example on.
        budget = boot.LAYER_BUDGETS["shared"][1]
        with tempfile.TemporaryDirectory() as tmp:
            shared_tier.seed_house_rules(tmp)
            with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": tmp}):
                active = len(boot._shared())
                (pathlib.Path(tmp) / "shared" / EXAMPLE.name).write_text(EXAMPLE.read_text())
                with_example = len(boot._shared())
        self.assertLessEqual(active, budget * 3 // 4, "%d of %d" % (active, budget))
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

    def test_the_boot_packet_quotes_every_active_rule(self):
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)}):
            text = boot.assemble("wren", self.home)["text"]
        section = text[text.index("## 2. Shared Rules and Fleet Memory"):
                       text.index("## 3. Cousin Self-Portrait")]
        for name in EXPECTED:
            self.assertIn("### %s\n" % name[:-3], section)
        self.assertNotIn("truncated", section)


class TestTheDocs(unittest.TestCase):
    def test_the_page_names_every_shipped_file(self):
        page = (REPO / "docs" / "house-rules.md").read_text()
        for name in EXPECTED + [EXAMPLE.name]:
            self.assertIn(name[:-3], page)


if __name__ == "__main__":
    unittest.main()
