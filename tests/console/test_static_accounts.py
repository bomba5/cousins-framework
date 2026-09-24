"""Static contract for the Accounts view and the Inspector's account panel
(accounts.jsx, WP-C): registered through the seams only, every secret box
a write-only SecretField, the code posted to its own route and never to a
chat, the sign-in URL opened without a referrer, remove behind the typed
name, validate saying it spends one model turn, and every route it calls
in docs/reference/console-api.md."""
import pathlib
import re
import shutil
import subprocess
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "cousin_lib" / "console_static"


def _component(text, name):
    start = text.index("function %s(" % name)
    rest = text[start + 1:]
    m = re.search(r"^(function \w+\(|class \w+ |const \w+ = |registerView\()", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


class AccountsJsx(unittest.TestCase):
    def setUp(self):
        self.src = (_STATIC / "accounts.jsx").read_text(encoding="utf-8")

    def test_registered_through_the_seams(self):
        self.assertRegex(self.src, r'registerView\(\{ id: "accounts", label: "Accounts"')
        self.assertIn('registerSlot("inspector.panels", { id: "account"', self.src)
        self.assertIn("<AccountsView {...props} />", self.src)
        self.assertIn("<CousinAccountPanel cousin={cousin} />", self.src)

    def test_every_secret_box_is_a_secret_field(self):
        self.assertNotIn('type="password"', self.src)
        login = _component(self.src, "AccountLogin")
        self.assertGreaterEqual(login.count("<SecretField"), 3)     # token, key, provider key
        flow = _component(self.src, "AccountFlow")
        self.assertIn("<SecretField", flow)

    def test_the_code_goes_to_its_own_route_never_a_chat(self):
        flow = _component(self.src, "AccountFlow")
        self.assertIn('accountPost(url + "/code", { code })', flow)
        for chat in ("/say", "/api/chat", "/send", "cousin-reply"):
            self.assertNotIn(chat, self.src)
        # the code box shows only while the CLI waits for it
        self.assertIn("flow.awaiting_code", flow)

    def test_the_url_opens_without_a_referrer(self):
        flow = _component(self.src, "AccountFlow")
        self.assertIn('target="_blank" rel="noopener noreferrer"', flow)

    def test_remove_needs_the_typed_name(self):
        remove = _component(self.src, "AccountRemove")
        self.assertIn("disabled={typed !== account.name}", remove)
        self.assertIn("{ confirm: typed }", remove)

    def test_host_login_needs_its_own_confirmation(self):
        login = _component(self.src, "AccountLogin")
        self.assertIn("confirm_host: true", login)
        self.assertIn("account.implicit && !hostOk", login)

    def test_validate_says_it_spends_a_turn(self):
        panel = _component(self.src, "CousinAccountPanel")
        self.assertIn("spends one model turn", panel)
        self.assertIn("window.confirm", panel)
        self.assertIn('c.lane === "sdk"', panel)
        self.assertIn("/check-auth`, { validate }", panel)

    def test_the_login_op_is_read_from_the_account_route(self):
        hook = _component(self.src, "useAccountOp")
        self.assertIn("/api/accounts/${encodeURIComponent(name)}/op", hook)
        self.assertIn('"fw-cousin-op"', hook)
        self.assertIn('const ACCOUNT_OP_PREFIX = "account:";', self.src)

    def test_no_em_dash_and_no_colour_literal(self):
        self.assertNotIn("—", self.src)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(|oklch\(", self.src))

    def test_every_route_is_documented(self):
        api = (_ROOT / "docs" / "reference" / "console-api.md").read_text(encoding="utf-8")
        for route in ("GET /api/accounts`", "GET /api/accounts/<name>/status",
                      "POST /api/accounts`", "POST /api/accounts/<name>`",
                      "POST /api/accounts/<name>/remove", "POST /api/accounts/<name>/key",
                      "POST /api/accounts/<name>/login", "POST /api/accounts/<name>/token",
                      "GET /api/accounts/<name>/flow", "GET /api/accounts/<name>/op",
                      "POST /api/accounts/<name>/code", "POST /api/accounts/<name>/cancel",
                      "POST /api/cousins/<slug>/check-auth"):
            self.assertIn(route, api, route)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class AccountsJsxRuns(unittest.TestCase):
    def test_the_entry_builder(self):
        src = (_STATIC / "accounts.jsx").read_text(encoding="utf-8")
        body = _component(src, "accountEntryFrom")
        consts = re.search(r"const ACCOUNT_INT_FIELDS = [^\n]+\n", src).group(0)
        out = subprocess.run(["node", "-e", consts + body + """
process.stdout.write(JSON.stringify([
  accountEntryFrom("opencode", {providers: " openai, mistral ,", data_dir: ""}),
  accountEntryFrom("opencode", {endpoint: "http://h/v1", endpoint_model: "m",
                                endpoint_context: "32768", endpoint_output: ""}),
]));"""], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        import json
        self.assertEqual(json.loads(out.stdout), [
            {"kind": "opencode", "providers": ["openai", "mistral"]},
            {"kind": "opencode", "endpoint": "http://h/v1", "endpoint_model": "m",
             "endpoint_context": 32768}])


if __name__ == "__main__":
    unittest.main()
