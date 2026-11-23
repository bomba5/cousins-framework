"""routes_agent: [agent] env_secrets values through the console. A value is
written once (POST /api/cousins/<slug>/env-secrets/<NAME>) through the one
secret writer, shown only as set/last4 in GET .../agent, refused without a
logged-in user, and removed (DELETE) only once no name lists it. The
.mcp.json page counts a listed secret as set. The invented cast."""
import json
import os
import stat
from unittest import mock

from cousin_lib import accounts
from tests.console._harness import ConsoleCase

VALUE = "lin_api_0123456789abcdef"
MCP = {"mcpServers": {"ledger": {"type": "http", "url": "https://ledger.invalid/mcp",
                                 "headers": {"Authorization": "Bearer ${LEDGER_KEY}"}}}}


class EnvSecretRoutes(ConsoleCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(accounts, "_inside_cousin_ancestry", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("COUSIN_HOME", "COUSIN_SLUG", "LEDGER_KEY"):
            os.environ.pop(name, None)
        from cousin_lib.console import auth
        auth.Users(self.root / "config" / "console-users.json").set_password("ana", "correct horse")
        self.home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()

    def login(self):
        status, body = self.post("/api/auth/login", {"user": "ana", "password": "correct horse"})
        self.assertEqual(status, 200, body)

    def file(self):
        return self.home / ".secrets" / "env" / "LEDGER_KEY"

    def test_writing_wants_a_logged_in_user(self):
        status, body = self.post("/api/cousins/wren/env-secrets/LEDGER_KEY", {"value": VALUE})
        self.assertEqual(status, 401, body)
        self.assertFalse(self.file().exists())

    def test_a_value_is_written_private_then_listed_and_shown_without_it(self):
        self.login()
        status, body = self.post("/api/cousins/wren/agent", {"changes": {"env_secrets": ["LEDGER_KEY"]}})
        self.assertEqual(status, 400, body)                      # no value yet
        status, body = self.post("/api/cousins/wren/env-secrets/LEDGER_KEY", {"value": VALUE})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["secret"]["set"])
        self.assertFalse(body["listed"])
        self.assertEqual(self.file().read_text().strip(), VALUE)
        self.assertEqual(stat.S_IMODE(self.file().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.file().parent.stat().st_mode), 0o700)
        status, body = self.post("/api/cousins/wren/agent", {"changes": {"env_secrets": ["LEDGER_KEY"]}})
        self.assertEqual(status, 200, body)
        status, body = self.get("/api/cousins/wren/agent")
        row = body["settings"]["env_secrets"]
        self.assertEqual(row["value"], ["LEDGER_KEY"])
        self.assertEqual(row["secrets"]["LEDGER_KEY"], {"set": True, "last4": "cdef", "error": None})
        self.assertNotIn(VALUE, json.dumps(body))

    def test_a_reserved_or_bad_name_is_refused(self):
        self.login()
        for name in ("ANTHROPIC_API_KEY", "PATH", "bad-name"):
            with self.subTest(name=name):
                status, body = self.post("/api/cousins/wren/env-secrets/%s" % name, {"value": VALUE})
                self.assertEqual(status, 400, body)
        self.assertFalse((self.home / ".secrets" / "env").exists())

    def test_a_listed_value_is_not_removed(self):
        self.login()
        self.post("/api/cousins/wren/env-secrets/LEDGER_KEY", {"value": VALUE})
        self.post("/api/cousins/wren/agent", {"changes": {"env_secrets": ["LEDGER_KEY"]}})
        status, body = self.delete("/api/cousins/wren/env-secrets/LEDGER_KEY")
        self.assertEqual(status, 409, body)
        self.assertTrue(self.file().exists())
        status, body = self.post("/api/cousins/wren/agent", {"changes": {"env_secrets": None}})
        self.assertEqual(status, 200, body)
        row = self.get("/api/cousins/wren/agent")[1]["settings"]["env_secrets"]
        self.assertIn("LEDGER_KEY", row["secrets"])            # a stray file is shown
        status, body = self.delete("/api/cousins/wren/env-secrets/LEDGER_KEY")
        self.assertEqual(status, 200, body)
        self.assertFalse(self.file().exists())

    def test_the_mcp_page_counts_a_listed_secret_as_set(self):
        self.login()
        (self.home / ".mcp.json").write_text(json.dumps(MCP))
        row = self.get("/api/cousins/wren/mcp/servers")[1]["servers"][0]
        self.assertEqual(row["unset_vars"], ["LEDGER_KEY"])
        self.post("/api/cousins/wren/env-secrets/LEDGER_KEY", {"value": VALUE})
        self.post("/api/cousins/wren/agent", {"changes": {"env_secrets": ["LEDGER_KEY"]}})
        row = self.get("/api/cousins/wren/mcp/servers")[1]["servers"][0]
        self.assertEqual(row["unset_vars"], [])
        self.assertEqual(row["secret_vars"], ["LEDGER_KEY"])


class OpenConsole(ConsoleCase):
    def test_a_console_with_no_users_still_wants_one_for_a_secret(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/env-secrets/LEDGER_KEY", {"value": VALUE})
        self.assertEqual(status, 403, body)
        self.assertIn("cousin-console adduser", body["error"])
        self.assertFalse((home / ".secrets").exists())
