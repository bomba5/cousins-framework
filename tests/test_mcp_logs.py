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
import re
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
        self.logs = self.root / "cache" / (re.sub(r"[^A-Za-z0-9]", "-", str(self.home))
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
        self.assertEqual(out["state"], "failed")
        self.assertIn("registry: meeting: no commands", out["stderr"])
        self.assertIn("CONNECTION_CLOSED", out["detail"])
        self.assertEqual(out["session_id"], "s1")

    def test_a_success_reports_ok_with_no_stderr(self):
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        out = mcp_logs.last_connection(self.home, self.root)
        self.assertEqual(out["state"], "connected")
        self.assertEqual(out["stderr"], "")

    def test_the_newest_file_wins(self):
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        self.assertEqual(
            mcp_logs.last_connection(self.home, self.root)["state"],
            "connected")

    def test_a_named_session_is_found_in_an_older_file(self):
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        out = mcp_logs.last_connection(self.home, self.root,
                                       session_id="s1")
        self.assertEqual(out["state"], "failed")

    def test_a_session_with_no_log_yet_is_none(self):
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        self.assertIsNone(mcp_logs.last_connection(self.home, self.root,
                                                   session_id="nope"))

    def test_a_corrupt_line_does_not_break_the_read(self):
        path = self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        path.write_text("not json\n" + path.read_text())
        out = mcp_logs.last_connection(self.home, self.root)
        self.assertEqual(out["state"], "failed")


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
        self.logs = self.root / "cache" / (re.sub(r"[^A-Za-z0-9]", "-", str(self.home))
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


_UNRECORDED = [
    {"debug": "Starting connection with timeout of 30000ms",
     "timestamp": "2026-04-18T11:42:50.265Z", "sessionId": "s3"},
]
_RECONNECT_UNRECORDED = [
    {"debug": "Starting connection with timeout of 30000ms",
     "timestamp": "2026-03-29T09:44:37.900Z", "sessionId": "s4"},
    {"debug": "Successfully connected (transport: stdio) in 1779ms",
     "timestamp": "2026-03-29T09:44:39.753Z", "sessionId": "s4"},
    {"debug": "Starting connection with timeout of 30000ms",
     "timestamp": "2026-04-18T11:42:50.265Z", "sessionId": "s4"},
]
_UNKNOWN_WORDING = [
    {"debug": "Starting connection with timeout of 30000ms",
     "timestamp": "2026-02-25T10:35:35.892Z", "sessionId": "s5"},
    {"debug": "claude.ai proxy connection failed after 353ms: "
              "Streamable HTTP error", "timestamp": "2026-02-25T10:35:36.239Z",
     "sessionId": "s5"},
    {"error": "Error: Streamable HTTP error: authentication_error",
     "timestamp": "2026-02-25T10:35:36.239Z", "sessionId": "s5"},
]


class TestOutcomeVocabulary(unittest.TestCase):
    """An attempt whose result nobody wrote down is not a failure, and a
    failure the parser has no literal for is not a silence. Measured
    2026-09-21 over 5908 harness logs: 93 files were reported FAILED
    with "no reason recorded"; 80 of them held the reason and 13 held
    no outcome at all."""

    def test_an_attempt_with_no_outcome_is_unrecorded_not_failed(self):
        out = mcp_logs._outcome(_UNRECORDED)
        self.assertEqual(out["state"], "unrecorded")

    def test_an_unrecorded_attempt_keeps_the_earlier_outcome_as_context(self):
        out = mcp_logs._outcome(_RECONNECT_UNRECORDED)
        self.assertEqual(out["state"], "unrecorded")
        self.assertEqual(out["earlier"], "connected")

    def test_the_anchor_is_the_last_attempt_not_the_earlier_success(self):
        out = mcp_logs._outcome(_RECONNECT_UNRECORDED)
        self.assertEqual(out["when"], "2026-04-18T11:42:50.265Z")

    def test_a_failure_with_no_known_literal_keeps_its_reason(self):
        out = mcp_logs._outcome(_UNKNOWN_WORDING)
        self.assertEqual(out["state"], "failed")
        self.assertIn("authentication_error", out["reason"])

    def test_a_known_failure_reports_the_servers_stderr_as_the_reason(self):
        out = mcp_logs._outcome(_FAIL)
        self.assertEqual(out["state"], "failed")
        self.assertIn("registry: meeting: no commands", out["reason"])

    def test_a_success_is_connected(self):
        self.assertEqual(mcp_logs._outcome(_OK)["state"], "connected")


class TestWarningSaysWhatItKnows(unittest.TestCase):
    """The packet is assembled before the new session exists, so it can
    only ever report a past one. It may name which, and may not predict
    this one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.root / "config").mkdir()
        self.logs = self.root / "cache" / (re.sub(r"[^A-Za-z0-9]", "-", str(self.home))
                                           ) / "mcp-logs-cousin"
        self.logs.mkdir(parents=True)
        (self.root / "config" / "harness.toml").write_text(
            'mcp_logs_dir = "%s/cache/{home_encoded}/mcp-logs-{server}"\n'
            % self.root)

    def _write(self, name, lines):
        (self.logs / name).write_text(
            "".join(json.dumps(x) + "\n" for x in lines))

    def _toml(self, session_id):
        (self.home / "cousin.toml").write_text(
            '[runtime]\nsession_id = "%s"\n' % session_id)

    def test_the_warning_names_the_session_it_is_about(self):
        from cousin_lib.boot import _mcp_warning
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        self.assertIn("s1", _mcp_warning(self.home))

    def test_the_warning_does_not_predict_this_session(self):
        from cousin_lib.boot import _mcp_warning
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        self.assertNotIn("this session", _mcp_warning(self.home))

    def test_an_unrecorded_attempt_does_not_read_as_failed(self):
        from cousin_lib.boot import _mcp_warning
        self._write("2026-04-18T11-42-50-265Z.jsonl", _UNRECORDED)
        line = _mcp_warning(self.home)
        self.assertNotIn("FAILED", line)
        self.assertIn("2026-04-18T11:42:50.265Z", line)

    def test_the_warning_is_scoped_to_the_dying_generation(self):
        from cousin_lib.boot import _mcp_warning
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        self._toml("s2")
        self.assertEqual(_mcp_warning(self.home), "")


class TestLastConnectionCLI(unittest.TestCase):
    """`cousin-mcp --last-connection` is the reader the boot packet
    points at, and it shares `_outcome`, so it inherits the same three
    states and must not call an unrecorded outcome a failure either."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.root / "config").mkdir()
        self.logs = self.root / "cache" / (re.sub(r"[^A-Za-z0-9]", "-", str(self.home))
                                           ) / "mcp-logs-cousin"
        self.logs.mkdir(parents=True)
        (self.root / "config" / "harness.toml").write_text(
            'mcp_logs_dir = "%s/cache/{home_encoded}/mcp-logs-{server}"\n'
            % self.root)

    def _write(self, name, lines):
        (self.logs / name).write_text(
            "".join(json.dumps(x) + "\n" for x in lines))

    def _run(self):
        import contextlib
        import io
        import os
        from unittest import mock
        from cousin_lib import mcp_server
        out = io.StringIO()
        env = {"COUSIN_HOME": str(self.home), "FRAMEWORK_ROOT": str(self.root)}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            rc = mcp_server.mcp_main(["--last-connection"])
        return rc, out.getvalue()

    def test_a_connection_that_worked_exits_zero(self):
        self._write("2026-09-20T02-43-40-375Z.jsonl", _OK)
        rc, text = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("connected", text)

    def test_a_failure_exits_one_and_prints_the_reason(self):
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        rc, text = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("registry: meeting: no commands", text)

    def test_an_unrecorded_outcome_is_inconclusive_not_a_failure(self):
        self._write("2026-04-18T11-42-50-265Z.jsonl", _UNRECORDED)
        rc, text = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("no outcome recorded", text)
        self.assertNotIn("FAILED", text)


class TestWarningWithNothingToScopeBy(TestWarningSaysWhatItKnows):
    """A cousin with no persisted `runtime.session_id` - hand-made, or
    being flipped for the first time - gives the warning nothing to
    scope by, and the lookup falls back to the newest file. That is
    the pre-1.6.0 reading, which can answer with an older generation's
    record, so the line has to say it could not scope rather than
    claim the generation that just died."""

    def test_with_no_session_id_on_file_the_line_says_it_could_not_scope(self):
        from cousin_lib.boot import _mcp_warning
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        line = _mcp_warning(self.home)          # no cousin.toml at all
        self.assertIn("s1", line)
        self.assertIn("could not be scoped", line)

    def test_a_scoped_line_makes_no_such_caveat(self):
        from cousin_lib.boot import _mcp_warning
        self._write("2026-09-20T02-00-54-732Z.jsonl", _FAIL)
        self._toml("s1")
        self.assertNotIn("could not be scoped", _mcp_warning(self.home))
