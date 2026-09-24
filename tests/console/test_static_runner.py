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


class TestRunnerPaneHighlighting(unittest.TestCase):
    """The pane's syntax highlighting (operator's ask): the kind label in the
    accent, the model's text in the primary foreground, tool executions
    muted, diffs in diff colors, JSON and light markdown on top. The helpers
    are pure (a node tree of strings and {tag, cls, children}); only
    rpToReact makes elements, so untrusted text can never become markup."""

    def setUp(self):
        self.chat = (_STATIC / "chat.jsx").read_text()
        self.css = (_STATIC / "styles.css").read_text()
        self.src = self.chat[self.chat.index("function runnerEventLine("):
                             self.chat.index("function RunnerPaneView(")]

    def run_node(self, body):
        out = subprocess.run(["node", "-e", self.src + body], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_the_pane_renders_the_highlighted_body(self):
        pane = self.chat[self.chat.index("function RunnerPaneView("):]
        pane = pane[:pane.index("\n}\n")]
        self.assertIn('className="rp-kind"', pane)
        self.assertIn("runnerKindClass(ev.kind)", pane)
        self.assertIn("rpRowBody(ev)", pane)
        self.assertNotIn("dangerouslySetInnerHTML", self.src + pane)

    def test_the_colors_come_from_the_theme_variables(self):
        """The kind label follows the Settings hue slider (it overrides
        --accent on :root), so a literal color would not move with it."""
        css = self.css[self.css.index("Runner pane highlighting"):]
        self.assertRegex(css, r"\.rp-kind \{[^}]*color: var\(--accent\)")
        self.assertRegex(css, r"\.rp-text \.rp-body \{ color: var\(--fg-0\)")
        self.assertRegex(css, r"\.rp-tool \.rp-body \{ color: var\(--fg-2\)")
        self.assertRegex(css, r"\.rp-diff-add \{ color: var\(--green\)")
        self.assertRegex(css, r"\.rp-diff-del \{ color: var\(--red\)")
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,6}\b|rgb\(|oklch\(\d")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_each_kind_takes_its_color_group(self):
        kinds = ["text", "user", "result", "thinking", "tool", "tool_call", "tool_result",
                 "output", "error", "state", "turn_start", "session", "usage", "extract",
                 "propose", "rate_limit"]
        got = self.run_node("process.stdout.write(JSON.stringify(%s.map(runnerKindClass)));"
                            % json.dumps(kinds))
        self.assertEqual(dict(zip(kinds, got)), {
            "text": "text", "user": "text", "result": "text", "thinking": "thinking",
            "tool": "tool", "tool_call": "tool", "tool_result": "tool", "output": "tool",
            "error": "error", "state": "meta", "turn_start": "meta", "session": "meta",
            "usage": "meta", "extract": "meta", "propose": "meta", "rate_limit": "meta"})

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_real_diff_is_detected_and_a_dashed_list_is_not(self):
        got = self.run_node(r"""
const git = "diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n import os\n-x = 1\n+x = 2\n";
const bare = "@@ -4 +4 @@\n-old\n+new";
const list = "Plan:\n- read the code\n- write the test\n+ maybe more\n--- \nthat is all";
const rule = "---\n- one\n- two";
process.stdout.write(JSON.stringify({
  git: looksLikeDiff(git), bare: looksLikeDiff(bare), list: looksLikeDiff(list),
  rule: looksLikeDiff(rule), one: looksLikeDiff("-just one line"),
  classes: renderDiff(git).filter(n => typeof n === "object").map(n => n.cls),
}));
""")
        self.assertTrue(got["git"])
        self.assertTrue(got["bare"])
        self.assertFalse(got["list"])
        self.assertFalse(got["rule"])
        self.assertFalse(got["one"])
        self.assertEqual(got["classes"], [
            "rp-diff-file", "rp-diff-file", "rp-diff-file", "rp-diff-file", "rp-diff-hunk",
            "rp-diff-ctx", "rp-diff-del", "rp-diff-add", "rp-diff-ctx"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_tool_events_get_json_diff_and_plain_bodies(self):
        got = self.run_node(r"""
const cls = (nodes) => nodes.filter(n => typeof n === "object").map(n => n.cls);
const text = (nodes) => nodes.map(n => typeof n === "object" ? n.children.join("") : n).join("");
const edit = runnerEventBody({kind: "tool", payload: {name: "Edit",
  input: {file_path: "a.py", old_string: "x = 1", new_string: "x = 2"}}});
const bash = runnerEventBody({kind: "tool", payload: {name: "Bash", input: {command: "ls"}}});
const big = runnerEventBody({kind: "tool", payload: {name: "Write", input: {content: "y".repeat(1000)}}});
const js = runnerEventBody({kind: "tool_result", payload: {text: '{"ok": true, "n": 2}'}});
const gd = runnerEventBody({kind: "tool_result", payload: {text: "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b"}});
const plain = runnerEventBody({kind: "tool_result", payload: {is_error: true, text: "- not\n- a diff"}});
const call = runnerEventBody({kind: "tool_call", payload: {tool: "cousin-memory", command: "search", is_error: true, ms: 12}});
process.stdout.write(JSON.stringify({
  edit: cls(edit), bash: text(bash), bigLen: text(big).length, bigEnd: text(big).slice(-3),
  js: cls(js), jsText: text(js), gd: cls(gd), plain: cls(plain), plainText: text(plain),
  call: cls(call), callText: text(call),
}));
""")
        self.assertEqual(got["edit"], ["rp-tool-name", "rp-diff-file", "rp-diff-file",
                                       "rp-diff-del", "rp-diff-add"])
        self.assertEqual(got["bash"], 'Bash {\n  "command": "ls"\n}')
        # the existing budget: the compact form cut at 300, as before
        self.assertEqual(got["bigLen"], len("Write ") + 300)
        self.assertEqual(got["bigEnd"], "...")
        self.assertIn("rp-json-bool", got["js"])
        self.assertIn("rp-json-num", got["js"])
        self.assertEqual(got["jsText"], '{\n  "ok": true,\n  "n": 2\n}')
        self.assertEqual(got["gd"], ["rp-diff-file", "rp-diff-file", "rp-diff-hunk",
                                     "rp-diff-del", "rp-diff-add"])
        self.assertEqual(got["plain"], ["rp-err"])
        self.assertEqual(got["plainText"], "error: - not\n- a diff")
        self.assertEqual(got["call"], ["rp-tool-name", "rp-err", "rp-dim"])
        self.assertEqual(got["callText"], "cousin-memory search error (12 ms)")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_json_highlighter_classes_each_token(self):
        got = self.run_node(r"""
const nodes = highlightJson('{"key": "val", "n": -1.5e3, "t": true, "f": false, "z": null, "a": [1]}');
const pairs = nodes.filter(n => typeof n === "object").map(n => [n.cls, n.children[0]]);
const cut = highlightJson('{"text": "unterminated str...');
process.stdout.write(JSON.stringify({pairs, cut: cut.filter(n => typeof n === "object").map(n => n.cls)}));
""")
        pairs = got["pairs"]
        self.assertIn(["rp-json-key", '"key"'], pairs)
        self.assertIn(["rp-json-str", '"val"'], pairs)
        self.assertIn(["rp-json-num", "-1.5e3"], pairs)
        self.assertIn(["rp-json-bool", "true"], pairs)
        self.assertIn(["rp-json-bool", "false"], pairs)
        self.assertIn(["rp-json-null", "null"], pairs)
        self.assertIn(["rp-json-num", "1"], pairs)
        self.assertIn(["rp-json-punc", "{"], pairs)
        self.assertEqual(got["cut"], ["rp-json-punc", "rp-json-key", "rp-json-punc", "rp-json-str"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_markdown_lite_renders_structure(self):
        got = self.run_node(r"""
const md = renderMarkdownLite("## Plan\n- read **this** and `that`\n1. *one* [docs](https://example.com/x)\n\n```diff\n--- a\n+++ b\n-x\n+y\n```\n```json\n{\"a\": 1}\n```\nplain");
const walk = (n) => typeof n === "string" ? null : [n.tag, n.cls || "", (n.children || []).map(walk).filter(Boolean), n.href || ""];
process.stdout.write(JSON.stringify(md.map(walk)));
""")
        tags = [n[0] + ":" + n[1] for n in got]
        self.assertEqual(tags, ["div:rp-md-h rp-md-h2", "div:rp-md-li", "div:rp-md-li",
                                "div:rp-md-gap", "pre:rp-md-pre", "pre:rp-md-pre", "div:rp-md-p"])
        li = got[1][2]
        self.assertIn(["strong", "", [], ""], li)
        self.assertIn(["code", "rp-md-code", [], ""], li)
        li2 = got[2][2]
        self.assertIn(["em", "", [], ""], li2)
        self.assertIn(["a", "rp-md-a", [], "https://example.com/x"], li2)
        self.assertEqual([c[1] for c in got[4][2]],
                         ["rp-diff-file", "rp-diff-file", "rp-diff-del", "rp-diff-add"])
        self.assertIn("rp-json-key", [c[1] for c in got[5][2]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_markdown_lite_never_makes_markup_from_its_input(self):
        """Model output is untrusted: `<script>`, `<img onerror>` and a
        javascript: link stay text nodes; the only elements are the
        whitelisted tags the renderer itself chose, and rpToReact passes no
        raw-HTML prop and no non-http(s) href."""
        got = self.run_node(r"""
const evil = "<script>alert(1)</script>\n# <img src=x onerror=alert(1)>\n- **<b>x</b>** [c](javascript:alert(1)) [d](data:text/html,<script>)\n```\n<iframe src=//evil>\n```";
const tree = renderMarkdownLite(evil);
const tags = new Set(), texts = [];
const walk = (n) => {
  if (typeof n === "string") { texts.push(n); return; }
  tags.add(n.tag);
  if (n.tag === "a") texts.push("HREF:" + n.href);
  (n.children || []).forEach(walk);
};
tree.forEach(walk);
const made = [];
global.React = {createElement: (tag, props, ...children) => { made.push({tag, props: Object.keys(props), href: props.href || null}); return {tag, children}; }};
tree.forEach((n, i) => rpToReact(n, i));
rpToReact({tag: "script", children: ["x"]}, 0);
rpToReact({tag: "a", href: "javascript:alert(1)", children: ["y"]}, 1);
process.stdout.write(JSON.stringify({tags: [...tags].sort(), texts, made}));
""")
        self.assertTrue(set(got["tags"]) <= {"div", "span", "strong", "pre"}, got["tags"])
        joined = "".join(got["texts"])
        self.assertIn("<script>alert(1)</script>", joined)
        self.assertIn("<img src=x onerror=alert(1)>", joined)
        self.assertIn("<b>x</b>", joined)
        self.assertIn("<iframe src=//evil>", joined)
        self.assertNotIn("HREF:", joined)
        for el in got["made"]:
            self.assertIn(el["tag"], {"div", "span", "strong", "em", "code", "pre", "a"})
            self.assertNotIn("dangerouslySetInnerHTML", el["props"])
            self.assertIsNone(el["href"])


class TestRunnerPaneHighlightingCost(unittest.TestCase):
    """Review round 1: the text is untrusted and the pane's thread renders
    it, so no input may cost more than linear time. The link alternative
    was unbounded (a line of unclosed `[` was quadratic: 80 KB took about
    5 s at a374601), and text events reached the markdown renderer with no
    length cap."""

    def setUp(self):
        chat = (_STATIC / "chat.jsx").read_text()
        self.src = chat[chat.index("function runnerEventLine("):
                        chat.index("function RunnerPaneView(")]

    def run_node(self, body):
        out = subprocess.run(["node", "-e", self.src + body], capture_output=True,
                             text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_adversarial_markdown_renders_in_linear_time(self):
        got = self.run_node(r"""
const ms = (f) => { const t = process.hrtime.bigint(); f(); return Number(process.hrtime.bigint() - t) / 1e6; };
const mixed = ["[`*a**b](x [*`", "[a](b", "**a*b`c[d", "`[**a [b](", "*a [b *c](d"]
  .map(u => u.repeat(Math.ceil(50000 / u.length)).slice(0, 50000));
process.stdout.write(JSON.stringify({
  brackets: ms(() => renderMarkdownLite("[a".repeat(40000))),
  textEvent: ms(() => runnerEventBody({kind: "text", payload: {text: "[a".repeat(40000)}})),
  mixed: mixed.map(m => ms(() => renderMarkdownLite(m))),
  // the regex bound alone, without the length cap in front of it
  inlineBrackets: ms(() => mdInline("[a".repeat(40000))),
  inlineLinks: ms(() => mdInline("[a](b".repeat(16000))),
}));
""")
        self.assertLess(got["brackets"], 100, got)
        self.assertLess(got["textEvent"], 100, got)
        for t in got["mixed"]:
            self.assertLess(t, 100, got)
        self.assertLess(got["inlineBrackets"], 300, got)
        self.assertLess(got["inlineLinks"], 300, got)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_long_text_is_parsed_up_to_the_cap_and_kept_after_it(self):
        got = self.run_node(r"""
const text = "**b**\n" + "x".repeat(30000) + "\n**tail**";
const nodes = renderMarkdownLite(text);
const flat = (n) => typeof n === "string" ? n : (n.children || []).map(flat).join("");
const last = nodes[nodes.length - 1];
process.stdout.write(JSON.stringify({
  first: nodes[0].children[0].tag, lastCls: last.cls, lastTag: last.tag,
  tail: flat(last).slice(-8), total: nodes.map(flat).join("").length,
}));
""")
        self.assertEqual(got["first"], "strong")
        self.assertEqual(got["lastCls"], "rp-md-rest")
        self.assertEqual(got["tail"], "**tail**")      # plain text past the cap, not bold
        self.assertEqual(got["total"], len("b") + 30000 + len("\n**tail**"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_json_is_not_parsed_above_the_threshold_nor_pretty_when_it_explodes(self):
        got = self.run_node(r"""
const cls = (nodes) => nodes.filter(n => typeof n === "object").map(n => n.cls);
const big = JSON.stringify({rows: Array.from({length: 8000}, (_, i) => ({i, s: "row"}))});
let parsed = 0;
const orig = JSON.parse;
JSON.parse = (t) => { parsed++; return orig(t); };
const out = rpToolOutput(big);
JSON.parse = orig;
const deep = "[".repeat(100) + "]".repeat(100);
const deepOut = runnerEventBody({kind: "tool_result", payload: {text: deep}});
process.stdout.write(JSON.stringify({
  bigLen: big.length, parsed, bigCls: cls(out), bigText: out.join("").length,
  deepText: deepOut.map(n => typeof n === "string" ? n : n.children.join("")).join(""),
}));
""")
        self.assertGreater(got["bigLen"], 65536)
        self.assertEqual(got["parsed"], 0)
        self.assertEqual(got["bigCls"], [])
        self.assertEqual(got["bigText"], 600)
        # 200 compact characters, about 10 KB pretty: stays compact
        self.assertEqual(got["deepText"], "[" * 100 + "]" * 100)


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
