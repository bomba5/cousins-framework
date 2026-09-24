"""Static-file contract for the runner cousin's views (master plan phase 5
tasks 2, 4 and 6): the chat page's pane is the reasoning stream for a
runner cousin and the tmux terminal for any other, the fleet card shows the
runner's state and what it declares unsupported, and the tokens view shows
the cache hit rate. The routes it calls are the spec's (the chat module's
own test checks every route against docs/reference/console-api.md)."""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


class TestRunnerPane(unittest.TestCase):
    def setUp(self):
        self.chat = (_STATIC / "chat.jsx").read_text()

    def test_the_pane_is_chosen_by_the_row(self):
        self.assertRegex(self.chat, r"c\.runner\s*\?\s*<RunnerPaneView")
        self.assertIn("function RunnerPaneView(", self.chat)
        self.assertIn("RunnerPaneView", self.chat.split("Object.assign(window")[-1])

    def test_the_pane_is_keyed_per_cousin(self):
        """Switching from runner cousin A to runner cousin B must remount
        the pane (fresh state, note and typed text), never carry A's into
        B: `<RunnerPaneView>` needs a `key` on the cousin's slug, not just
        `cousin={c}`."""
        self.assertRegex(self.chat, r"<RunnerPaneView\s+key=\{c\.slug\}")

    def test_it_streams_interrupts_and_says(self):
        pane = self.chat[self.chat.index("function RunnerPaneView("):]
        pane = pane[:pane.index("\n}\n")]
        self.assertIn('new EventSource(`/api/cousins/${encodeURIComponent(slug)}/stream`)', pane)
        self.assertIn('addEventListener("runner-event"', pane)
        self.assertIn('addEventListener("session"', pane)
        self.assertIn("`/api/cousins/${encodeURIComponent(slug)}/interrupt`", pane)
        self.assertIn("`/api/cousins/${encodeURIComponent(slug)}/say`", pane)
        self.assertNotIn("/api/pane/", pane)

    def test_events_are_batched_into_one_render_per_frame(self):
        """Review I4: a burst of SSE messages is one React update per
        animation frame, not one per event."""
        pane = self.chat[self.chat.index("function RunnerPaneView("):]
        pane = pane[:pane.index("\n}\n")]
        self.assertIn("requestAnimationFrame", pane)

    def test_a_dead_runner_is_not_running_and_cannot_be_interrupted(self):
        """Review M2: after the runner is gone its last recorded state stays
        in the stream; the pane says it is not running. Review 3b: the
        fleet row is only the fallback/tiebreaker (paneLiveness), read
        through `runner.alive`, not the pane's only source of truth."""
        pane = self.chat[self.chat.index("function RunnerPaneView("):]
        pane = pane[:pane.index("\n}\n")]
        self.assertIn("paneLiveness(", pane)
        self.assertIn("live.alive", pane)
        self.assertIn("not running", pane)
        fn = self.chat[self.chat.index("function paneLiveness("):
                       self.chat.index("function RunnerPaneView(")]
        self.assertIn("fleetRunner", fn)
        self.assertIn(".alive", fn)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_every_kind_reads_as_a_line(self):
        src = self.chat[self.chat.index("function runnerEventLine("):
                        self.chat.index("function RunnerPaneView(")]
        probe = src + """
const ev = (kind, payload) => runnerEventLine({kind, payload});
process.stdout.write(JSON.stringify([
  ev("state", {from: "idle", to: "running", detail: "turn"}),
  ev("thinking", {length: 5, text: "hmm..", truncated: true}),
  ev("thinking", {length: 5}),
  ev("tool", {name: "Bash", input: {command: "ls"}}),
  ev("result", {inbox_ids: [3], interrupted: true}),
  ev("rate_limit", {status: "rejected"}),
  ev("session", {session: "fake-1c035576"}),
]));
"""
        out = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [
            "idle -> running (turn)", "hmm.. [truncated]", "(5 chars, not recorded)",
            'Bash {"command":"ls"}', "rows [3] interrupted", '{"status":"rejected"}',
            "fake-1c035576"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_pane_liveness_follows_the_stream_not_the_15s_fleet_poll(self):
        """Review 3b: the header and the interrupt button used to read
        straight off the fleet row (`cousin.runner.alive`), which only
        refreshes on the 15s `cousins-refresh` poll, so a restart or a stop
        lagged by up to 15s while the stream already had the truth. The
        fleet row is now only the fallback before any stream evidence, and
        the tiebreaker whenever it refreshes after the last stream event.
        Round 1 fixes: a `session` frame resets state to a neutral
        "starting" (never a stale leftover state like "stopped" read as
        live), and a `fleet` event that IS applied adopts alive and state
        TOGETHER, never a mismatched pair."""
        src = self.chat[self.chat.index("function runnerEventLine("):
                        self.chat.index("function RunnerPaneView(")]
        self.assertIn("function paneLiveness(", src)
        probe = src + """
let s = paneLiveness(null, {kind: "fleet"}, {alive: false, state: "stopped"});  // fallback before any stream evidence
const results = {};
results.fallback = s;
s = paneLiveness(s, {kind: "session"}, {alive: false});        // a restart's session frame: alive, neutral state
results.session_after_stopped = s;
s = paneLiveness(s, {kind: "state", payload: {from: "idle", to: "running"}}, {alive: false});
results.running = s;                                            // a state event: alive + the state
s = paneLiveness(s, {kind: "state", payload: {from: "running", to: "stopped"}}, {alive: true});
results.stopped = s;                                             // the terminal state: not alive
s = paneLiveness(s, {kind: "fleet"}, {alive: true, state: "running"});  // a newer fleet row wins, as a whole pair
results.fleet_after_stopped = s;
process.stdout.write(JSON.stringify(results));
"""
        out = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertEqual(results["fallback"], {"alive": False, "state": "stopped"})
        self.assertEqual(results["session_after_stopped"], {"alive": True, "state": "starting"})
        self.assertEqual(results["running"], {"alive": True, "state": "running"})
        self.assertEqual(results["stopped"], {"alive": False, "state": "stopped"})
        # A fresh fleet row landing after the stopped-state evidence is
        # fresher (e.g. a restart the stream connection missed): it wins,
        # and alive/state come from it TOGETHER, never a mismatched pair
        # like alive:true with a leftover state:"stopped".
        self.assertEqual(results["fleet_after_stopped"], {"alive": True, "state": "running"})

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_fleet_effect_is_keyed_on_the_runners_own_data_not_the_cousin_row(self):
        """Round 1 finding 1: app.jsx's `cousin-status` SSE handler (around
        app.jsx:454, driven by routes_fleet.py's `cousin-status` events on
        the console's own start/stop actions) spreads a brand new cousin
        row on every status patch WITHOUT touching `c.runner`. Keying the
        fleet effect on `cousin` itself re-dispatched that unchanged runner
        snapshot as "fresh" evidence, able to land after and override a live
        stream event - the same bug, shorter. `runnerFleetKey` gives the
        effect a value key instead: unchanged runner data (even as a new
        object) must not look like a fresh update, and changed data must."""
        src = self.chat[self.chat.index("function runnerEventLine("):
                        self.chat.index("function RunnerPaneView(")]
        self.assertIn("function runnerFleetKey(", src)
        pane = self.chat[self.chat.index("function RunnerPaneView("):]
        pane = pane[:pane.index("\n}\n")]
        self.assertIn("runnerFleetKey(runner)", pane)
        self.assertNotIn("}, [cousin]);", pane)
        probe = src + """
const a = runnerFleetKey({alive: true, state: "running", session: "s1", since: 100});
// A status-only patch (app.jsx's cousin-status handler) spreads a NEW
// cousin object but the SAME runner data (a distinct object, same values):
// the key must be equal, so the effect it gates does not re-fire.
const b = runnerFleetKey({alive: true, state: "running", session: "s1", since: 100});
// A real refresh with different runner data must produce a different key.
const c = runnerFleetKey({alive: false, state: "stopped", session: "s1", since: 100});
process.stdout.write(JSON.stringify({same: a === b, different: a === c}));
"""
        out = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertTrue(result["same"])
        self.assertFalse(result["different"])


class TestFleetAndTokens(unittest.TestCase):
    def test_the_card_shows_the_runners_state_and_unsupported_items(self):
        cousins = (_STATIC / "cousins.jsx").read_text()
        self.assertIn("c.runner.state", cousins)
        self.assertIn("c.runner.alive", cousins)            # review M2
        self.assertIn("(not running)", cousins)
        self.assertIn("c.runner.unsupported.join", cousins)

    def test_the_tokens_view_shows_the_cache_rate(self):
        views = (_STATIC / "views.jsx").read_text()
        body = views[views.index("function TokensView("):views.index("function Stat(")]
        self.assertIn("row.cache", body)
        self.assertRegex(body, r'label="cache today"')
        self.assertRegex(body, r'label="cache 14 days"')


if __name__ == "__main__":
    unittest.main()
