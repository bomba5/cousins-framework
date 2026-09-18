"""The memory explorer, pinned by text (the console compiles its JSX in
the browser; the headless check lives outside the suite). Each pin is a
canary for a defect the operator saw."""
import pathlib
import re
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
        self.assertIn("TRUTH_LEVELS.map", src)

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


if __name__ == "__main__":
    unittest.main()
