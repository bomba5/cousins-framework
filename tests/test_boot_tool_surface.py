"""The boot packet's Tool Surface layer.

The manifest at <root>/data/tool-surface.md is what a fresh generation
reads instead of re-discovering its CLIs. Bounded to 1500 characters,
degraded when absent, and the truncation marker counts inside the
budget like every other layer.
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.boot import LAYER_BUDGETS, assemble

TOOL_SURFACE_MAX = 1500


class ToolSurfaceCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory").mkdir()
        (self.home / "STATUS.md").write_text(
            "# STATUS\n\n## Open loops\n\n- one\n")
        (self.home / "self-portrait.md").write_text(
            "# Cousin Self-Portrait: testa\n## Voice\nPlain.\n"
            "## Operator Calibration\nShort.\n")
        (self.root / "config").mkdir()
        (self.root / "config" / "law.md").write_text("1. law\n")
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _manifest(self, text):
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "tool-surface.md").write_text(text)

    def _section(self, text):
        start = text.index("## 9. Tool Surface")
        end = text.index("## 10. Required Boot Actions")
        return text[start:end]


class TestToolSurfaceLayer(ToolSurfaceCase):
    def test_budget_is_1500_chars(self):
        self.assertEqual(LAYER_BUDGETS["tool_surface"][1], TOOL_SURFACE_MAX)

    def test_present_manifest_is_quoted_and_not_degraded(self):
        self._manifest("# Tool Surface\n\n- `cousin-memory` - usage: m\n"
                       "- `cousin-loops` - usage: l\n")
        pkt = assemble("testa", self.home)
        section = self._section(pkt["text"])
        self.assertIn("- `cousin-memory` - usage: m", section)
        self.assertIn("- `cousin-loops` - usage: l", section)
        self.assertNotIn("tool_surface", pkt["degraded_sections"])

    def test_the_manifest_title_line_is_not_repeated(self):
        self._manifest("# Tool Surface\n\n- `cousin-x` - u\n")
        section = self._section(assemble("testa", self.home)["text"])
        self.assertNotIn("# Tool Surface\n", section.split("\n", 1)[1])

    def test_absent_manifest_is_degraded_and_says_how_to_fix(self):
        pkt = assemble("testa", self.home)
        self.assertIn("tool_surface", pkt["degraded_sections"])
        section = self._section(pkt["text"])
        self.assertIn("cousin-tool-surface", section)
        self.assertIn("DEGRADED layers:", pkt["text"])

    def test_empty_manifest_is_degraded(self):
        self._manifest("")
        pkt = assemble("testa", self.home)
        self.assertIn("tool_surface", pkt["degraded_sections"])

    def test_bounded_with_the_marker_inside_the_budget(self):
        self._manifest("# Tool Surface\n" + "".join(
            "- `cousin-tool-%03d` - usage: cousin-tool-%03d [-h] x\n"
            % (i, i) for i in range(200)))
        pkt = assemble("testa", self.home)
        section = self._section(pkt["text"])
        body = section.split("\n", 1)[1].strip()
        self.assertLessEqual(len(body), TOOL_SURFACE_MAX)
        self.assertIn("truncated, tool_surface", body)
        self.assertNotIn("tool_surface", pkt["degraded_sections"])

    def test_sits_between_memories_and_boot_actions(self):
        self._manifest("# Tool Surface\n- `cousin-x` - u\n")
        text = assemble("testa", self.home)["text"]
        self.assertLess(text.index("## 8. Retrieved Memories"),
                        text.index("## 9. Tool Surface"))
        self.assertLess(text.index("## 9. Tool Surface"),
                        text.index("## 10. Required Boot Actions"))


if __name__ == "__main__":
    unittest.main()
