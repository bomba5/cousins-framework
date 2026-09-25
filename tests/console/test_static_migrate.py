"""migrate.jsx (WP-B): the kind switch dialog, the migration and lifecycle
panels. Pinned by text where the contract is a string (the event the
dialog opens on, the routes, the confirmations, the words that say what
a step spends), and the pure helpers run under node."""
import json
import pathlib
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


class MigrateJsx(unittest.TestCase):
    def setUp(self):
        self.src = _read("migrate.jsx")

    def test_panels_register_through_the_seams(self):
        self.assertIn('registerSlot("inspector.panels", { id: "migrate"', self.src)
        self.assertIn('registerSlot("inspector.panels", { id: "lifecycle"', self.src)

    def test_the_dialog_opens_on_the_window_event_and_is_mounted_once(self):
        self.assertIn('window.addEventListener("fw-open-kind-switch"', self.src)
        self.assertIn('new CustomEvent("fw-open-kind-switch", { detail: { slug } })', self.src)
        self.assertIn("ReactDOM.createRoot(host).render(<KindSwitchHost />)", self.src)

    def test_it_calls_the_package_routes(self):
        for path in ("/migrate`", "/migrate/plan", "/migrate/apply", "/migrate/check",
                     "/migrate/rollback", "/reincarnate", "/api/lifecycle/transplant",
                     "/lifecycle`",
                     "/api/lifecycle/modes", "/api/accounts"):
            self.assertIn(path, self.src, path)

    def test_validate_says_it_spends_a_model_turn(self):
        self.assertIn("validate: spends one model turn", self.src)
        self.assertIn("validate (spends one model turn)", self.src)

    def test_the_deferred_items_say_not_yet(self):
        self.assertIn("<DeferredList items={state.deferred} />", self.src)
        self.assertIn("not yet:", self.src)

    def test_destructive_steps_ask_twice_or_are_typed(self):
        self.assertIn("click again: this stops and restarts", self.src)
        self.assertIn("confirm: true", self.src)
        self.assertIn("force_confirm = true", self.src)
        self.assertIn("Click again to force", self.src)
        self.assertIn("type <code>{phrase}</code> to confirm", self.src)
        self.assertIn("confirmPhrase(info.phrase, donor, recipient)", self.src)
        self.assertIn("confirm: typedMode ? typed : true", self.src)
        self.assertIn("click again: rewrite the role and flip", self.src)

    def test_the_trust_screen_opens_the_pane_one_input_at_a_time(self):
        self.assertIn("window.PaneView", self.src)
        self.assertIn('tmuxSession: "tmux-" + slug }} onClose={onClose} serial />', self.src)
        self.assertIn("open the pane to accept the trust dialog", self.src)
        self.assertIn("finish it in a terminal", self.src)
        chat = _read("chat.jsx")
        self.assertIn("function PaneView({ cousin, onClose, serial })", chat)
        self.assertIn("if (serialRef.current && sendingRef.current) return;", chat)

    def test_screens_kinds_and_op_kinds_come_from_the_server(self):
        for key in ("state.person_screens", "state.pane_answers", "state.pane_kinds",
                    "state.op_kinds", "meta.op_kinds", "info.phrase"):
            self.assertIn(key, self.src, key)
        for literal in ('"trust", "onboarding"', '"kind-switch"', '"reincarnate", "transplant"',
                        '=== "tmux"', "swap ${"):
            self.assertNotIn(literal, self.src, literal)

    def test_errors_are_shown_and_never_leave_a_button_busy(self):
        self.assertIn("the console did not answer: ", self.src)
        self.assertIn("<MigLoadError error={loadError} />", self.src)
        self.assertNotIn("apiGet(", self.src)
        self.assertIn("[planOpId, op && op.id, op && op.status]", self.src)
        self.assertIn("[opId, op && op.id, op && op.status]", self.src)

    def test_the_donor_shows_it_is_held(self):
        self.assertIn("/lifecycle`", self.src)
        self.assertIn("held as the donor of a", self.src)

    def test_no_colour_literals_and_no_em_dashes(self):
        self.assertNotRegex(self.src, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|oklch\(")
        self.assertNotIn("\u2014", self.src)

    def test_index_loads_it_in_the_package_block(self):
        html = _read("index.html")
        self.assertIn('<script type="text/babel" src="migrate.jsx"></script>', html)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Helpers(unittest.TestCase):
    def run_node(self, body):
        src = _read("migrate.jsx")
        start = src.index("// ---- pure helpers")
        end = src.index("// ---- end pure helpers")
        out = subprocess.run(["node", "-e", src[start:end] + body],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_stage_rows_follow_the_plan_and_mark_the_rest_pending(self):
        got = self.run_node("""
          const op = {status: "running", params: {steps: ["close", "toml", "verify"]},
                      stages: [{name: "close", status: "done", detail: "ok"},
                               {name: "extra", status: "done"}]};
          const ended = Object.assign({}, op, {status: "failed"});
          console.log(JSON.stringify([migrateStageRows(op).map(r => [r.name, r.status]),
                                      migrateStageRows(ended).map(r => r.status),
                                      migrateStageRows(null)]));
        """)
        self.assertEqual(got[0], [["close", "done"], ["toml", "pending"], ["verify", "pending"],
                                  ["extra", "done"]])
        self.assertEqual(got[1], ["done", "not run", "not run", "done"])
        self.assertEqual(got[2], [])

    def test_the_pane_screen_comes_from_a_running_detail_or_the_login_state(self):
        got = self.run_node("""
          const waiting = {stages: [{name: "verify", status: "running",
            detail: "waiting for the operator to accept the trust dialog in the pane (screen: trust)"}]};
          const done = {stages: [{name: "verify", status: "done", detail: "(screen: trust)"}]};
          const screens = ["trust", "login", "bypass"];
          console.log(JSON.stringify([
            paneWaitScreen(waiting, null, screens), paneWaitScreen(done, null, screens),
            paneWaitScreen(null, {screen: "trust"}, screens),
            paneWaitScreen(null, {screen: "limit"}, screens),
            paneWaitScreen(null, null, screens), paneWaitScreen(waiting, null, []),
            paneAction("trust", ["trust", "bypass"]), paneAction("login", ["trust"]),
            paneAction(null, ["trust"])]));
        """)
        self.assertEqual(got, ["trust", None, "trust", None, None, None, "pane", "terminal", None])

    def test_rollback_offers_follow_the_records(self):
        got = self.run_node("""
          console.log(JSON.stringify([
            rollbackOffers({switch: {state: "switched", from: "sdk", to: "tmux"}, migration: null}),
            rollbackOffers({switch: {state: "rolled_back", from: "sdk"}, migration: {state: "migrated"}}),
            rollbackOffers({switch: null, migration: {state: "rolled_back"}})]));
        """)
        self.assertEqual([(o["which"], o["to"]) for o in got[0]], [("switch", "sdk")])
        self.assertEqual([o["which"] for o in got[1]], ["migration"])
        self.assertEqual(got[2], [])

    def test_bodies_and_the_confirm_phrase(self):
        got = self.run_node("""
          console.log(JSON.stringify([
            migrateBody(true, {account: "team", validate: true, to: "tmux"}),
            migrateBody(true, {account: "", validate: false}),
            migrateBody(false, {to: "tmux", account: "team", validate: true}),
            confirmPhrase("swap {donor} {recipient}", "wren", "owl"), confirmPhrase(null, "a", "b"),
            otherKind("sdk", ["sdk", "tmux"]), otherKind("x", [])]));
        """)
        self.assertEqual(got, [{"validate": True, "account": "team"}, {"validate": False},
                               {"to": "tmux"}, "swap wren owl", "", "tmux", ""])


if __name__ == "__main__":
    unittest.main()
