"""Prompt-cache audit.

The provider's prompt cache is prefix-matched: a byte changed anywhere
in the prefix invalidates everything after it, and the only visible
symptom is a turn whose cache_read collapses while cache_creation
rises. The audit reads the harness transcripts through the harness
seam, takes the per-turn usage fields, and names the files whose
mtime falls between two successive turns where the hit rate dropped.
Synthetic transcripts in a temp dir; no harness, no service.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cousin_lib import cache_audit

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


def _stamp(minutes, base=NOW):
    return (base + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _turn(minutes, *, read, fresh=0, created=0, kind="assistant",
          sidechain=False, msg_id=None, base=NOW, nested=False):
    usage = {"input_tokens": fresh, "cache_read_input_tokens": read,
             "output_tokens": 5}
    if nested:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": created,
                                   "ephemeral_1h_input_tokens": 0}
    else:
        usage["cache_creation_input_tokens"] = created
    message = {"role": kind, "usage": usage,
               "content": [{"type": "text", "text": "x"}]}
    if msg_id:
        message["id"] = msg_id
    return json.dumps({"type": kind, "isSidechain": sidechain,
                       "timestamp": _stamp(minutes, base),
                       "message": message})


class AuditCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "memory").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n')
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        self.auto_memory = self.root / "harness-memory"

    def _configure(self, *, auto_memory=False):
        (self.root / "config").mkdir(exist_ok=True)
        text = 'transcripts_dir = "%s"\n' % self.transcripts
        if auto_memory:
            self.auto_memory.mkdir(exist_ok=True)
            text += 'auto_memory_dir = "%s"\n' % self.auto_memory
        (self.root / "config" / "harness.toml").write_text(text)

    def _write(self, name, lines):
        (self.transcripts / name).write_text("\n".join(lines) + "\n")

    def _touch(self, path, minutes):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("v")
        when = (NOW + timedelta(minutes=minutes)).timestamp()
        os.utime(path, (when, when))

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        env = {"FRAMEWORK_ROOT": str(self.root),
               "COUSIN_HOME": str(self.home)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cache_audit, "_now", lambda: NOW), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cache_audit.cache_audit_main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class TestTurns(AuditCase):
    def test_absent_seam_exits_2_naming_the_config_file(self):
        rc, out, err = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("config/harness.toml", err)

    def test_seam_without_transcripts_dir_exits_2_naming_the_key(self):
        (self.root / "config").mkdir()
        (self.root / "config" / "harness.toml").write_text(
            'auto_memory_dir = "%s"\n' % self.auto_memory)
        rc, out, err = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("transcripts_dir", err)

    def test_no_cousin_context_exits_2(self):
        self._configure()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ,
                             {"FRAMEWORK_ROOT": str(self.root)},
                             clear=True), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cache_audit.cache_audit_main([])
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_HOME", err.getvalue())

    def test_turns_come_from_assistant_usage_only(self):
        self._configure()
        self._write("s1.jsonl", [
            _turn(0, read=900, fresh=100),
            _turn(1, read=0, fresh=10, kind="user"),
            _turn(2, read=800, fresh=200, sidechain=True),
            "not json at all",
            json.dumps({"type": "assistant", "message": {}}),
            _turn(3, read=950, fresh=50),
        ])
        turns = cache_audit.turns(self.home, self.root, days=None)
        self.assertEqual([t["cache_read"] for t in turns], [900, 950])
        self.assertEqual(turns[0]["hit_rate"], 0.9)

    def test_a_message_split_over_several_records_counts_once(self):
        # The harness writes one record per content block, each
        # carrying the whole message's usage under the same id.
        self._configure()
        self._write("s1.jsonl", [
            _turn(0, read=900, fresh=100, msg_id="m1"),
            _turn(0, read=900, fresh=100, msg_id="m1"),
            _turn(1, read=950, fresh=50, msg_id="m2"),
        ])
        turns = cache_audit.turns(self.home, self.root, days=None)
        self.assertEqual(len(turns), 2)

    def test_cache_creation_falls_back_to_the_nested_breakdown(self):
        self._configure()
        self._write("s1.jsonl", [
            _turn(0, read=0, fresh=10, created=990, nested=True),
        ])
        turns = cache_audit.turns(self.home, self.root, days=None)
        self.assertEqual(turns[0]["cache_creation"], 990)
        self.assertEqual(turns[0]["hit_rate"], 0.0)

    def test_days_window_drops_older_turns_and_orders_the_rest(self):
        self._configure()
        old = NOW - timedelta(days=3)
        self._write("old.jsonl", [_turn(0, read=100, base=old)])
        self._write("b.jsonl", [_turn(5, read=300)])
        self._write("a.jsonl", [_turn(1, read=200)])
        with mock.patch.object(cache_audit, "_now", lambda: NOW):
            turns = cache_audit.turns(self.home, self.root, days=1)
        self.assertEqual([t["cache_read"] for t in turns], [200, 300])
        with mock.patch.object(cache_audit, "_now", lambda: NOW):
            turns = cache_audit.turns(self.home, self.root, days=7)
        self.assertEqual(len(turns), 3)

    def test_transcripts_dir_absent_means_no_turns(self):
        self._configure()
        self.transcripts.rmdir()
        self.assertEqual(
            cache_audit.turns(self.home, self.root, days=None), [])


class TestSummary(unittest.TestCase):
    def test_rate_and_distribution(self):
        turns = [
            {"hit_rate": 0.5, "cache_read": 50, "input": 50,
             "cache_creation": 0},
            {"hit_rate": 1.0, "cache_read": 100, "input": 0,
             "cache_creation": 0},
            {"hit_rate": 0.8, "cache_read": 80, "input": 10,
             "cache_creation": 10},
        ]
        summary = cache_audit.summarize(turns)
        self.assertEqual(summary["turns"], 3)
        self.assertAlmostEqual(summary["hit_rate"], 230 / 300, places=3)
        self.assertEqual(summary["distribution"],
                         {"min": 0.5, "median": 0.8, "max": 1.0})
        self.assertEqual(summary["verdict"], "marginal")

    def test_no_turns_is_a_null_summary_not_an_error(self):
        summary = cache_audit.summarize([])
        self.assertEqual(summary["turns"], 0)
        self.assertIsNone(summary["hit_rate"])
        self.assertIsNone(summary["distribution"])
        self.assertEqual(summary["verdict"], "no data")


class TestDrops(unittest.TestCase):
    def _t(self, minutes, rate):
        return {"timestamp": _stamp(minutes),
                "epoch": (NOW + timedelta(minutes=minutes)).timestamp(),
                "hit_rate": rate}

    def test_a_drop_is_a_fall_past_the_threshold_between_neighbours(self):
        turns = [self._t(0, 0.95), self._t(1, 0.94), self._t(2, 0.30),
                 self._t(3, 0.90), self._t(4, 0.05)]
        drops = cache_audit.drops(turns)
        self.assertEqual([(d["before"]["timestamp"], d["after"]["timestamp"])
                          for d in drops],
                         [(_stamp(1), _stamp(2)), (_stamp(3), _stamp(4))])
        self.assertAlmostEqual(drops[0]["fell_by"], 0.64, places=2)


class TestSuspects(AuditCase):
    def test_files_touched_between_the_dropped_pair_are_suspects(self):
        self._configure(auto_memory=True)
        self._write("s1.jsonl", [
            _turn(0, read=950, fresh=50),
            _turn(10, read=0, fresh=20, created=980),
            _turn(20, read=990, fresh=10),
        ])
        self._touch(self.home / "STATUS.md", 4)
        self._touch(self.home / "memory" / "quiet.md", 15)
        self._touch(self.auto_memory / "MEMORY.md", 7)
        self._touch(self.root / "elsewhere.md", 5)
        report = cache_audit.audit(self.home, self.root, days=None)
        names = [s["path"] for s in report["suspects"]]
        self.assertIn(str(self.home / "STATUS.md"), names)
        self.assertIn(str(self.auto_memory / "MEMORY.md"), names)
        self.assertNotIn(str(self.home / "memory" / "quiet.md"), names)
        self.assertNotIn(str(self.root / "elsewhere.md"), names)
        first = report["suspects"][0]
        self.assertEqual(first["pair"]["before"]["timestamp"], _stamp(0))
        self.assertEqual(first["pair"]["after"]["timestamp"], _stamp(10))

    def test_the_transcripts_themselves_are_never_suspects(self):
        # A transcript under the home would name itself on every drop.
        self._configure()
        self.transcripts.rmdir()
        self.transcripts = self.home / "transcripts"
        self.transcripts.mkdir()
        self._configure()
        self._write("s1.jsonl", [
            _turn(0, read=950, fresh=50),
            _turn(10, read=0, fresh=20, created=980),
        ])
        self._touch(self.transcripts / "s1.jsonl", 5)
        report = cache_audit.audit(self.home, self.root, days=None)
        self.assertEqual(report["suspects"], [])

    def test_suspects_rank_by_the_size_of_the_fall_they_sit_in(self):
        self._configure()
        self._write("s1.jsonl", [
            _turn(0, read=950, fresh=50),
            _turn(10, read=700, fresh=300),
            _turn(20, read=990, fresh=10),
            _turn(30, read=0, fresh=20, created=980),
        ])
        self._touch(self.home / "mild.md", 5)
        self._touch(self.home / "severe.md", 25)
        report = cache_audit.audit(self.home, self.root, days=None)
        self.assertEqual(report["drops"], 2)
        self.assertEqual([Path(s["path"]).name for s in report["suspects"]],
                         ["severe.md", "mild.md"])
        self.assertGreater(report["suspects"][0]["fell_by"],
                           report["suspects"][1]["fell_by"])


class TestCli(AuditCase):
    def _seed(self):
        self._configure()
        self._write("s1.jsonl", [
            _turn(0, read=950, fresh=50),
            _turn(10, read=0, fresh=20, created=980),
            _turn(20, read=990, fresh=10),
        ])
        self._touch(self.home / "STATUS.md", 4)

    def test_text_report_names_rate_distribution_and_suspects(self):
        self._seed()
        rc, out, err = self._run("--days", "7")
        self.assertEqual(rc, 0, err)
        self.assertIn("hit rate", out)
        self.assertIn("min", out)
        self.assertIn("median", out)
        self.assertIn("STATUS.md", out)
        self.assertNotIn(_stamp(0), out)

    def test_diagnose_lists_the_turn_pair_per_suspect(self):
        self._seed()
        rc, out, err = self._run("--diagnose")
        self.assertEqual(rc, 0, err)
        self.assertIn(_stamp(0), out)
        self.assertIn(_stamp(10), out)

    def test_json_report_is_one_parseable_object(self):
        self._seed()
        rc, out, err = self._run("--json", "--home", str(self.home))
        self.assertEqual(rc, 0, err)
        report = json.loads(out)
        self.assertEqual(report["slug"], "testa")
        self.assertEqual(report["turns"], 3)
        self.assertEqual(report["suspects"][0]["path"],
                         str(self.home / "STATUS.md"))
        self.assertIn("distribution", report)

    def test_empty_window_reports_no_data_and_exits_0(self):
        self._configure()
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertIn("no data", out)


if __name__ == "__main__":
    unittest.main()
