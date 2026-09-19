"""The console on a phone, pinned by text: the page compiles its JSX in
the browser and has no build step, so these tests read styles.css and
the views the way the other static tests do.

The failure they hold off: a grid or flex child defaults to
min-width:auto, so one unbreakable line (a path, a hash, a nowrap
activity line, a wide table) widens its track past the viewport and
.main's overflow:hidden cuts the page off on the right.
"""
import pathlib
import re
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _mobile_css():
    """Every rule inside a `@media (max-width: 820px)` block, joined."""
    css = _read("styles.css")
    out = []
    for m in re.finditer(r"@media \(max-width: 820px\) \{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[m.end():i])
    return "\n".join(out)


class MobileTracksShrink(unittest.TestCase):
    """Single-column grids on a phone are minmax(0, 1fr), never a bare
    1fr, whose minimum is the widest child's content."""

    def setUp(self):
        self.css = _mobile_css()

    def test_no_bare_1fr_single_column_override(self):
        self.assertNotRegex(self.css, r"grid-template-columns:\s*1fr\b")

    def test_inline_grids_and_named_grids_collapse_to_a_shrinkable_track(self):
        for sel in ('div[style*="grid-template-columns"]',
                    "[data-memory-grid]", ".mt-layout"):
            self.assertIn(sel, self.css, sel)
        self.assertIn("minmax(0, 1fr)", self.css)

    def test_grid_children_may_shrink(self):
        self.assertRegex(self.css, r'\[style\*="display: grid"\] > \*')
        self.assertIn("min-width: 0", self.css)

    def test_card_stats_are_two_shrinkable_columns(self):
        self.assertIn("repeat(2, minmax(0, 1fr))", self.css)


class MobileTablesAndRows(unittest.TestCase):
    """Wide tables either scroll in their own box or stack into cards;
    header rows wrap instead of running off the right edge."""

    def setUp(self):
        self.css = _mobile_css()
        self.views = _read("views.jsx")

    def test_tracker_table_stacks_with_labelled_cells(self):
        self.assertIn("table.data.tracker-table thead { display: none; }",
                      self.css)
        for label in ("title", "owner", "domain", "state", "tags",
                      "updated", "actions"):
            self.assertIn('data-label="%s"' % label, self.views, label)

    def test_panel_bodies_scroll_sideways(self):
        self.assertRegex(self.css, r"\.panel-body[^{]*\{[^}]*overflow-x: auto")

    def test_job_header_row_wraps(self):
        self.assertIn('className="job-head"', self.views)
        self.assertIn(".job-head { flex-wrap: wrap;", self.css)

    def test_meeting_thread_header_wraps(self):
        self.assertIn(".mt-thread-hdr { flex-wrap: wrap;", self.css)

    def test_drawers_clear_the_safe_area_topbar(self):
        self.assertIn("top: calc(36px + env(safe-area-inset-top, 0px));",
                      self.css)


if __name__ == "__main__":
    unittest.main()
