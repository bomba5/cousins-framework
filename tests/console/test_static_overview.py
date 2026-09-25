"""The overview (views.jsx HostView), pinned by text and, where node is
installed, by running its pure fleet helpers: one sentence of fleet
health, the fleet table's columns read from the rows (never a hardcoded
runner kind), what needs the operator first, the next flip from the
rows' own flip_at, and the activity rail from the jobs, recent-fires
and flip events the console already serves."""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _component(text, name):
    start = text.index("function %s(" % name)
    rest = text[start + 1:]
    m = re.search(r"^(function \w+\(|const \w+ = )", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


def _helpers():
    views = _read("views.jsx")
    start = views.index("// ---- fleet helpers")
    end = views.index("// ---- end fleet helpers ----")
    return views[start:end]


class OverviewMarkup(unittest.TestCase):
    def setUp(self):
        self.view = _component(_read("views.jsx"), "HostView")

    def test_the_fleet_table_has_the_columns(self):
        heads = re.findall(r"<th[^>]*>([^<]+)</th>", self.view)
        self.assertEqual(heads, ["state", "cousin", "runner", "model", "next flip",
                                 "beat", "operator", "tokens today"])

    def test_columns_come_from_the_row_and_the_helpers(self):
        for token in ("fleetState(c)", "fleetAttention(c)", "fleetRunnerKind(c)",
                      "nextFlip(c.flipAt, now)", "c.model", "c.effort", "c.heartbeat",
                      "c.operator", "c.tokensSpent", "fleetOrder(cousins)"):
            self.assertIn(token, self.view, token)
        # the runner kind is the row's, never a literal list of lanes
        self.assertNotRegex(self.view, r'\[\s*"sdk"\s*,')

    def test_the_state_is_said_in_words_beside_its_dot(self):
        self.assertRegex(self.view, r'className=\{"led " \+ st\.tone[^}]*\}\s*/>\s*\{st\.word\}')

    def test_health_stats_and_rail_are_marked(self):
        for marker in ("data-fleet-health", "data-fleet-stats", "data-fleet-events",
                       "data-fleet-row"):
            self.assertIn(marker, self.view, marker)
        self.assertIn('apiGet("/api/jobs?since_hours=24")', self.view)
        self.assertIn('apiGet("/api/loops/recent")', self.view)

    def test_nothing_the_old_overview_showed_is_gone(self):
        # host meters, the uptimes, the daemon's complaint, and the numbers
        # of the old totals panel all still render
        for token in ("h.cpu", "h.mem", "h.disk", "h.net", "h.console_uptime",
                      "h.uptime", "h.kernel", "daemon.message", "loops.length",
                      "fires.length", "totalTokensToday"):
            self.assertIn(token, self.view, token)

    def test_no_column_is_invented(self):
        # generation and context fill are not on the fleet rows: no
        # column may pretend otherwise
        for word in ("generation", "context", "packet"):
            self.assertNotIn("<th>%s" % word, self.view)

    def test_a_login_wait_is_a_banner_with_its_action_line(self):
        self.assertIn("data-login-required", self.view)
        self.assertIn("fleetLoginWaits(", self.view)
        self.assertIn("loginRequired.action", self.view)

    def test_the_sidebar_carries_the_count_and_the_next_flip(self):
        app = _read("app.jsx")
        self.assertIn("fleetHealth(", app)
        self.assertIn('className="nav-count"', app)
        self.assertIn("data-next-flip", app)
        self.assertIn("<HostView onOpen=", app)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FleetHelpers(unittest.TestCase):
    def run_node(self, body):
        out = subprocess.run(["node", "-e", _helpers() + body], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_runner_kind_is_read_from_the_row(self):
        got = self.run_node("""
process.stdout.write(JSON.stringify([
  fleetRunnerKind({runner: {kind: "sdk"}, tmuxSession: "a"}),
  fleetRunnerKind({runner: {kind: "opencode"}}),
  fleetRunnerKind({runner: {kind: "something-new"}}),
  fleetRunnerKind({runner: {}}),
  fleetRunnerKind({tmuxSession: "wren"}),
  fleetRunnerKind({type: "worker", tmuxSession: "w"}),
  fleetRunnerKind({remote: true}),
  fleetRunnerKind({}),
]));""")
        self.assertEqual(got, ["sdk", "opencode", "something-new", "runner",
                               "tmux", "worker", "remote", "-"])

    def test_attention_and_state_words(self):
        got = self.run_node("""
const rows = [
  {status: "running", attention: "Do you want to proceed?", tmuxSession: "a"},
  {status: "running", runner: {alive: true, state: "waiting_permission"}},
  {status: "running", runner: {alive: true, state: "errored"}},
  {status: "running", runner: {alive: true, state: "rate_limited"}},
  {status: "running", runner: {alive: true, state: "running"}},
  {status: "running", runner: {alive: true, state: "idle"}},
  {status: "running", chat: "down", tmuxSession: "b"},
  {status: "running", active: true, tmuxSession: "c"},
  {status: "stopped", attention: "x", chat: "down"},
  {status: "running", type: "worker"},
];
process.stdout.write(JSON.stringify(rows.map(r => [
  (fleetAttention(r) || {}).level || null, fleetState(r).word, fleetState(r).tone])));""")
        self.assertEqual(got, [
            ["needs", "needs you", "amber"],
            ["needs", "needs you", "amber"],
            ["needs", "errored", "red"],
            ["warn", "rate limited", "amber"],
            [None, "working", "green"],
            [None, "idle", "green"],
            ["warn", "idle", "green"],
            [None, "working", "green"],
            [None, "stopped", "gray"],
            [None, "enrolled", "green"],
        ])

    def test_a_login_wait_needs_you_and_says_its_action(self):
        got = self.run_node("""
const login = {reason: "login_required", action: "cousin-account login fleet", since: "t"};
const rows = [
  {slug: "a", status: "running", runner: {alive: true, state: "idle"}, loginRequired: login},
  {slug: "b", status: "running", runner: {alive: true, state: "idle"},
   loginRequired: {reason: "billing", action: "top up the account", since: "t"}},
  {slug: "c", status: "stopped", loginRequired: login},
  {slug: "d", status: "running", runner: {alive: true, state: "idle"}, loginRequired: null},
  {slug: "e", remote: true, status: "running", loginRequired: login},
];
process.stdout.write(JSON.stringify({
  att: rows.map(r => fleetAttention(r)),
  word: fleetState(rows[0]).word,
  waits: fleetLoginWaits(rows).map(r => r.slug),
}));""")
        self.assertEqual(got["att"][0]["level"], "needs")
        self.assertIn("cousin-account login fleet", got["att"][0]["why"])
        self.assertIn("billing", got["att"][1]["why"])
        self.assertEqual(got["att"][2:], [None, None, None])
        self.assertEqual(got["word"], "needs you")
        # a stopped cousin still waits for its login; a remote row has no file
        self.assertEqual(got["waits"], ["a", "b", "c"])

    def test_a_failing_runner_needs_you_and_says_why(self):
        """Round 4: a runner the supervisor left down `failing` is not an
        operator's stop: it asks for a person, with the supervisor's reason."""
        got = self.run_node("""
const rows = [
  {slug: "a", status: "stopped", supervisor: {state: "failing",
   reason: "the runner gave up on its pane: 5 starts in a row"}},
  {slug: "b", status: "stopped", supervisor: {state: "stopped", reason: "stopped"}},
  {slug: "c", status: "stopped", supervisor: null},
];
process.stdout.write(JSON.stringify(rows.map(r => fleetAttention(r))));""")
        self.assertEqual(got[0]["level"], "needs")
        self.assertIn("gave up on its pane", got[0]["why"])
        self.assertEqual(got[1:], [None, None])

    def test_needs_you_first_then_warnings_then_running_then_stopped(self):
        got = self.run_node("""
const rows = [
  {slug: "a", status: "stopped"},
  {slug: "b", status: "running"},
  {slug: "c", status: "running", chat: "down"},
  {slug: "d", status: "running", attention: "menu"},
  {slug: "e", status: "running"},
];
process.stdout.write(JSON.stringify(fleetOrder(rows).map(r => r.slug)));""")
        self.assertEqual(got, ["d", "c", "b", "e", "a"])

    def test_next_flip_from_the_rows_flip_at(self):
        got = self.run_node("""
const now = new Date(2026, 8, 24, 19, 30, 0).getTime();
const f = (v) => nextFlip(v, now);
process.stdout.write(JSON.stringify({
  later: f("20:15"), tomorrow: f("04:00"), never: f("never"), none: f(null),
  bad: f("25:00"), junk: f("soon"),
}));""")
        self.assertEqual(got["later"]["at"], "20:15")
        self.assertEqual(got["later"]["inSec"], 45 * 60)
        self.assertEqual(got["tomorrow"]["at"], "04:00")
        self.assertEqual(got["tomorrow"]["inSec"], (8 * 60 + 30) * 60)
        self.assertEqual(got["never"], {"never": True})
        self.assertIsNone(got["none"])
        self.assertIsNone(got["bad"])
        self.assertIsNone(got["junk"])

    def test_the_fleet_sentence(self):
        got = self.run_node("""
const now = new Date(2026, 8, 24, 19, 30, 0).getTime();
const rows = [
  {slug: "a", status: "running", flipAt: "04:10", tmuxSession: "a"},
  {slug: "b", status: "running", flipAt: "04:00", attention: "menu", tmuxSession: "b"},
  {slug: "c", status: "running", flipAt: null, tmuxSession: "c"},
  {slug: "d", status: "stopped", flipAt: "03:00", tmuxSession: "d"},
  {slug: "e", status: "running", type: "worker"},
  {slug: "f", status: "running", remote: true},
];
const quiet = [{slug: "q", status: "running", tmuxSession: "q"}];
process.stdout.write(JSON.stringify({busy: fleetHealth(rows, now), quiet: fleetHealth(quiet, now)}));""")
        busy = got["busy"]
        self.assertEqual(busy["text"], "5 of 6 running · 1 needs you · next flip 04:00, in 8h 30m")
        self.assertEqual(busy["nextFlip"]["slugs"], ["b"])
        self.assertEqual(busy["nextFlip"]["onDefault"], 1)
        self.assertEqual((busy["needs"], busy["warn"]), (1, 0))
        # no flip time set anywhere: the sentence says nothing about flips
        self.assertEqual(got["quiet"]["text"], "1 of 1 running · nothing needs you")
        self.assertIsNone(got["quiet"]["nextFlip"])


if __name__ == "__main__":
    unittest.main()
