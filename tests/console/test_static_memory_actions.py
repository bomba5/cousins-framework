"""The memory explorer's operator actions (WP-E), pinned by text: each
new panel is reachable from the rail and calls its route, the gates the
server enforces are said in the UI ahead of time, and the pure helpers
behave (run in node when it is installed)."""
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


class TheRailReachesEveryAction(unittest.TestCase):
    def setUp(self):
        self.src = _read("explorer.jsx")
        self.explorer = _component(self.src, "MemoryExplorer")

    def test_each_action_has_a_rail_entry_and_a_panel(self):
        for layer, component in (("op-search", "MemorySearch"),
                                 ("op-write", "MemoryWrite"),
                                 ("op-tensions", "TensionsList"),
                                 ("op-review", "ReviewQueue"),
                                 ("op-history", "TopicHistory"),
                                 ("op-maintain", "MemoryMaintenance"),
                                 ("op-portrait", "SelfPortrait"),
                                 ("op-moments", "MomentsList")):
            self.assertIn('"%s"' % layer, self.explorer, layer)
            self.assertIn('layer === "%s" && <%s' % (layer, component),
                          self.explorer, component)

    def test_each_panel_calls_its_route(self):
        for component, route in (
                ("MemorySearch", "/api/memory/${slug}/search?"),
                ("MemoryWrite", "/api/memory/${slug}/writer"),
                ("MemoryWrite", "/api/memory/${slug}/remember"),
                ("MemoryWrite", "/api/memory/${slug}/decide"),
                ("SelfPortrait", "/api/memory/${slug}/portrait/commit"),
                ("TensionsList", "/api/memory/${slug}/tensions"),
                ("TopicHistory", "/api/memory/${slug}/history?topic="),
                ("ReviewQueue", "/api/memory/${slug}/review"),
                ("MemoryMaintenance", "/api/memory/${slug}/maintain"),
                ("SelfPortrait", "/api/memory/${slug}/portrait"),
                ("MomentsList", "/api/memory/${slug}/callbacks"),
                ("MomentsList", "/api/memory/${slug}/capsules"),
                ("SharedReviewersPanel", "/api/shared/reviewers")):
            self.assertIn(route, _component(self.src, component),
                          "%s -> %s" % (component, route))

    def test_a_claim_is_retired_with_its_id(self):
        self.assertIn("/api/memory/${slug}/obsolete", self.explorer)
        self.assertIn("retireClaim", self.explorer)
        self.assertIn("retire this claim", _component(self.src, "ClaimRetire"))
        self.assertIn("claimRetireBody(topic, id, why)",
                      _component(self.src, "ClaimRetire"))

    def test_raw_entries_open_the_topic_history(self):
        raw = _component(self.src, "RawEntries")
        self.assertIn("onHistory(e.topic)", raw)
        self.assertIn("onHistory={openHistory}", self.explorer)


class TheGatesAreSaid(unittest.TestCase):
    def setUp(self):
        self.src = _read("explorer.jsx")

    def test_the_write_form_offers_the_servers_levels_and_shows_the_cite(self):
        write = _component(self.src, "MemoryWrite")
        self.assertIn("writer.levels", write)
        self.assertIn("can_write_operator", write)
        self.assertIn("data-cite-preview", write)
        # the client never sends a cite of its own, only a note
        self.assertNotIn("cite:", write)

    def test_a_drop_asks_twice(self):
        review = _component(self.src, "ReviewQueue")
        self.assertIn("<ConfirmButton", review)
        self.assertIn("no undo", review)

    def test_maintenance_runs_as_a_long_op(self):
        maint = _component(self.src, "MemoryMaintenance")
        self.assertIn("useLongOp(slug)", maint)
        self.assertIn("<LongOpStatus", maint)
        self.assertIn("dry_run: true", maint)
        self.assertIn("<ConfirmButton", maint)

    def test_the_portrait_commit_is_typed(self):
        portrait = _component(self.src, "SelfPortrait")
        self.assertIn("portraitCommitReady(typed, slug, st.candidate_sha, dirty)",
                      portrait)
        self.assertIn("confirm: typed, sha: st.candidate_sha", portrait)
        self.assertIn("replace: true", portrait)

    def test_the_memory_view_shows_the_reviewers(self):
        view = _component(_read("views.jsx"), "MemoryView")
        self.assertIn("<SharedReviewersPanel", view)
        self.assertIn("you_review === false", view)
        panel = _component(self.src, "SharedReviewersPanel")
        self.assertIn("<ConfirmButton", panel)

    def test_no_em_dash(self):
        self.assertNotIn("—", self.src)


class RoundOne(unittest.TestCase):
    """The review round's findings, pinned."""

    def setUp(self):
        self.src = _read("explorer.jsx")

    def test_confirm_button_can_be_disabled_and_the_gated_ones_are(self):
        confirm = _component(self.src, "ConfirmButton")
        self.assertIn("disabled={!!disabled}", confirm)
        self.assertIn("if (disabled) return;", confirm)
        review = _component(self.src, "ReviewQueue")
        self.assertIn('<ConfirmButton className="btn danger" disabled={blocked}', review)
        self.assertIn("const blocked = busy || running || !loggedIn;", review)
        portrait = _component(self.src, "SelfPortrait")
        self.assertIn('confirmLabel="confirm: replace the candidate" disabled={busy}', portrait)

    def test_a_big_settle_is_followed_as_a_long_op(self):
        review = _component(self.src, "ReviewQueue")
        self.assertIn('op.kind === "memory-review"', review)
        self.assertIn("r.status === 202", review)
        self.assertIn('<LongOpStatus slug={slug} kind="memory-review"', review)

    def test_an_operator_claim_offers_no_retire_to_anyone_else(self):
        row = _component(self.src, "ClaimRow")
        self.assertIn('normLevel(c.truth_level) === "L0_OPERATOR" && !operator', row)
        self.assertIn("!theirs && <ClaimRetire", row)
        raw = _component(self.src, "RawEntries")
        self.assertIn('(e.level !== "L0_OPERATOR" || operator)', raw)
        explorer = _component(self.src, "MemoryExplorer")
        self.assertIn("writer.can_write_operator", explorer)

    def test_errors_are_said_and_calls_go_through_the_shared_helper(self):
        hook = _component(self.src, "useMemoryJson")
        self.assertIn("setErr(", hook)
        self.assertIn('safeSend("GET", url)', hook)
        safe = _component(self.src, "safeSend")
        self.assertIn("await apiSend(method, path, body)", safe)
        self.assertIn("catch (e)", safe)
        search = _component(self.src, "MemorySearch")
        self.assertNotIn("fetch(", search)
        self.assertIn('safeSend("GET"', search)
        for name in ("MemoryWrite", "ReviewQueue", "MemoryMaintenance",
                     "SelfPortrait", "SharedReviewersPanel"):
            self.assertNotIn("apiSend(", _component(self.src, name), name)

    def test_capsule_lists_are_guarded(self):
        moments = _component(self.src, "MomentsList")
        self.assertIn("Array.isArray(v)", moments)
        self.assertNotIn("c.evidence.join", moments)

    def test_only_an_editor_gets_the_edit_button(self):
        panel = _component(self.src, "SharedReviewersPanel")
        self.assertIn("disabled={!st.can_edit}", panel)


class TheHelpersBehave(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_helpers(self):
        src = _read("explorer.jsx")
        start = src.index("// ---- operator-action helpers")
        end = src.index("// ---- end operator-action helpers ----")
        probe = src[start:end] + """
process.stdout.write(JSON.stringify({
  legs: [hitLegsText(["keyword"]), hitLegsText(["keyword", "semantic"]), hitLegsText([])],
  review: reviewBody({a: "keep", b: "drop", c: undefined, d: "maybe"}, "  tidy "),
  retire: [claimRetireBody("t", "id1", " moved "), claimRetireBody("t", "id1", "  ")],
  ready: [portraitCommitReady("wren", "wren", "abc", false),
          portraitCommitReady("wren ", "wren", "abc", false),
          portraitCommitReady("wren", "wren", null, false),
          portraitCommitReady("wren", "wren", "abc", true)],
}));"""
        out = subprocess.run(["node", "-e", probe], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["legs"], ["keyword", "keyword + meaning", "fused"])
        self.assertEqual(got["review"], {"verdicts": {"a": "keep", "b": "drop"},
                                         "why": "tidy"})
        self.assertEqual(got["retire"], [{"topic": "t", "why": "moved",
                                          "entry": "id1"}, None])
        self.assertEqual(got["ready"], [True, False, False, False])


if __name__ == "__main__":
    unittest.main()
