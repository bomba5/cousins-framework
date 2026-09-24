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
        in the stream; the pane says it is not running."""
        pane = self.chat[self.chat.index("function RunnerPaneView("):]
        pane = pane[:pane.index("\n}\n")]
        self.assertIn("runner.alive", pane)
        self.assertIn("not running", pane)

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
]));
"""
        out = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [
            "idle -> running (turn)", "hmm.. [truncated]", "(5 chars, not recorded)",
            'Bash {"command":"ls"}', "rows [3] interrupted", '{"status":"rejected"}'])


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
