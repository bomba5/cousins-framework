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
        self.assertIn("<ArtifactsPanel ", self.view)

    def test_the_job_list_no_longer_carries_the_panel(self):
        self.assertNotIn("<ArtifactsPanel", self.jobs)

    def test_an_empty_list_says_how_rows_get_there(self):
        panel = _component(_read("views.jsx"), "ArtifactsPanel")
        self.assertIn("data-artifacts-empty", panel)
        self.assertIn("cousin-job start --artifact", panel)



class JobsAndArtifactsFollowTheHiddenToggle(unittest.TestCase):
    """A hidden cousin's jobs and artifacts stayed listed with the
    show-hidden toggle off, while its sidebar row and its loops went."""

    def setUp(self):
        src = _read("views.jsx")
        self.view = _component(src, "JobsView")
        self.panel = _component(src, "ArtifactsPanel")
        self.filter = _component(src, "visibleArtifacts")
        self.jobs = _component(src, "JobsList")
        self.job_filter = _component(src, "visibleJobs")

    def test_rows_of_hidden_cousins_go_unless_the_toggle_shows_them(self):
        self.assertIn("if (showHidden) return rows", self.filter)
        self.assertIn("hiddenSlugs.has(r.created_by)", self.filter)

    def test_the_view_reads_the_toggle_and_the_hidden_flag(self):
        self.assertIn('useSetting("showHidden")', self.view)
        self.assertIn("c.hidden", self.view)
        self.assertIn("hiddenSlugs={hiddenSlugs} showHidden={showHidden}", self.view)

    def test_the_tab_count_and_the_panel_both_filter(self):
        self.assertIn("visibleArtifacts(artifactRows", self.view)
        self.assertIn("visibleArtifacts(allRows", self.panel)

    def test_the_panel_says_how_many_it_hides(self):
        self.assertIn("data-artifacts-hidden", self.panel)
        self.assertIn("from hidden cousins", self.panel)
        self.assertIn("data-artifacts-all-hidden", self.panel)

    def test_nothing_hidden_flashes_before_the_cousins_answer(self):
        # null until /api/cousins answers; the filters hold rows back
        self.assertIn("React.useState(null)", self.view)
        self.assertIn(": [];", self.filter)
        self.assertIn(": [];", self.job_filter)
        self.assertIn("!hiddenSlugs && !showHidden", self.panel)

    def test_the_hidden_flags_are_polled_not_read_once(self):
        self.assertIn("setInterval(pull", self.view)

    def test_a_cousin_going_hidden_takes_its_pick_and_open_log(self):
        self.assertIn('hiddenSlugs.has(spawnedBy)) setSpawnedBy("all")', self.jobs)
        self.assertIn("hiddenSlugs.has(o.spawned_by) ? null", self.jobs)

    def test_jobs_of_hidden_cousins_go_unless_the_toggle_shows_them(self):
        self.assertIn("if (showHidden) return rows", self.job_filter)
        self.assertIn("hiddenSlugs.has(j.spawned_by)", self.job_filter)
        self.assertIn("hiddenSlugs={hiddenSlugs} showHidden={showHidden} />", self.view)
        self.assertIn("visibleJobs(allJobs", self.jobs)
        self.assertIn("data-jobs-hidden", self.jobs)
