"""`cousin-memory decide --stdin`: the shell-free path for decision prose.

Text passed to `decide` as a double-quoted argument is mangled by the
CALLER's shell before argv exists: backticks run command substitution,
and every fix so far has been "remember to quote", a rule with a
measured failure rate. A quoted heredoc into stdin cannot be expanded
by any shell, so this path removes the class instead of patching it.
Format: three chunks separated by a line that is exactly `---`.
"""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory
from cousin_lib.memory import memory_main, parse_decide_stdin


class TestParse(unittest.TestCase):
    def test_splits_three_chunks_on_a_bare_separator_line(self):
        self.assertEqual(
            parse_decide_stdin("the topic\n---\nthe decision\n---\n"
                               "the reasoning\n"),
            ("the topic", "the decision", "the reasoning"))

    def test_shell_metacharacters_survive_verbatim(self):
        body = ("wrappers\n---\n"
                "the wrapper is `exec python3 $lib \"$@\"`\n---\n"
                "$(uname -a) and 'unbalanced and a\nsecond line")
        topic, decision, reasoning = parse_decide_stdin(body)
        self.assertEqual(decision, 'the wrapper is `exec python3 $lib "$@"`')
        self.assertTrue(reasoning.startswith("$(uname -a) and 'unbalanced"))
        self.assertIn("second line", reasoning)

    def test_internal_newlines_are_preserved_in_the_reasoning(self):
        _, _, reasoning = parse_decide_stdin(
            "t\n---\nd\n---\nline one\n\nline three\n")
        self.assertEqual(reasoning, "line one\n\nline three")

    def test_a_separator_with_trailing_text_is_not_a_separator(self):
        topic, decision, reasoning = parse_decide_stdin(
            "t\n---\nd --- still the decision\n--- not a separator\n---\nr\n")
        self.assertIn("still the decision", decision)
        self.assertIn("not a separator", decision)
        self.assertEqual(reasoning, "r")

    def test_too_few_chunks_is_an_error_naming_the_count(self):
        with self.assertRaises(ValueError) as ctx:
            parse_decide_stdin("only\n---\ntwo\n")
        self.assertIn("2", str(ctx.exception))

    def test_too_many_chunks_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_decide_stdin("a\n---\nb\n---\nc\n---\nd\n")

    def test_empty_chunk_is_an_error_not_a_silent_blank_decision(self):
        with self.assertRaises(ValueError):
            parse_decide_stdin("topic\n---\n\n---\nreasoning\n")


class WiringCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "testa"
        self.home.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            rc = memory_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _entry(self):
        line = (self.home / "data" / "decisions.jsonl").read_text().strip()
        return json.loads(line)


class TestWiring(WiringCase):
    def test_the_flag_reaches_the_log_and_the_raw_bridge(self):
        body = "epaper\n---\nuse `ssd1680`\n---\n$(cheaper) and in stock\n"
        rc, out, _ = self._main(["decide", "--stdin"], stdin=body)
        self.assertEqual(rc, 0)
        entry = self._entry()
        self.assertEqual(entry["topic"], "epaper")
        self.assertEqual(entry["decision"], "use `ssd1680`")
        self.assertEqual(entry["reasoning"], "$(cheaper) and in stock")
        raw = memory.list_raw(self.home, since_days=1)
        self.assertEqual(len(raw), 1)
        self.assertIn("use `ssd1680` - why: $(cheaper)", raw[0]["content"])

    def test_positional_form_still_works_untouched(self):
        rc, _, _ = self._main(["decide", "t", "d", "r"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._entry()["decision"], "d")

    def test_a_bad_body_is_a_usage_error_and_logs_nothing(self):
        rc, _, err = self._main(["decide", "--stdin"], stdin="only\n---\ntwo\n")
        self.assertEqual(rc, 2)
        self.assertIn("2", err)
        self.assertFalse((self.home / "data" / "decisions.jsonl").exists())

    def test_neither_form_is_a_usage_error(self):
        rc, _, err = self._main(["decide", "t", "d"])
        self.assertEqual(rc, 2)
        self.assertIn("--stdin", err)


if __name__ == "__main__":
    unittest.main()
