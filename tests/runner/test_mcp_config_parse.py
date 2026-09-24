"""mcp_config.parse(text): the structural half of reading .mcp.json,
over text the console has not written yet, so an edit is checked by the
same rules the runner reads the file with. Invented names and values."""
import json
import pathlib
import tempfile
import unittest

from cousin_lib.runner import mcp_config
from tests._hermetic import HermeticCase


def _doc(servers):
    return json.dumps({"mcpServers": servers})


class TestParse(HermeticCase):
    def test_parse_matches_read_on_the_same_bytes(self):
        text = _doc({"notes": {"command": "/opt/notes", "args": ["--root", "/srv"]},
                     "ha": {"type": "http", "url": "http://ha.lan/api/mcp"},
                     "cousin": {"command": "cousin-mcp"},
                     "bad": {"type": "ftp"}})
        entries, skipped = mcp_config.parse(text)
        self.assertEqual([(n, k) for n, k, _ in entries], [("ha", "http"), ("notes", "stdio")])
        self.assertEqual([s["name"] for s in skipped], ["bad", "cousin"])
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / ".mcp.json").write_text(text)
            present, r_entries, r_skipped = mcp_config.read(tmp)
        self.assertTrue(present)
        self.assertEqual((r_entries, r_skipped), (entries, skipped))

    def test_text_that_does_not_parse_is_one_file_level_skip(self):
        entries, skipped = mcp_config.parse("{nope")
        self.assertEqual(entries, [])
        self.assertIsNone(skipped[0]["name"])
        self.assertIn("does not parse", skipped[0]["reason"])
        entries, skipped = mcp_config.parse("[]")
        self.assertIn("mcpServers", skipped[0]["reason"])

    def test_variables_lists_every_reference_the_cli_expands(self):
        entry = {"command": "${BIN:-/opt/x}", "args": ["--k", "${A}"],
                 "env": {"TOKEN": "${NOTES_TOKEN}"}}
        self.assertEqual(sorted(mcp_config.variables("stdio", entry)),
                         [("A", False), ("BIN", True), ("NOTES_TOKEN", False)])
        entry = {"url": "${HA_URL}/mcp", "headers": {"Authorization": "Bearer ${HA_TOKEN}"}}
        self.assertEqual(sorted(mcp_config.variables("http", entry)),
                         [("HA_TOKEN", False), ("HA_URL", False)])


if __name__ == "__main__":
    unittest.main()
