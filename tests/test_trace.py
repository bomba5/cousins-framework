"""The tool-trace ledger: store, CLI wiring, boot consumption.

Producer and consumer land together - the ledger, the CLI invocation
logging that feeds it, and the boot layer that reads it un-reserve in
one change, per the producer-honesty rule. Logging itself is
best-effort by design: a tracing hiccup must never break the call it
was recording, and for the CALLER a failed trace write is a genuine
no-op.
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.trace import log_call, recent_calls, summary_for_boot


class TraceCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "COUSIN_HOME": str(self.home),
        })
        patcher.start()
        self.addCleanup(patcher.stop)


class TestStore(TraceCase):
    def test_log_and_read_roundtrip(self):
        log_call("wren", "cousin-memory",
                 args_summary="search 'ports'", result_summary="rc=0")
        rows = recent_calls("wren", n=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "cousin-memory")

    def test_summary_for_boot_formats_recent_calls(self):
        for i in range(3):
            log_call("wren", "cousin-job", args_summary="start #%d" % i)
        text = summary_for_boot("wren", n=10, since_hours=24)
        self.assertIn("cousin-job", text)
        self.assertIn("start #2", text)

    def test_empty_ledger_summary_is_the_idle_marker(self):
        text = summary_for_boot("wren", n=10, since_hours=24)
        self.assertIn("no substantive tool traces", text)

    def test_logging_failure_never_reaches_the_caller(self):
        # An unreachable root means the store cannot open; the caller
        # must not notice - a tracing hiccup never breaks the call it
        # was recording.
        with mock.patch.dict(os.environ,
                             {"FRAMEWORK_ROOT": "/nonexistent/x"}):
            result = log_call("wren", "cousin-memory")
        self.assertIsNone(result)


class TestCliWiring(TraceCase):
    def test_a_cli_invocation_lands_in_the_ledger(self):
        import contextlib
        import io

        from cousin_lib.memory import memory_main
        with contextlib.redirect_stdout(io.StringIO()):
            memory_main(["activity", "testing the wiring"])
        rows = recent_calls("wren", n=5)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["tool"], "cousin-memory")
        self.assertIn("activity", rows[0]["args_summary"])

    def test_contextless_invocation_skips_tracing_silently(self):
        import contextlib
        import io

        from cousin_lib.shared_tier import shared_main
        with mock.patch.dict(os.environ, {"COUSIN_HOME": ""}), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = shared_main(["list"])
        self.assertEqual(rc, 0)


class TestBootConsumption(TraceCase):
    def test_boot_packet_carries_recent_traces(self):
        from cousin_lib.boot import assemble
        log_call("wren", "cousin-schedule", args_summary="add 'in 30m'")
        packet = assemble("wren", self.home, generation=1)
        self.assertIn("cousin-schedule", packet["text"])
        self.assertNotIn("trace_summary", packet["degraded_sections"])


if __name__ == "__main__":
    unittest.main()
