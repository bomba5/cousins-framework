"""agent.jsx: the Inspector's agent and cousin settings panels, and
the lane-aware bits it gives the Inspector's identity rows, the spawn
dialog and the chat header's effort select. Pinned by text where the
contract is a string (the slot, the routes, the kind-switch event), and
the pure helpers run under node."""
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
    m = re.search(r"^(function \w+\(|class \w+ |const \w+ = )", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


class AgentJsx(unittest.TestCase):
    def setUp(self):
        self.src = _read("agent.jsx")

    def test_the_panel_fills_the_inspector_lane_slot(self):
        self.assertIn('registerSlot("inspector.lane", { id: "agent"', self.src)

    def test_it_calls_its_routes_and_the_fleet_restart(self):
        for path in ("/api/cousins/${c.slug}/agent", "/api/cousins/${c.slug}/settings",
                     "/api/cousins/${cousin.slug}/restart"):
            self.assertIn(path, self.src, path)

    def test_switch_kind_is_wp_bs_dialog_through_a_window_event(self):
        self.assertIn('new CustomEvent("fw-open-kind-switch", { detail: { slug } })', self.src)
        self.assertIn("switch kind", self.src)

    def test_the_kinds_accounts_and_models_come_from_the_server(self):
        # no list of kinds, efforts or models written into the page
        self.assertNotRegex(self.src, r'\[\s*"(sdk|fake|opencode|tmux|low|high)"\s*,')
        for key in ("row.choices", "row.suggestions", "row.kinds", "row.always_primary",
                    "row.base", "row.deny_prefixes"):
            self.assertIn(key, self.src, key)

    def test_no_kind_is_named_in_the_page(self):
        """The lanes, the model shape and the turn come from the
        server (describe()'s tmux_lane, model_rule, model_change_spends_turn)."""
        for kind in ("sdk", "fake", "opencode", "tmux", "tmux-legacy"):
            self.assertNotIn('"%s"' % kind, self.src, kind)
        for key in ("data.tmux_lane", "data.model_rule", "data.model_change_spends_turn",
                    "rule.placeholder", "rule.provider_model"):
            self.assertIn(key, self.src, key)

    def test_the_panel_waits_for_its_own_op_and_polls_as_a_fallback(self):
        panel = _component(self.src, "AgentSettingsPanel")
        self.assertIn("setWaitOp(d.op.id)", panel)
        self.assertIn("agentOpSettled(op, waitOp)", panel)
        self.assertIn("agentOpSettled(d.op, waitOp)", panel)
        self.assertIn("setInterval(", panel)
        self.assertIn("setErrors(r.errors ||", panel)

    def test_it_reloads_on_a_fleet_refresh_and_the_settings_stand_alone(self):
        panel = _component(self.src, "AgentSettingsPanel")
        self.assertIn("[c.lane, c.held, c.effort, c.model, c.account, c.autoStart]", panel)
        self.assertNotIn("<CousinSettingsPanel", panel)
        self.assertIn('registerSlot("inspector.lane", { id: "cousin-settings"', self.src)
        settings = _component(self.src, "CousinSettingsPanel")
        self.assertIn("d.written", settings)
        self.assertIn('"unknown"', settings)

    def test_an_sdk_model_change_is_a_long_operation(self):
        self.assertIn("r.status === 202", self.src)
        self.assertIn('<LongOpStatus slug={c.slug} kind="agent-settings" />', self.src)
        self.assertIn("validates the model (one turn)", self.src)

    def test_the_hold_the_deprecated_key_and_restart_to_apply(self):
        self.assertIn("data.held", self.src)
        self.assertIn("api_key_file is deprecated", self.src)
        self.assertIn("restart to apply", self.src)
        self.assertIn("click again to restart", self.src)

    def test_commit_attribution_shows_the_install_default_and_its_source(self):
        panel = _component(self.src, "CousinSettingsPanel")
        self.assertIn("install.source", panel)
        self.assertIn("attribution.effective", panel)
        self.assertIn("read-only", panel)

    def test_no_colour_literals_and_no_em_dashes(self):
        self.assertNotRegex(self.src, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|oklch\(")
        self.assertNotIn("—", self.src)


class TheOtherFiles(unittest.TestCase):
    def test_the_inspector_shows_model_and_effort_on_the_tmux_legacy_lane_only(self):
        inspector = _component(_read("cousins.jsx"), "Inspector")
        # the name the server gives the lane, never a literal
        self.assertIn("c.lane === options.tmux_lane", inspector)
        self.assertNotIn('"tmux-legacy"', inspector)
        block = inspector[inspector.index("{tmuxLane && <>"):inspector.index("</>}")]
        for token in ('field="model"', 'field="effort"'):
            self.assertIn(token, block, token)

    def test_the_spawn_dialog_offers_the_kinds_and_the_accounts_models(self):
        # the kinds only, the legacy lane not offered
        modal = _component(_read("cousins.jsx"), "SpawnModal")
        self.assertIn("runners.map(k =>", modal)
        self.assertNotIn("tmux_lane", modal)
        self.assertIn(".models || []", modal)
        self.assertIn("laneSuggestions.map", modal)

    def test_the_chat_effort_select_is_shown_where_the_lane_reads_effort(self):
        header = _component(_read("chat.jsx"), "ChatHeader")
        self.assertIn('window.agentLaneReads(cousin.lane, "effort", laneKeys)', header)
        self.assertIn("d.lane_keys", header)
        self.assertIn("readsEffort && (", header)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Helpers(unittest.TestCase):
    def run_node(self, body):
        src = _read("agent.jsx")
        start = src.index("// ---- pure helpers")
        end = src.index("// ---- end pure helpers")
        out = subprocess.run(["node", "-e", src[start:end] + body],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_lane_reads(self):
        got = self.run_node("""
const keys = {sdk: ["model", "effort"], opencode: ["model", "shell_env"], fake: ["account"]};
console.log(JSON.stringify([
  agentLaneReads("sdk", "effort", keys), agentLaneReads("opencode", "effort", keys),
  agentLaneReads("fake", "model", keys), agentLaneReads("tmux-legacy", "effort", keys),
  agentLaneReads(null, "effort", keys), agentLaneReads("tmux-legacy", "account", keys)]));
""")
        self.assertEqual(got, [True, False, False, True, True, False])

    def test_changes(self):
        got = self.run_node("""
const s = {
  effort: {value: "low", set: true}, auto_start: {value: true, set: false},
  model: {value: null, set: false}, runner: {value: "sdk", readonly: true, set: true},
  rollover_at_percent: {value: 80, set: true},
  sessions: {value: {operator: "primary", peer: "primary", meeting: "own"}, set: true}};
console.log(JSON.stringify([
  agentChanges(s, {effort: "low", auto_start: true, runner: "fake"}),
  agentChanges(s, {effort: "high", model: "m-two", rollover_at_percent: null}),
  agentChanges(s, {sessions: {operator: "primary", peer: "own", meeting: "own"}}),
  agentChanges(s, {model: null})]));
""")
        self.assertEqual(got, [{}, {"effort": "high", "model": "m-two",
                                    "rollover_at_percent": None},
                               {"sessions": {"peer": "own"}}, {}])

    def test_op_settled_is_this_op_only(self):
        got = self.run_node("""
console.log(JSON.stringify([
  agentOpSettled({id: "a", status: "done"}, "a"), agentOpSettled({id: "a", status: "done"}, "b"),
  agentOpSettled({id: "b", status: "running"}, "b"), agentOpSettled(null, "b"),
  agentOpSettled({id: "a", status: "failed"}, null)]));
""")
        self.assertEqual(got, [True, False, False, False, False])

    def test_model_problem_follows_the_rule(self):
        got = self.run_node("""
console.log(JSON.stringify([agentModelProblem({provider_model: true}, "gpt-5"),
  agentModelProblem({provider_model: true}, "openai/gpt-5"),
  agentModelProblem({provider_model: false}, "gpt-5"), agentModelProblem(null, "a b")]));
""")
        self.assertIsNotNone(got[0])
        self.assertEqual(got[1:3], [None, None])
        self.assertIsNotNone(got[3])

    def test_env_names_and_the_hard_deny(self):
        got = self.run_node("""
const deny = ["CLAUDE", "ANTHROPIC"];
console.log(JSON.stringify([agentEnvList("A, B  C"), agentEnvProblem("EDITOR", deny),
  agentEnvProblem("CLAUDE_X", deny), agentEnvProblem("1X", deny)]));
""")
        self.assertEqual(got[0], ["A", "B", "C"])
        self.assertIsNone(got[1])
        self.assertIn("hard deny", got[2])
        self.assertIn("not a variable name", got[3])

    def test_cousin_setting_changes_and_flip_at(self):
        got = self.run_node("""
const f = {"cousin.name": {value: "Wren", set: true},
           "lifecycle.flip_at": {value: null, set: false},
           "agent.commit_attribution": {value: false, set: true},
           "cousin.peer_visible": {value: true, set: false}};
console.log(JSON.stringify([
  cousinSettingChanges(f, {"cousin.name": "Wren", "cousin.peer_visible": true}),
  cousinSettingChanges(f, {"lifecycle.flip_at": "05:30", "agent.commit_attribution": null}),
  [flipAtProblem("05:30"), flipAtProblem("never"), flipAtProblem(""), flipAtProblem("25:00")]]));
""")
        self.assertEqual(got[0], {})
        self.assertEqual(got[1], {"lifecycle.flip_at": "05:30",
                                  "agent.commit_attribution": None})
        self.assertEqual(got[2][:3], [None, None, None])
        self.assertIsNotNone(got[2][3])
