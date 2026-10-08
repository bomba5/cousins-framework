"""The jobs log follower, pinned by text (the console compiles its JSX
in the browser)."""
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


class JobsLogFollows(unittest.TestCase):
    """A running job showed no scrollable live log: the panel was a
    flex child without min-height 0 inside an overflow-hidden modal (it
    grew past the modal and was clipped), it re-fetched a 200-line tail
    and never followed the end, and a job without a log showed an empty
    box."""

    def setUp(self):
        self.src = _component(_read("views.jsx"), "JobLogPanel")

    def test_follows_by_offset_and_appends(self):
        self.assertIn("?from=${offsetRef.current}", self.src)
        self.assertIn("d.next", self.src)
        self.assertIn("t + d.log", self.src)

    def test_sticks_to_the_bottom_until_the_reader_scrolls_up(self):
        self.assertIn("box.scrollTop = box.scrollHeight", self.src)
        self.assertIn("onScroll={onScroll}", self.src)
        self.assertIn("atBottom", self.src)
        self.assertIn("data-job-log-follow", self.src)

    def test_the_box_can_scroll_inside_the_modal(self):
        css = _read("styles.css")
        rule = re.search(r"\.joblog \{[^}]*\}", css).group(0)
        self.assertIn("min-height: 0", rule)
        self.assertIn("overflow: auto", rule)
        view = _component(_read("views.jsx"), "JobsList")
        self.assertIn("minHeight: 0", view)

    def test_a_job_without_a_log_says_how_to_attach_one(self):
        self.assertIn("no log attached; start the job with", self.src)
        self.assertIn("cousin-job start shell TITLE -- CMD", self.src)
        self.assertIn("--log PATH", self.src)


if __name__ == "__main__":
    unittest.main()


class JobsArtifactsTab(unittest.TestCase):
    """The artifacts list is a tab of the Jobs view, not a panel at the
    bottom of the job list where nobody scrolled to it."""

    def setUp(self):
        self.view = _component(_read("views.jsx"), "JobsView")
        self.jobs = _component(_read("views.jsx"), "JobsList")

    def test_the_jobs_view_has_a_jobs_tab_and_an_artifacts_tab(self):
        self.assertIn("data-jobs-tabs", self.view)
        self.assertIn('["jobs", "jobs"]', self.view)
        self.assertIn('"artifacts"', self.view)
        self.assertIn("<ArtifactsPanel />", self.view)

    def test_the_job_list_no_longer_carries_the_panel(self):
        self.assertNotIn("<ArtifactsPanel />", self.jobs)

    def test_an_empty_list_says_how_rows_get_there(self):
        panel = _component(_read("views.jsx"), "ArtifactsPanel")
        self.assertIn("data-artifacts-empty", panel)
        self.assertIn("cousin-job start --artifact", panel)

