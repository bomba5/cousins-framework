"""The Language routes (routes_language.py): check text, read and write
config/plain-language.toml, a cousin's reply mode through its settings,
and the paths a request may name."""
import tomllib
import unittest

from cousin_lib import plain_language as pl
from tests.console._harness import ConsoleCase

DASH = "\u2014"


class TestLanguageRoutes(ConsoleCase):
    def test_check_returns_findings_and_the_fixed_text(self):
        self.serve()
        status, body = self.post("/api/language/check", {"text": "a %s b\n\nIt was written." % DASH})
        self.assertEqual(status, 200, body)
        self.assertEqual([(f["rule"], f["line"], f["severity"]) for f in body["findings"]],
                         [("em-dash", 1, "error"), ("passive", 3, "note")])
        self.assertEqual(body["fixed"], "a - b\n\nIt was written.")
        self.assertEqual(self.post("/api/language/check", {"text": 3})[0], 400)
        self.assertEqual(self.post("/api/language/check",
                                   {"text": "x" * (200_001)})[0], 400)

    def test_check_takes_a_glossary_inside_the_install_only(self):
        self.serve()
        (self.root / "g.md").write_text("### reply\n\nan answer.\nDo not use: response\n")
        status, body = self.post("/api/language/check", {"text": "A response.", "glossary": "g.md"})
        self.assertEqual(status, 200, body)
        self.assertEqual([f["rule"] for f in body["findings"]], ["glossary"])
        self.assertEqual(body["glossary_terms"], 1)
        status, body = self.post("/api/language/check", {"text": "x", "glossary": "/etc/hostname"})
        self.assertEqual(status, 400)
        self.assertIn("outside", body["error"])
        self.assertEqual(self.post("/api/language/check",
                                   {"text": "x", "glossary": "../escape.md"})[0], 400)
        self.assertEqual(self.post("/api/language/check",
                                   {"text": "x", "glossary": "missing.md"})[0], 400)

    def test_config_get_put_and_refusal(self):
        self.cousin("wren")
        self.cousin("kestrel", extra='\n[plain_language]\nreply = "fix"\n')
        self.serve()
        status, body = self.get("/api/language/config")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["exists"])
        self.assertEqual(body["config"], pl.default_config())
        self.assertEqual([(c["slug"], c["reply"], c["set"]) for c in body["cousins"]],
                         [("kestrel", "fix", True), ("wren", "off", False)])
        cfg = pl.default_config()
        cfg.update(reply_default="note", banned=[{"word": "leverage", "use": "use"}])
        status, body = self.request("PUT", "/api/language/config", {"config": cfg})
        self.assertEqual(status, 200, body)
        written = tomllib.loads((self.root / "config" / pl.CONFIG_NAME).read_text())
        self.assertEqual(written["banned"], [{"word": "leverage", "use": "use"}])
        _, body = self.get("/api/language/config")
        self.assertTrue(body["exists"])
        self.assertEqual(body["cousins"][1]["reply"], "note")
        status, body = self.request("PUT", "/api/language/config",
                                    {"config": {"limits": {"step": 0}}})
        self.assertEqual(status, 400)
        self.assertIn("limits.step", body["error"])
        self.assertEqual(self.request("PUT", "/api/language/config",
                                      {"config": {"glossary": "/etc/hostname"}})[0], 400)
        self.assertEqual(pl.load_config(self.root)["reply_default"], "note")
        # the check uses what was written
        _, body = self.post("/api/language/check", {"text": "We leverage it."})
        self.assertEqual([f["rule"] for f in body["findings"]], ["banned-word"])

    def test_a_broken_config_reads_as_the_defaults_with_its_error(self):
        (self.root / "config" / pl.CONFIG_NAME).write_text("[limits\n")
        self.serve()
        _, body = self.get("/api/language/config")
        self.assertIn("does not parse", body["error"])
        self.assertEqual(self.post("/api/language/check", {"text": "x"})[0], 400)

    def test_the_reply_mode_is_a_cousin_setting(self):
        home = self.cousin("wren")
        self.serve()
        _, body = self.get("/api/cousins/wren/settings")
        row = body["fields"]["plain_language.reply"]
        self.assertEqual((row["value"], row["set"], row["choices"], row["install"], row["restart"]),
                         (None, False, ["off", "note", "fix"], "off", False))
        status, body = self.post("/api/cousins/wren/settings",
                                 {"changes": {"plain_language.reply": "note"}})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["restart_required"])
        self.assertEqual(pl.reply_mode(home, self.root), "note")
        status, body = self.post("/api/cousins/wren/settings",
                                 {"changes": {"plain_language.reply": "loud"}})
        self.assertEqual(status, 400)
        self.assertIn("off, note, fix", body["errors"]["plain_language.reply"])
        self.post("/api/cousins/wren/settings", {"changes": {"plain_language.reply": None}})
        written = tomllib.loads((home / "cousin.toml").read_text())
        self.assertNotIn("reply", written.get("plain_language", {}))


class TestGlossaryRoutes(ConsoleCase):
    def setUp(self):
        super().setUp()
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("conversation_id = conversation_of(thread)  # a thread\n")
        (repo / "b.md").write_text("A conversation.\n")
        (self.root / "g.md").write_text("| Term | Meaning | Do not use |\n|---|---|---|\n"
                                        "| thread | a chat | conversation |\n")

    def test_terms(self):
        self.serve()
        status, body = self.get("/api/language/terms?path=repo&top=2&glossary=g.md")
        self.assertEqual(status, 200, body)
        self.assertEqual([(t["term"], t["code"], t["docs"], t["glossary"]) for t in body["terms"]],
                         [("conversation", 2, 1, "avoid"), ("thread", 1, 1, "term")])
        self.assertEqual((body["files"], body["truncated"]), (2, False))
        self.assertEqual(self.get("/api/language/terms?path=/etc")[0], 400)
        self.assertEqual(self.get("/api/language/terms?path=repo/../..")[0], 400)
        self.assertEqual(self.get("/api/language/terms?path=nope")[0], 404)
        self.assertEqual(self.get("/api/language/terms?path=repo&top=0")[0], 400)
        self.assertEqual(self.get("/api/language/terms")[0], 400)

    def test_secrets_fifos_and_the_checkout(self):
        import os
        from unittest import mock
        from cousin_lib.console import routes_language
        home = self.cousin("wren")
        (home / ".secrets").mkdir()
        (home / ".secrets" / "g.md").write_text("| Term | Meaning | Do not use |\n|---|---|---|\n"
                                                "| key | the token is abc | - |\n")
        os.mkfifo(self.root / "pipe")
        self.serve()
        for body in ({"path": "cousins/wren/.secrets", "groups": ["a,b"]},
                     {"path": "repo", "glossary": "cousins/wren/.secrets/g.md"}):
            status, answer = self.post("/api/language/derive", body)
            self.assertEqual(status, 400, answer)
            self.assertIn("secrets", answer["error"])
        status, answer = self.post("/api/language/check", {"text": "x", "glossary": "pipe"})
        self.assertEqual(status, 400)
        self.assertIn("not a regular file", answer["error"])
        self.assertEqual(self.get("/api/language/terms?path=pipe")[1]["files"], 0)
        with mock.patch("cousin_lib.version.CHECKOUT", self.root / "repo"):
            self.assertNotIn(self.root / "repo", routes_language.allowed_bases(self.root / "x"))
        self.assertIn(routes_language._checkout(), routes_language.allowed_bases(self.root))

    def test_groups_and_derive_read_only(self):
        self.serve()
        before = sorted(p.name for p in (self.root / "repo").iterdir()) + sorted(self.root.glob("*.md"))
        status, body = self.post("/api/language/groups",
                                 {"path": "repo", "glossary": "g.md", "groups": ["of,from"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["flagged"], 1)
        first = body["groups"][0]
        self.assertEqual((first["term"], first["dominant"], first["flagged"]),
                         ("thread", "conversation", True))
        status, body = self.post("/api/language/derive", {"path": "repo", "glossary": "g.md"})
        self.assertEqual(status, 200, body)
        self.assertIn("| conversation | a chat | thread | 2 code, 1 docs |", body["text"])
        self.assertEqual(sorted(p.name for p in (self.root / "repo").iterdir())
                         + sorted(self.root.glob("*.md")), before)
        self.assertEqual(self.post("/api/language/groups", {"path": "repo"})[0], 400)
        self.assertEqual(self.post("/api/language/groups",
                                   {"path": "repo", "groups": "a,b"})[0], 400)
        self.assertEqual(self.post("/api/language/derive",
                                   {"path": "/tmp", "groups": ["a,b"]})[0], 400)


if __name__ == "__main__":
    unittest.main()
