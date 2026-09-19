"""Telegram provisioning routes: the token is accepted and never
answered; enabling a stopped cousin's bridge waits for its start."""
import json
import unittest

from tests.console._harness import ConsoleCase

TOKEN = "123456789:" + "B" * 35


class TestTelegramRoutes(ConsoleCase):
    def test_provision_flow_never_leaks_the_token(self):
        self.cousin("wren")
        self.serve()
        status, body = self.get("/api/cousins/wren/telegram")
        self.assertEqual((status, body["enabled"], body["token_set"],
                          body["ready"]), (200, False, False, "disabled"))
        status, body = self.post("/api/cousins/wren/telegram/token",
                                 {"token": TOKEN})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["token_set"])
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertEqual(self.post("/api/cousins/wren/telegram/token",
                                   {"token": "nope"})[0], 400)
        status, body = self.post("/api/cousins/wren/telegram/operators", {
            "operators": [{"user_id": 42, "name": "Ana"}]})
        self.assertEqual(body["operators"], [{"user_id": 42, "name": "Ana"}])
        status, body = self.post("/api/cousins/wren/telegram/enabled",
                                 {"enabled": True})
        self.assertEqual((body["enabled"], body["bridge"], body["running"]),
                         (True, "starts with the cousin", False))
        self.assertIsNone(body["ready"])
        self.assertEqual(self.get("/api/cousins/nobody/telegram")[0], 404)


if __name__ == "__main__":
    unittest.main()
