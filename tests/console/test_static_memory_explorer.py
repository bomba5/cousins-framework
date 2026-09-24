"""The memory explorer, pinned by text (the console compiles its JSX in
the browser; the headless check lives outside the suite). Each pin is a
canary for a defect the operator saw."""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _component(text, name):
    start = text.index("function %s(" % name)
    rest = text[start + 1:]
    m = re.search(r"^(function \w+\(|const \w+ = )", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


class MemoryViewShowsFilesWhole(unittest.TestCase):
    """The cut Markdown: the old view put GET /api/memory's 400-char
    `preview` in a <pre>. A cousin scope now renders the explorer, and
    files come whole from /api/memory/<slug>/file."""

    def test_the_memory_view_never_shows_a_preview(self):
        src = _component(_read("views.jsx"), "MemoryView")
        self.assertNotIn("preview", src)
        self.assertIn("<MemoryExplorer slug={scope}", src)

    def test_the_explorer_reads_whole_files_and_renders_markdown(self):
        src = _read("explorer.jsx")
        self.assertIn("/api/memory/${slug}/file?path=", src)
        viewer = _component(src, "FileViewer")
        self.assertIn("<MarkdownDoc text={page.text}", viewer)
        self.assertNotRegex(viewer, r"\.slice\(0,\s*\d+\)")

class ExplorerLayersAndRemoval(unittest.TestCase):
    def setUp(self):
        self.src = _read("explorer.jsx")

    def test_every_layer_the_overview_names_has_a_panel(self):
        src = _component(self.src, "MemoryExplorer")
        for layer in ("active", "index", "raw", "digest", "archive",
                      "distilled", "decisions", "memory", "notes",
                      "harness", "search", "recall", "trash", "legacy"):
            self.assertIn('"%s"' % layer, src, layer)

    def test_raw_entries_filter_by_level_topic_text_and_date(self):
        src = _component(self.src, "RawEntries")
        for param in ('"level"', '"topic"', '"q"', '"since"', '"until"'):
            self.assertIn("p.set(%s" % param, src)
        # the level filter moved from chips over the list to the rail,
        # which lists every level with its count (design A, 2026-09-22)
        explorer = _component(self.src, "MemoryExplorer")
        self.assertIn("TRUTH_LEVELS.map", explorer)
        self.assertIn("data-level-rail", explorer)
        self.assertIn("levels={levels} setLevels={setLevels}", explorer)

    def test_removal_asks_first_and_offers_undo(self):
        src = _component(self.src, "RawEntries")
        self.assertIn("<ConfirmButton", src)
        files = _component(self.src, "LayerFiles")
        self.assertIn("window.confirm", files)
        explorer = _component(self.src, "MemoryExplorer")
        self.assertIn("/api/memory/${slug}/delete", explorer)
        self.assertIn("/api/memory/${slug}/restore", explorer)
        self.assertIn("undo", explorer)

    def test_raw_entries_offer_mark_obsolete_with_a_reason(self):
        # Canary (2026-09-18): the explorer's L5 writer.
        raw = _component(self.src, "RawEntries")
        self.assertIn("onObsolete(e.topic)", raw)
        self.assertIn("mark obsolete", raw)
        explorer = _component(self.src, "MemoryExplorer")
        self.assertIn("/api/memory/${slug}/obsolete", explorer)
        self.assertIn("window.prompt", explorer)
        self.assertIn("onObsolete={markObsolete}", explorer)

    def test_a_removal_refetches_without_dropping_the_filters(self):
        # keyed remounts reset the level chips after every delete
        explorer = _component(self.src, "MemoryExplorer")
        self.assertNotIn("key={layer + tick}", explorer)
        self.assertIn("reload={tick}", explorer)


class MemoryByTruthLevel(unittest.TestCase):
    """Memory by truth level (design A): the rail counts each level, the
    list groups by level, an operator-stated entry carries its citation,
    and an obsolete entry is struck through and dimmed, never hidden."""

    def setUp(self):
        self.src = _read("explorer.jsx")
        self.css = _read("styles.css")

    def test_the_rail_counts_every_level(self):
        explorer = _component(self.src, "MemoryExplorer")
        self.assertIn("counts[l] || 0", explorer)
        self.assertIn("ov.insights && ov.insights.levels", explorer)
        self.assertIn("onCounts={setLevelCounts}", explorer)

    def test_the_list_groups_by_level_and_can_go_back_to_time(self):
        raw = _component(self.src, "RawEntries")
        self.assertIn("groupByLevel(data.entries)", raw)
        self.assertIn("data-level-group", raw)
        self.assertIn('grouping === "time" && data.entries.map(renderEntry)', raw)

    def test_an_operator_entry_shows_its_cite(self):
        raw = _component(self.src, "RawEntries")
        self.assertIn('e.level === "L0_OPERATOR"', raw)
        self.assertIn("entryCite(e)", raw)
        self.assertIn("data-cite", raw)

    def test_obsolete_is_struck_through_and_dimmed_not_hidden(self):
        self.assertRegex(self.css, r"\.mx-entry\.lvl-L5 \.mx-topic \{[^}]*line-through")
        self.assertRegex(self.css, r'\.mx-entry\[data-entry-level="L5_OBSOLETE"\] \{ opacity: 0\.\d+')
        raw = _component(self.src, "RawEntries")
        self.assertNotIn('!== "L5_OBSOLETE").map', raw)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_grouping_and_cite_behave(self):
        start = self.src.index("// ---- truth-level helpers")
        end = self.src.index("// ---- end truth-level helpers ----")
        probe = self.src[start:end] + """
const rows = [
  {topic: "a", level: "L3_COUSIN_CONCLUSION"},
  {topic: "b", level: "L5_OBSOLETE"},
  {topic: "c", level: "L0_OPERATOR", cite: "chat 12"},
  {topic: "d", level: "weird"},
  {topic: "e", level: "L3_COUSIN_CONCLUSION"},
  {topic: "f", level: "L0_OPERATOR", cite: null, extra: {}},
];
process.stdout.write(JSON.stringify({
  groups: groupByLevel(rows).map(g => [g.level, g.entries.map(e => e.topic)]),
  cites: rows.map(entryCite),
  labels: TRUTH_LEVELS.map(l => LEVEL_META[l].label),
}));"""
        out = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["groups"], [
            ["L0_OPERATOR", ["c", "f"]],
            ["L3_COUSIN_CONCLUSION", ["a", "e"]],
            ["L5_OBSOLETE", ["b"]],
            ["other", ["d"]],
        ])
        self.assertEqual(got["cites"], [None, None, "chat 12", None, None, None])
        self.assertEqual(got["labels"], ["operator", "framework", "tool", "conclusion",
                                         "hypothesis", "obsolete"])


if __name__ == "__main__":
    unittest.main()
