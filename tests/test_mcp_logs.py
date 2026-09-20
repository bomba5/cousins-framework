"""What the harness recorded about a cousin's own MCP server.

On 2026-09-20 four cousins booted with no MCP tools at all. The boot
packet said only CONNECTION_CLOSED; the reason - the server printing
`registry: meeting: no commands` and exiting 377 ms in - was in the
harness's own log directory, which nothing surfaced. Every cousin fell
back to the CLIs and carried on, because a degraded surface that still
works is the easiest failure to ignore.
"""
import json
import pathlib
import tempfile
import unittest

from cousin_lib import mcp_logs

_FAIL = [
    {"debug": "Starting connection with timeout of 30000ms",
     "timestamp": "2026-09-20T02:00:54.736Z", "sessionId": "s1"},
    {"error": "Server stderr: cousin-mcp: registry: meeting: no commands\n",
     "timestamp": "2026-09-20T02:00:55.105Z", "sessionId": "s1"},
    {"error": "Connection failed (CONNECTION_CLOSED): Connection closed",
     "timestamp": "2026-09-20T02:00:55.113Z", "sessionId": "s1"},
]
_OK = [
    {"debug": "Starting connection with timeout of 30000ms",
     "timestamp": "2026-09-20T02:43:40.379Z", "sessionId": "s2"},
    {"debug": "Successfully connected (transport: stdio) in 1779ms",
     "timestamp": "2026-09-20T02:43:42.152Z", "sessionId": "s2"},
]


class TestLastConnection(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.root / "config").mkdir()
        self.logs = self.root / "cache" / (str(self.home).replace("/", "-")
                                           ) / "mcp-logs-cousin"
        self.logs.mkdir(parents=True)
        (self.root / "config" / "harness.toml").write_text(
            'mcp_logs_dir = "%s/cache/{home_encoded}/mcp-logs-{server}"\n'
            % self.root)

    def _write(self, name, lines):
        path = self.logs / name
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))
        return path

    def test_no_log_at_all_is_none_not_an_error(self):
        self.assertIsNone(mcp_logs.last_connection(self.home, self.root))

    def test_a_failure_carries_the_servers_own_stderr(self):
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        out = mcp_logs.last_connection(self.home, self.root)
        self.assertFalse(out["ok"])
        self.assertIn("registry: meeting: no commands", out["stderr"])
        self.assertIn("CONNECTION_CLOSED", out["detail"])
        self.assertEqual(out["session_id"], "s1")

    def test_a_success_reports_ok_with_no_stderr(self):
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        out = mcp_logs.last_connection(self.home, self.root)
        self.assertTrue(out["ok"])
        self.assertEqual(out["stderr"], "")

    def test_the_newest_file_wins(self):
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        self.assertTrue(mcp_logs.last_connection(self.home, self.root)["ok"])

    def test_a_named_session_is_found_in_an_older_file(self):
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        out = mcp_logs.last_connection(self.home, self.root,
                                       session_id="s1")
        self.assertFalse(out["ok"])

    def test_a_session_with_no_log_yet_is_none(self):
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        self.assertIsNone(mcp_logs.last_connection(self.home, self.root,
                                                   session_id="nope"))

    def test_a_corrupt_line_does_not_break_the_read(self):
        path = self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        path.write_text("not json\n" + path.read_text())
        out = mcp_logs.last_connection(self.home, self.root)
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()


class TestBootWarning(unittest.TestCase):
    """The boot packet carries the reason, which is the whole point:
    the harness says CONNECTION_CLOSED and nothing else, and a cousin
    that cannot see why falls back to the CLIs and forgets."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.root / "config").mkdir()
        self.logs = self.root / "cache" / (str(self.home).replace("/", "-")
                                           ) / "mcp-logs-cousin"
        self.logs.mkdir(parents=True)
        (self.root / "config" / "harness.toml").write_text(
            'mcp_logs_dir = "%s/cache/{home_encoded}/mcp-logs-{server}"\n'
            % self.root)

    def _write(self, name, lines):
        (self.logs / name).write_text(
            "".join(json.dumps(x) + "\n" for x in lines))

    def test_a_failure_puts_the_servers_stderr_in_the_packet(self):
        from cousin_lib.boot import _mcp_warning
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        line = _mcp_warning(self.home)
        self.assertIn("registry: meeting: no commands", line)
        self.assertIn("FAILED", line)

    def test_a_good_connection_says_nothing(self):
        from cousin_lib.boot import _mcp_warning
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        self.assertEqual(_mcp_warning(self.home), "")

    def test_no_log_says_nothing(self):
        from cousin_lib.boot import _mcp_warning
        self.assertEqual(_mcp_warning(self.home), "")

    def test_a_broken_log_directory_never_costs_a_boot(self):
        from cousin_lib.boot import _mcp_warning
        (self.root / "config" / "harness.toml").write_text("not = [toml\n")
        self.assertEqual(_mcp_warning(self.home), "")
