"""The auth-mode routes are gone with the legacy lane's [runtime] auth:
GET and POST /api/cousins/<slug>/auth and POST .../auth/key answer 404,
and a fleet row carries no `auth` field. A runner cousin's credential is
its account (the accounts routes)."""
from tests.console._harness import ConsoleCase


class AuthModeRoutesRetired(ConsoleCase):
    def test_the_three_routes_are_404(self):
        self.cousin("wren")
        self.serve()
        self.assertEqual(self.get("/api/cousins/wren/auth")[0], 404)
        self.assertEqual(self.post("/api/cousins/wren/auth", {"mode": "claude"})[0], 404)
        self.assertEqual(self.post("/api/cousins/wren/auth/key", {"key": "k" * 20})[0], 404)
        self.assertFalse((self.root / "cousins" / "wren" / ".secrets").exists())

    def test_a_fleet_row_has_no_auth_field(self):
        self.cousin("wren")
        self.serve()
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertNotIn("auth", row)
