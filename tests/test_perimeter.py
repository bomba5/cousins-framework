"""perimeter: the shapes a write may not take, the writers that check
them, and the tool chokepoint that turns one into a deny."""
import inspect
import os
import unittest
from unittest import mock

from cousin_lib import (boot, distill, memory, memory_trash, perimeter,
                        raw_fold, reinforce, self_portrait, shared_tier)
from tests._hermetic import HermeticCase


class TestShapes(unittest.TestCase):
    def test_the_law_is_protected_however_it_is_spelled(self):
        for path in ("/srv/cf/config/law.md",
                     "config/law.md", "./config/law.md",
                     "/srv/framework/config/law.md"):
            self.assertIsNotNone(perimeter.protected_reason(path), path)

    def test_the_shipped_template_is_not_the_installed_law(self):
        # The seed reads templates/law.md on every install; protecting it
        # would refuse the install itself.
        self.assertIsNone(perimeter.protected_reason(
            "/srv/cf/templates/law.md"))

    def test_a_committed_portrait_is_protected_and_its_candidate_is_not(self):
        self.assertIsNotNone(perimeter.protected_reason(
            "/srv/cf/cousins/chico/self-portrait.md"))
        self.assertIsNone(perimeter.protected_reason(
            "/srv/cf/cousins/chico/.self-portrait-candidate.md"))
        self.assertIsNone(perimeter.protected_reason(
            "/srv/cf/cousins/chico/.self-portrait.md.bak"))

    def test_canonical_shared_memory_is_protected(self):
        self.assertIsNotNone(perimeter.protected_reason(
            "/srv/cf/shared/reference_operator-style.md"))
        self.assertIsNotNone(perimeter.protected_reason("shared/rules.md"))

    def test_the_proposal_path_stays_open(self):
        # propose() is the tier's single agent-side entry path; protecting
        # it would refuse every cousin's share of the fleet's memory.
        self.assertIsNone(perimeter.protected_reason(
            "/srv/cf/shared/proposed/chico__reference_h.md"))
        self.assertIsNone(perimeter.protected_reason(
            "/srv/cf/shared/examples/scratch.md"))
        self.assertIsNone(perimeter.protected_reason(
            "/srv/cf/shared/audit.jsonl"))

    def test_ordinary_memory_is_not_protected(self):
        for path in ("memory/raw/2026-10-02.jsonl",
                     "memory/distilled/notes.md",
                     "/srv/cf/cousins/chico/memory/raw/2026-10-02.jsonl",
                     "/srv/cf/data/decision.jsonl",
                     "/srv/cf/config/policy.toml",
                     "/srv/cf/cousins/chico/notes/plan.md"):
            self.assertIsNone(perimeter.protected_reason(path), path)

    def test_a_path_below_a_shared_subdirectory_is_not_canonical(self):
        # Only the file directly in shared/ is canonical; a nested one
        # belongs to the subdirectory's own surface.
        self.assertIsNone(perimeter.protected_reason(
            "/srv/cf/shared/proposed/nested/deep.md"))

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
        path = "/srv/cf/memory/raw/2026-10-02.jsonl"
        self.assertEqual(perimeter.assert_writable(path), path)

    def test_the_refusal_is_an_exception_a_caller_can_catch_by_name(self):
        self.assertTrue(issubclass(perimeter.PerimeterRefused, Exception))


class TestWriteTargets(unittest.TestCase):
    def test_the_path_tools_name_the_path_they_write(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            out = perimeter.write_targets(tool, {"file_path": "/tmp/x.md"})
            self.assertEqual(out, ["/tmp/x.md"], tool)
        self.assertEqual(
            perimeter.write_targets("NotebookEdit", {"notebook_path": "/tmp/x.ipynb"}),
            ["/tmp/x.ipynb"])

    def test_a_read_tool_names_no_target_at_all(self):
        # The defect this exists to hold: a perimeter that refuses the read
        # is a perimeter an operator switches off.
        for tool in ("Read", "Grep", "Glob", "LS", "NotebookRead",
                     "mcp__cousin__reply"):
            for field in ("file_path", "path", "notebook_path", "pattern"):
                self.assertEqual(
                    perimeter.write_targets(tool, {field: "/srv/config/law.md"}),
                    [], tool)

    def test_bash_reads_of_a_protected_path_are_not_write_targets(self):
        for command in ("cat /srv/config/law.md",
                        "cat /srv/self-portrait.md",
                        "cat /srv/shared/reference_house-style.md",
                        "grep -n rule /srv/config/law.md",
                        "git diff -- config/law.md",
                        "sed s/a/b/ config/law.md",
                        "cp config/law.md /tmp/x",
                        "wc -l shared/reference_house-style.md",
                        "rg -l private /srv/cousins/chico"):
            self.assertIsNone(perimeter.check_tool("Bash", {"command": command}),
                              command)

    def test_bash_writes_of_a_protected_path_are_write_targets(self):
        for command in ("echo x > config/law.md",
                        "echo x >> /srv/config/law.md",
                        "tee /srv/config/law.md",
                        "tee -a config/law.md",
                        "sed -i s/a/b/ config/law.md",
                        "sed -i.bak s/a/b/ config/law.md",
                        "cp /tmp/x shared/a.md",
                        "mv /tmp/x shared/a.md",
                        "rm self-portrait.md",
                        "chmod 644 /srv/config/law.md",
                        "dd of=config/law.md bs=1",
                        "cat > /srv/config/law.md; echo done"):
            self.assertIsNotNone(perimeter.check_tool("Bash", {"command": command}),
                                 command)

    def test_a_redirect_that_duplicates_a_descriptor_is_not_a_write(self):
        self.assertIsNone(perimeter.check_tool(
            "Bash", {"command": "cat /srv/config/law.md 2>&1"}))

    def test_each_segment_of_a_chained_command_is_checked(self):
        self.assertIsNotNone(perimeter.check_tool(
            "Bash", {"command": "cd /tmp && rm config/law.md"}))

    def test_a_malformed_command_does_not_raise(self):
        self.assertIsInstance(
            perimeter.write_targets("Bash", {"command": 'echo "unclosed'}), list)

    def test_a_tool_with_no_path_field_yields_nothing(self):
        self.assertEqual(perimeter.write_targets("Write", {"file_path": ""}), [])
        self.assertEqual(perimeter.write_targets("mcp__cousin__reply", None), [])


class TestCheckTool(unittest.TestCase):
    def test_a_write_to_the_law_is_refused_and_says_which_tool(self):
        reason = perimeter.check_tool("Write", {"file_path": "/srv/config/law.md"})
        self.assertIn("Write may not write", reason)
        self.assertIn("memory perimeter", reason)

    def test_a_bash_command_that_writes_the_law_is_refused(self):
        self.assertIsNotNone(perimeter.check_tool(
            "Bash", {"command": "echo x >> ~/cf/config/law.md"}))

    def test_an_ordinary_write_is_not(self):
        self.assertIsNone(perimeter.check_tool(
            "Write", {"file_path": "/srv/cf/cousins/chico/notes/a.md"}))
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


class TestUnderTheRoot(unittest.TestCase):
    """With the framework root known, only paths under it are protected."""

    def test_root_limits_the_shapes(self):
        from cousin_lib import perimeter as p
        root = "/srv/cf"
        self.assertIsNotNone(p.protected_reason("/srv/cf/config/law.md", root=root))
        self.assertIsNotNone(p.protected_reason("/srv/cf/shared/a.md", root=root))
        self.assertIsNone(p.protected_reason("/srv/cfx/shared/a.md", root=root))
        self.assertIsNone(p.protected_reason("/elsewhere/shared/a.md", root=root))
        # relative: joined to the cwd when known, else by shape alone
        self.assertIsNone(p.protected_reason("shared/a.md", root=root, cwd="/elsewhere"))
        self.assertIsNotNone(p.protected_reason("shared/a.md", root=root, cwd="/srv/cf"))
        self.assertIsNotNone(p.protected_reason("shared/a.md", root=root))
        self.assertIsNotNone(p.protected_reason("../config/law.md", root=root,
                                                cwd="/srv/cf/cousins"))
        # a shell expands ~ before it writes: so does the check
        with mock.patch.dict(os.environ, {"HOME": "/srv"}):
            self.assertIsNotNone(p.protected_reason("~/cf/config/law.md", root=root))
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            self.assertIsNone(p.protected_reason("~/cf/config/law.md", root=root))
        # no root given: shape alone, as before
        self.assertIsNotNone(p.protected_reason("/elsewhere/shared/a.md"))

    def test_anchored_where_an_install_keeps_them(self):
        # the default install's checkout IS the root: its templates are free
        from cousin_lib import perimeter as p
        root = "/srv/cf"
        free = ["/srv/cf/templates/shared/first-principles.md", "/srv/cf/templates/law.md",
                "/srv/cf/shared/proposed/bart__x.md", "/srv/cf/cousins/wren/notes/self-portrait.md",
                "/srv/cf/docs/config/law.md"]
        for path in free:
            self.assertIsNone(p.protected_reason(path, root=root), path)
        self.assertIsNone(p.protected_reason("templates/shared/a.md", root=root, cwd=root))
        for path in ("/srv/cf/config/law.md", "/srv/cf/shared/reference_a.md",
                     "/srv/cf/cousins/wren/self-portrait.md"):
            self.assertIsNotNone(p.protected_reason(path, root=root), path)


if __name__ == "__main__":
    unittest.main()
