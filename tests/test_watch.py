"""cousin-watch: a runner cousin's reasoning stream in a terminal
(watch.py). Hermetic: a temp install, an EventStream written directly."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import watch
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase


def _event(kind, payload, seq=1):
    return {"seq": seq, "ts": 1790000000.0, "kind": kind, "payload": payload}


class TestFormat(unittest.TestCase):
    def test_the_views_kinds_read_as_lines(self):
        cases = [
            ("state", {"from": "idle", "to": "running", "detail": "turn"},
             "idle -> running (turn)"),
            ("thinking", {"length": 5, "text": "hmm.."}, "hmm.."),
            ("thinking", {"length": 5}, "(5 chars, not recorded)"),
            ("tool", {"name": "Bash", "input": {"command": "ls"}}, 'Bash {"command": "ls"}'),
            ("tool_result", {"text": "a  b\nc", "is_error": True}, "error: a b c"),
            ("result", {"inbox_ids": [3], "interrupted": True}, "rows [3] interrupted"),
            ("rate_limit", {"status": "rejected", "resets_at": 5}, 'status="rejected" resets_at=5'),
        ]
        for kind, payload, want in cases:
            line = watch.format_event(_event(kind, payload))
            self.assertTrue(line.endswith(want), (kind, line))

    def test_a_multi_line_text_is_indented_under_its_first_line(self):
        line = watch.format_event(_event("text", {"text": "one\ntwo"}))
        first, second = line.split("\n")
        self.assertTrue(first.endswith("text    one"), first)
        self.assertEqual(second.strip(), "two")
        self.assertEqual(len(second) - len(second.lstrip()), len(first) - len("one"))

    def test_a_long_summary_is_cut(self):
        line = watch.format_event(_event("tool_result", {"text": "x" * 500}))
        self.assertLessEqual(len(line.split("  ", 1)[1].strip()), watch.WIDTH)


class InstallCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[agent]\nrunner = "fake"\n')
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)})
        p.start(); self.addCleanup(p.stop)
        self.stream = EventStream(self.home, "fake-a")
        self.stream.append("state", {"from": "idle", "to": "running", "detail": "turn"})
        self.stream.append("text", {"text": "the ledger closes on Monday"})

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = watch.watch_main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class TestCli(InstallCase):
    def test_it_prints_the_stream_and_exits(self):
        rc, out, _ = self._main("wren")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("the ledger closes on Monday", out)

    def test_a_plain_watch_is_the_tail_and_tail_0_is_everything(self):
        for i in range(10):
            self.stream.append("tool", {"name": "Bash", "input": {"n": i}})
        rc, out, _ = self._main("wren", "--tail", "3", "--json")
        self.assertEqual([json.loads(line)["seq"] for line in out.splitlines()], [10, 11, 12])
        rc, out, _ = self._main("wren", "--tail", "0", "--json")
        self.assertEqual(len(out.splitlines()), 12)

    def test_after_and_json(self):
        rc, out, _ = self._main("wren", "--after", "1", "--json")
        self.assertEqual([json.loads(line)["kind"] for line in out.splitlines()], ["text"])

    def test_follow_prints_what_is_appended(self):
        steps = iter([lambda: self.stream.append("result", {"inbox_ids": [1]}),
                      lambda: (_ for _ in ()).throw(KeyboardInterrupt())])
        out = io.StringIO()
        with self.assertRaises(KeyboardInterrupt):
            watch.run(self.home, follow=True, out=out, sleep=lambda _: next(steps)())
        self.assertIn("rows [1]", out.getvalue())

    def test_an_unknown_or_tmux_cousin_is_2(self):
        self.assertEqual(self._main("nobody")[0], 2)
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        rc, _, err = self._main("wren")
        self.assertEqual(rc, 2)
        self.assertIn("not a runner cousin", err)

    def test_the_module_runs_as_a_script(self):
        import subprocess
        import sys
        r = subprocess.run([sys.executable, "-m", "cousin_lib.watch", "wren"],
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, FRAMEWORK_ROOT=str(self.root)),
                           cwd=str(pathlib.Path(watch.__file__).resolve().parents[1]))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("idle -> running", r.stdout)


if __name__ == "__main__":
    unittest.main()
