"""perimeter: the shapes a write may not take, the writers that check
them, and the tool chokepoint that turns one into a deny."""
import inspect
import unittest

from cousin_lib import (boot, distill, memory, memory_trash, perimeter,
                        raw_fold, reinforce, self_portrait, shared_tier)
from tests._hermetic import HermeticCase


class TestShapes(unittest.TestCase):
    def test_the_law_is_protected_however_it_is_spelled(self):
        for path in ("/home/bomba/cf/config/law.md",
                     "config/law.md", "./config/law.md",
                     "/srv/framework/config/law.md"):
            self.assertIsNotNone(perimeter.protected_reason(path), path)

    def test_the_shipped_template_is_not_the_installed_law(self):
        # The seed reads templates/law.md on every install; protecting it
        # would refuse the install itself.
        self.assertIsNone(perimeter.protected_reason(
            "/home/bomba/cf/templates/law.md"))

    def test_a_committed_portrait_is_protected_and_its_candidate_is_not(self):
        self.assertIsNotNone(perimeter.protected_reason(
            "/home/bomba/cf/cousins/chico/self-portrait.md"))
        self.assertIsNone(perimeter.protected_reason(
            "/home/bomba/cf/cousins/chico/.self-portrait-candidate.md"))
        self.assertIsNone(perimeter.protected_reason(
            "/home/bomba/cf/cousins/chico/.self-portrait.md.bak"))

    def test_canonical_shared_memory_is_protected(self):
        self.assertIsNotNone(perimeter.protected_reason(
            "/home/bomba/cf/shared/reference_operator-style.md"))
        self.assertIsNotNone(perimeter.protected_reason("shared/rules.md"))

    def test_the_proposal_path_stays_open(self):
        # propose() is the tier's single agent-side entry path; protecting
        # it would refuse every cousin's share of the fleet's memory.
        self.assertIsNone(perimeter.protected_reason(
            "/home/bomba/cf/shared/proposed/chico__reference_h.md"))
        self.assertIsNone(perimeter.protected_reason(
            "/home/bomba/cf/shared/examples/scratch.md"))
        self.assertIsNone(perimeter.protected_reason(
            "/home/bomba/cf/shared/audit.jsonl"))

    def test_ordinary_memory_is_not_protected(self):
        for path in ("memory/raw/2026-10-02.jsonl",
                     "memory/distilled/notes.md",
                     "/home/bomba/cf/cousins/chico/memory/raw/2026-10-02.jsonl",
                     "/home/bomba/cf/data/decision.jsonl",
                     "/home/bomba/cf/config/policy.toml",
                     "/home/bomba/cf/cousins/chico/notes/plan.md"):
            self.assertIsNone(perimeter.protected_reason(path), path)

    def test_a_path_below_a_shared_subdirectory_is_not_canonical(self):
        # Only the file directly in shared/ is canonical; a nested one
        # belongs to the subdirectory's own surface.
        self.assertIsNone(perimeter.protected_reason(
            "/home/bomba/cf/shared/proposed/nested/deep.md"))

    def test_an_empty_or_absurd_path_is_not_protected(self):
        for path in ("", "   ", None, "/"):
            self.assertIsNone(perimeter.protected_reason(path))


class TestAssertWritable(unittest.TestCase):
    def test_the_writer_is_told_which_one_refused(self):
        with self.assertRaises(perimeter.PerimeterRefused) as caught:
            perimeter.assert_writable("/srv/config/law.md",
                                      writer="memory._append_raw")
        self.assertIn("memory._append_raw", str(caught.exception))
        self.assertIn("config/law.md is the Framework Law", str(caught.exception))

    def test_a_permitted_path_comes_back_for_the_caller_to_use(self):
        path = "/home/bomba/cf/memory/raw/2026-10-02.jsonl"
        self.assertEqual(perimeter.assert_writable(path), path)

    def test_the_refusal_is_an_exception_a_caller_can_catch_by_name(self):
        self.assertTrue(issubclass(perimeter.PerimeterRefused, Exception))


class TestToolPaths(unittest.TestCase):
    def test_the_path_tools_name_their_path(self):
        for tool, field in (("Write", "file_path"), ("Edit", "file_path"),
                            ("MultiEdit", "file_path"),
                            ("NotebookEdit", "notebook_path")):
            out = perimeter.tool_paths(tool, {field: "/tmp/x.md"})
            self.assertEqual(out, ["/tmp/x.md"], tool)

    def test_bash_is_tokenised(self):
        out = perimeter.tool_paths("Bash", {"command": "sed -i s/a/b/ /srv/config/law.md"})
        self.assertIn("/srv/config/law.md", out)

    def test_a_malformed_command_does_not_raise(self):
        out = perimeter.tool_paths("Bash", {"command": 'echo "unclosed'})
        self.assertIsInstance(out, list)

    def test_a_tool_with_no_path_field_yields_nothing(self):
        self.assertEqual(perimeter.tool_paths("Read", {"file_path": ""}), [])
        self.assertEqual(perimeter.tool_paths("mcp__cousin__reply", None), [])


class TestCheckTool(unittest.TestCase):
    def test_a_write_to_the_law_is_refused_and_says_which_tool(self):
        reason = perimeter.check_tool("Write", {"file_path": "/srv/config/law.md"})
        self.assertIn("Write may not write", reason)
        self.assertIn("memory perimeter", reason)

    def test_a_bash_command_that_names_the_law_is_refused(self):
        self.assertIsNotNone(perimeter.check_tool(
            "Bash", {"command": "echo x >> ~/cf/config/law.md"}))
        self.assertIsNotNone(perimeter.check_tool(
            "Bash", {"command": "L=/srv/config/law.md; rm $L"}))

    def test_an_ordinary_write_is_not(self):
        self.assertIsNone(perimeter.check_tool(
            "Write", {"file_path": "/home/bomba/cf/cousins/chico/notes/a.md"}))
        self.assertIsNone(perimeter.check_tool(
            "Bash", {"command": "git status"}))

    def test_the_refusal_carries_one_path_not_a_list(self):
        reason = perimeter.check_tool(
            "Bash", {"command": "rm /srv/config/law.md /srv/self-portrait.md"})
        self.assertEqual(reason.count("may not write"), 1)


# The writer list is a test, not a docstring: a new memory writer that
# skips assert_writable shows up here as a diff the reviewer has to
# answer for. `mark_obsolete` and `record_event` are deliberately absent
# - both reach disk through memory._append_raw, which is checked.
WRITERS = (
    (memory, "_append_raw"),
    (distill, "_distill"),
    (raw_fold, "_fold_month"),
    (reinforce, "record"),
    (memory_trash, "_rewrite"),
    (self_portrait, "write_candidate_text"),
    (shared_tier, "propose"),
)


class TestWriterList(HermeticCase):
    def test_every_writer_checks_the_perimeter_before_it_writes(self):
        for module, name in WRITERS:
            source = inspect.getsource(getattr(module, name))
            self.assertIn("perimeter.assert_writable", source,
                          "%s.%s writes without checking the perimeter"
                          % (module.__name__.split(".")[-1], name))

    def test_the_list_is_not_empty_and_not_the_whole_framework(self):
        self.assertGreaterEqual(len(WRITERS), 5)


class TestTheLawSurvivesFit(unittest.TestCase):
    def test_a_hard_layer_is_never_cut_to_its_maximum(self):
        law = "rule. " * 9000
        out = boot.fit({"law": law, "memories": "y" * 9000},
                       dict(boot.LAYER_BUDGETS), boot.TRUNCATE_ORDER, 10_000)
        self.assertEqual(out["law"], law)
        self.assertIn("truncated", out["memories"])

    def test_a_hard_layer_is_never_cut_by_the_overflow_loop_either(self):
        law = "rule. " * 9000
        out = boot.fit({"law": law, "a": "y" * 200},
                       dict(boot.LAYER_BUDGETS), boot.TRUNCATE_ORDER, 100)
        self.assertEqual(out["law"], law)

    def test_the_overflow_is_reported_rather_than_hidden(self):
        law = "rule. " * 9000
        out = boot.fit({"law": law}, dict(boot.LAYER_BUDGETS),
                       boot.TRUNCATE_ORDER, 1000)
        self.assertEqual(boot.hard_overflow(out, 1000), [("law", len(law))])
        self.assertEqual(boot.hard_overflow({"law": "short"}, 1000), [])

    def test_the_law_is_the_only_hard_layer_today(self):
        self.assertEqual(tuple(boot.HARD_LAYERS), ("law",))
        self.assertNotIn("law", boot.TRUNCATE_ORDER)


if __name__ == "__main__":
    unittest.main()