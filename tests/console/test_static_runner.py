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
                 "propose", "rate_limit", "auth"]
        got = self.run_node("process.stdout.write(JSON.stringify(%s.map(runnerKindClass)));"
                            % json.dumps(kinds))
        self.assertEqual(dict(zip(kinds, got)), {
            "text": "text", "user": "text", "result": "text", "thinking": "thinking",
            "tool": "tool", "tool_call": "tool", "tool_result": "tool", "output": "tool",
            "error": "error", "state": "meta", "turn_start": "meta", "session": "meta",
            "usage": "meta", "extract": "meta", "propose": "meta", "rate_limit": "meta",
            "auth": "error"})

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

    def test_where_the_markdown_stops_is_marked(self):
        """#99: past the cap the text is plain, and nothing said so: a raw
        `**` after 20k characters looked like a rendering bug. The rest
        gets a thin rule and a muted note, from the theme's variables."""
        self.assertIn('cls: "rp-md-rest"', self.chat)
        rest = re.search(r"\.rp-md-rest \{([^}]*)\}", self.css)
        self.assertIsNotNone(rest)
        self.assertRegex(rest.group(1), r"border-top: 1px \w+ var\(--line\)")
        self.assertIn("white-space: pre-wrap", rest.group(1))
        note = re.search(r"\.rp-md-rest::before \{([^}]*)\}", self.css)
        self.assertIsNotNone(note)
        self.assertIn('content: "raw text from here"', note.group(1))
        self.assertIn("color: var(--fg-3)", note.group(1))


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


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestRunnerPaneFolded(unittest.TestCase):
    """The folded pane (#125): the stream becomes a status strip (activity,
    quota, session, totals) and one row per real thing (a turn, a tool with
    its result, a reply, text, a turn's summary). Raw keeps one row per
    event. The fold is pure, so it is tested on event lists in node."""

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

    EVENTS = """
const E = (seq, kind, payload) => ({seq, ts: 1000 + seq, kind, payload});
const evs = [
  E(1, "runner", {kind: "sdk"}), E(2, "policy", {describe: "x"}), E(3, "system", {subtype: "fresh"}),
  E(4, "state", {from: "idle", to: "running"}),
  E(5, "turn_start", {bodies: ["hi"]}), E(6, "recall", {hits: 3}),
  E(7, "session_init", {model: "claude-opus-5-5", apiKeySource: "none"}),
  E(8, "rate_limit", {status: "allowed_warning", resets_at: 1790733600, type: "seven_day", utilization: 0.91}),
  E(9, "user", {text: "[operator:jo] chat from jo at 2026-09-25 09:48 UTC\\n\\nretire them"}),
  E(10, "system", {subtype: "thinking_tokens"}), E(11, "system", {subtype: "thinking_tokens"}),
  E(14, "thinking", {length: 0, text: ""}),
  E(15, "tool", {id: "t1", name: "Bash", input: {command: "ls", description: "List files"}}),
  E(16, "system", {subtype: "vcs_state_changed"}),
  E(17, "tool_result", {tool_use_id: "t1", is_error: false, text: "a b"}),
  E(18, "tool", {id: "t2", name: "mcp__cousin__reply", input: {text: "done"}}),
  E(19, "tool_call", {tool: "reply", command: "", is_error: false, ms: 9}),
  E(20, "tool_result", {tool_use_id: "t2", is_error: false, text: "replied to jo (#7)"}),
  E(21, "user", {text: "[operator:jo] chat from jo at 2026-09-25 09:50 UTC\\n\\nalso this"}),
  E(22, "text", {text: "ok"}), E(23, "checkpoint", {kind: "session"}),
  E(24, "result", {num_turns: 3, is_error: false, interrupted: false}),
  E(25, "usage", {total: 254517, cost_usd: 0.44, estimate: true}),
  E(26, "extract", {written: 2}), E(27, "propose", {proposal: null}),
  E(28, "state", {from: "running", to: "idle"}),
];
"""

    def test_the_stream_folds_into_rows(self):
        got = self.run_node(self.EVENTS + """
const m = rpModel(evs);
process.stdout.write(JSON.stringify({
  types: m.rows.map(r => r.t + (r.mid ? ":mid" : "")),
  boot: m.rows[0].events.length,
  recall: m.rows[1].recall.hits,
  head: rpTurnHead(m.rows[1]),
  mid: rpTurnHead(m.rows[5]).snippet,
  thought: m.rows[2].secs,
  tool: [m.rows[3].result.payload.text, rpToolLabel("Bash", {command: "ls", description: "List files"})],
  reply: [m.rows[4].ms, m.rows[4].result.payload.text],
  footer: rpFooterParts(m.rows[7].meta).join(" · "),
}));""")
        self.assertEqual(got["types"], ["boot", "turn", "thinking", "tool", "reply", "turn:mid", "text", "footer"])
        self.assertEqual(got["boot"], 3)
        self.assertEqual(got["recall"], 3)
        self.assertEqual(got["head"]["head"], "jo 09:48")
        self.assertEqual(got["head"]["snippet"], "retire them")
        self.assertEqual(got["mid"], "also this")
        self.assertEqual(got["thought"], 4)  # first thinking tick at 1010, thought at 1014
        self.assertEqual(got["tool"], ["a b", {"name": "Bash", "server": None, "desc": "List files"}])
        self.assertEqual(got["reply"], [9, "replied to jo (#7)"])
        # the checkpoint before the result and the meta after it land on one line
        self.assertEqual(got["footer"], "done · 3 steps · 255k tok · $0.44 est · 2 memories · checkpoint")

    def test_a_background_turn_is_its_own_turn_row(self):
        # #134: a CLI turn of its own between turns (a task notification)
        got = self.run_node(self.EVENTS + """
const more = [
  E(30, "system", {subtype: "background_turn", phase: "start"}),
  E(31, "user", {text: "<task-notification>done</task-notification>"}),
  E(32, "text", {text: "noted"}),
  E(33, "result", {num_turns: 1, is_error: false, background: true, inbox_ids: []}),
  E(34, "system", {subtype: "background_turn", phase: "handed_over"}),
  E(35, "system", {subtype: "background_end"}),
];
const m = rpModel(evs.concat(more));
const rows = m.rows.slice(8);
process.stdout.write(JSON.stringify({
  types: rows.map(r => r.t), head: rpTurnHead(rows[0]),
  lines: rows.filter(r => r.t === "line").map(r => r.text), turns: m.strip.turns,
}));""")
        self.assertEqual(got["types"], ["turn", "text", "footer", "line", "line"])
        self.assertEqual(got["head"]["head"], "background")
        self.assertEqual(got["head"]["snippet"], "<task-notification>done</task-notification>")
        self.assertEqual(got["lines"], ["background turn handed to the next turn",
                                        "the CLI's stream ended between turns"])
        self.assertEqual(got["turns"], 2)

    def test_the_strip_reads_the_runner_now(self):
        got = self.run_node(self.EVENTS + """
const done = rpModel(evs).strip;
const mid = rpModel(evs.slice(0, 11)).strip;
const tool = rpModel(evs.slice(0, 13)).strip;
process.stdout.write(JSON.stringify({done, mid, tool,
  rate: rpRate("seven_day", done.rate.seven_day),
  over: rpRate("five_hour", {status: "rejected", utilization: 1.2}).level,
  ok: rpRate("five_hour", {status: "allowed", utilization: 0.2})}));""")
        self.assertIsNone(got["done"]["activity"])
        self.assertEqual(got["done"]["state"], "idle")
        self.assertEqual(got["done"]["session"], {"model": "claude-opus-5-5", "auth": "your login"})
        self.assertEqual((got["done"]["tokens"], got["done"]["turns"]), (254517, 1))
        self.assertEqual(got["mid"]["activity"], {"kind": "thinking", "since": 1010})
        self.assertEqual(got["tool"]["activity"], {"kind": "tool", "name": "Bash", "since": 1015})
        self.assertEqual(got["rate"]["label"], "7-day")
        self.assertEqual((got["rate"]["pct"], got["rate"]["level"]), (91, "warn"))
        self.assertRegex(got["rate"]["resets"], r"^(Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d\d:\d\d$")
        self.assertEqual(got["over"], "over")
        self.assertEqual((got["ok"]["pct"], got["ok"]["level"]), (20, "ok"))

    def test_an_api_key_session_says_so(self):
        got = self.run_node("""
const s = rpModel([{kind: "session_init", payload: {model: "m", apiKeySource: "ANTHROPIC_API_KEY"}}]).strip.session;
process.stdout.write(JSON.stringify(s));""")
        self.assertEqual(got["auth"], "API key (ANTHROPIC_API_KEY)")

    def test_a_folded_messages_recall_lands_on_its_own_divider(self):
        """The runner emits a message's recall before its user event, so
        a folded message's recall must not overwrite the turn's."""
        got = self.run_node("""
const E = (seq, kind, payload) => ({seq, ts: 1000 + seq, kind, payload});
const m = rpModel([
  E(1, "turn_start", {bodies: ["a"]}), E(2, "recall", {hits: 3}), E(3, "user", {text: "a"}),
  E(4, "recall", {hits: 5}), E(5, "recall", {hits: 0}), E(6, "user", {text: "b"}), E(7, "user", {text: "c"}),
  E(8, "turn_start", {bodies: ["d"]}), E(9, "user", {text: "d"}), E(10, "recall", {hits: 2}), E(11, "user", {text: "e"}),
]);
process.stdout.write(JSON.stringify(m.rows.map(r => [r.t + (r.mid ? ":mid" : ""), r.recall && r.recall.hits])));""")
        self.assertEqual(got, [["turn", 3], ["turn:mid", 5], ["turn:mid", 0],
                               ["turn", None], ["turn:mid", 2]])

    def test_every_runner_kind_has_a_row_or_a_home(self):
        """The kinds the runners emit that are not turn content (auth,
        api_retry, resumed, rollover, review_gate, a tool_call with no
        card) each get a readable line, never raw JSON or silence."""
        got = self.run_node("""
const E = (seq, kind, payload) => ({seq, ts: 1000 + seq, kind, payload});
const evs = [
  E(1, "runner", {kind: "sdk"}), E(2, "system", {subtype: "resumed", session_id: "s"}),
  E(3, "system", {subtype: "api_retry", error_status: 401, error: "authentication_failed", attempt: 1}),
  E(4, "auth", {account: "host", kind: "claude-login", reason: "login_required", detail: "OAuth session expired", action: "run claude auth login"}),
  E(5, "rollover", {phase: "start", reason: "max_age", session_id: "s"}),
  E(6, "review_gate", {turn: 3, held: 5, kept: 4, dropped: 1, pending: 0, error: null}),
  E(7, "tool_call", {tool: "reply", command: "", is_error: true, ms: 4}),
  E(8, "tool_call", {tool: "memory", command: "search", is_error: false, ms: 4}),
];
const blocked = rpModel(evs).strip.auth;
const m = rpModel(evs.concat([E(9, "auth", {account: "host", restored: true})]));
process.stdout.write(JSON.stringify({
  rows: m.rows.map(r => r.t === "boot" ? ["boot", r.events.length] : [r.t, r.cls, r.text]),
  blocked: !!blocked, cleared: m.strip.auth,
  retry: rpAuthLine({retry: "401"}).cls,
  mismatch: rpAuthLine({mismatch: true, expected: "none", got: "ANTHROPIC_API_KEY"}),
}));""")
        self.assertEqual(got["rows"], [
            ["boot", 2],
            ["line", "rp-err", "API retry 1: 401 authentication_failed"],
            ["line", "rp-err", "login required · OAuth session expired · run claude auth login"],
            ["line", "rp-roll", "session rollover · start · max age"],
            ["line", "rp-dim", "memory review · 4 kept, 1 dropped"],
            ["line", "rp-err", "tool reply failed"],
            ["line", "rp-ok", "login restored · host"],
        ])
        self.assertTrue(got["blocked"])
        self.assertIsNone(got["cleared"])
        self.assertEqual(got["retry"], "rp-warn")
        self.assertEqual(got["mismatch"], {"cls": "rp-err", "blocking": True,
                                           "text": "credentials mismatch · expected none, got ANTHROPIC_API_KEY"})

    TASKS = """
const E = (seq, kind, payload) => ({seq, ts: 1000 + seq, kind, payload});
const S = (seq, payload) => E(seq, "system", payload);
"""

    def test_the_bg_tasks_list_folds_from_started_and_ended_events(self):
        """#129: task_started adds a running task, task_progress names its
        last tool, a task_notification ends it; the count is the running
        ones; no task event is a row; an end for an unknown task is
        ignored."""
        got = self.run_node(self.TASKS + """
const m = rpModel([
  S(1, {subtype: "task_started", task_id: "a", description: "Audit the docs", task_type: "local_agent", tool_use_id: "tu1"}),
  S(2, {subtype: "task_started", task_id: "b", description: "npm test", task_type: "local_bash"}),
  S(3, {subtype: "task_progress", task_id: "a", last_tool_name: "Grep", usage: {total_tokens: 9}}),
  S(4, {subtype: "task_notification", task_id: "b", status: "completed", summary: "all green"}),
  S(5, {subtype: "task_notification", task_id: "zz", status: "failed"}),
  S(6, {subtype: "task_updated", task_id: "zz", status: "killed"}),
]);
const l = rpTaskList(m.strip.tasks, 5);
process.stdout.write(JSON.stringify({bg: m.strip.bg, rows: m.rows.length,
  running: l.running.map(t => [t.id, t.desc, rpTaskType(t.type), t.started, t.tool, t.status]),
  ended: l.ended.map(t => [t.id, rpTaskType(t.type), t.status, t.ended, t.summary]),
  ids: Object.keys(m.strip.tasks).sort()}));""")
        self.assertEqual(got["bg"], 1)
        self.assertEqual(got["rows"], 0)
        self.assertEqual(got["running"], [["a", "Audit the docs", "agent", 1001, "Grep", "running"]])
        self.assertEqual(got["ended"], [["b", "shell", "completed", 1004, "all green"]])
        self.assertEqual(got["ids"], ["a", "b"])

    def test_a_terminal_task_updated_ends_a_task_without_a_notification(self):
        """The SDK may end a task only with a task_updated whose status is
        terminal (a TaskStop reports `killed`); a non-terminal or status-less
        update changes nothing but the status; a later notification keeps
        the first end time."""
        got = self.run_node(self.TASKS + """
const evs = [
  S(1, {subtype: "task_started", task_id: "a", description: "watch", task_type: "local_bash"}),
  S(2, {subtype: "task_updated", task_id: "a"}),
  S(3, {subtype: "task_updated", task_id: "a", status: "running"}),
];
const mid = rpModel(evs).strip;
const killed = rpModel(evs.concat([S(4, {subtype: "task_updated", task_id: "a", status: "killed"})])).strip;
const late = rpModel(evs.concat([S(4, {subtype: "task_updated", task_id: "a", status: "killed"}),
                                 S(9, {subtype: "task_notification", task_id: "a", status: "stopped", summary: "stopped by TaskStop"})])).strip;
const restarted = rpModel(evs.concat([E(7, "runner", {kind: "sdk"})])).strip;
process.stdout.write(JSON.stringify({
  mid: [mid.bg, mid.tasks.a.status, mid.tasks.a.ended],
  killed: [killed.bg, killed.tasks.a.status, killed.tasks.a.ended],
  late: [late.bg, late.tasks.a.status, late.tasks.a.ended, late.tasks.a.summary],
  restarted: [restarted.bg, restarted.tasks.a.status, restarted.tasks.a.ended]}));""")
        self.assertEqual(got["mid"], [1, "running", None])
        self.assertEqual(got["killed"], [0, "killed", 1004])
        self.assertEqual(got["late"], [0, "stopped", 1004, "stopped by TaskStop"])
        self.assertEqual(got["restarted"], [0, "lost", 1007])

    def test_an_old_stream_with_no_task_ids_keeps_the_count(self):
        """A stream recorded before 1.27 has the subtype alone: the count
        goes up on task_started and down on task_notification, as before,
        and a bare task_updated or task_progress changes nothing."""
        got = self.run_node(self.TASKS + """
const m = rpModel([
  S(1, {subtype: "task_started"}), S(2, {subtype: "task_started"}), S(3, {subtype: "task_progress"}),
  S(4, {subtype: "task_updated"}), S(5, {subtype: "task_notification"}),
  S(6, {subtype: "task_started", task_id: "n", description: "new"}),
]).strip;
const drained = rpModel([S(1, {subtype: "task_notification"}), S(2, {subtype: "task_notification"})]).strip;
process.stdout.write(JSON.stringify({bg: m.bg, untracked: m.bgUntracked, tasks: Object.keys(m.tasks),
                                     drained: [drained.bg, drained.bgUntracked]}));""")
        self.assertEqual(got, {"bg": 2, "untracked": 1, "tasks": ["n"], "drained": [0, 0]})

    def test_the_bg_chip_is_a_button_that_opens_the_list(self):
        strip = self.chat[self.chat.index("function RpBgTasks("):self.chat.index("function RpStrip(")]
        self.assertIn('<button type="button" className={"rp-chip rp-bg-chip"', strip)
        self.assertIn("setOpen(o => !o)", strip)
        self.assertIn('addEventListener("mousedown", away)', strip)
        self.assertIn("rpTaskList(strip.tasks, 5)", strip)
        self.assertIn("<RpBgTasks strip={strip} />", self.chat)
        for rule in (".rp-bg-chip", ".rp-tasks {", ".rp-task {", ".rp-task-desc"):
            self.assertIn(rule, self.css)

    def test_thinking_ticks_are_compacted_before_the_keep_cap(self):
        got = self.run_node("""
const T = (ts) => ({kind: "system", ts, payload: {subtype: "thinking_tokens"}});
const out = rpCompact([T(1)], [T(2), T(3), {kind: "text", ts: 4, payload: {}}, T(5)]);
process.stdout.write(JSON.stringify(out.map(e => [e.kind, e.ts, e.n || 1, e.last_ts || null])));""")
        self.assertEqual(got, [["system", 1, 3, 3], ["text", 4, 1, None], ["system", 5, 1, None]])

    def test_the_pane_holds_the_scroll_and_keeps_raw(self):
        pane = self.chat[self.chat.index("function RunnerPaneView("):]
        pane = pane[:pane.index("\n}\n")]
        self.assertIn("if (atBottomRef.current) toBottom();", pane)
        self.assertIn("else setBehind(true);", pane)
        self.assertIn('<button className="rp-jump" onClick={toBottom}>', pane)
        self.assertIn("rpCompact(prev, add).slice(-RUNNER_PANE_KEEP)", pane)
        self.assertIn('localStorage.setItem("fw_rp_raw"', pane)
        self.assertIn("<RpStrip strip={model.strip}", pane)
        self.assertIn("@keyframes rp-spin", self.css)


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
