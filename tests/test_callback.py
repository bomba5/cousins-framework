"""Callback moments: a cousin's own library of moments worth calling
back to, one bullet per moment under memory/ so search indexes it.

The source kept this as a markdown table keyed by slug; here it is a
bullet list at a fixed path under the home (the home already names the
cousin), and pipes inside a moment are escaped so a field separator
inside the text cannot split a row.
"""
import contextlib
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import callback
from cousin_lib.callback import callback_main


class CallbackCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "testa"
        self.home.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = callback_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _file(self):
        return self.home / "memory" / "callbacks.md"


class TestLibrary(CallbackCase):
    def test_tag_writes_one_bullet_under_memory(self):
        path = callback.tag(self.home, "the operator said the magic word",
                            cycle=12, category="banter")
        self.assertEqual(path, self._file())
        text = path.read_text()
        self.assertTrue(text.startswith("# "), "header missing")
        bullets = [l for l in text.splitlines() if l.startswith("- ")]
        self.assertEqual(len(bullets), 1)
        self.assertIn("the operator said the magic word", bullets[0])
        self.assertIn("12", bullets[0])
        self.assertIn("banter", bullets[0])

    def test_round_trip_through_list_all(self):
        callback.tag(self.home, "first", cycle=1, category="a")
        callback.tag(self.home, "second")
        entries = callback.list_all(self.home)
        self.assertEqual([e["moment"] for e in entries], ["first", "second"])
        self.assertEqual(entries[0]["cycle"], 1)
        self.assertEqual(entries[0]["category"], "a")
        self.assertIsNone(entries[1]["cycle"])
        self.assertIsNone(entries[1]["category"])
        self.assertTrue(entries[0]["time"])

    def test_search_is_case_insensitive_substring(self):
        callback.tag(self.home, "Espresso ran out at 07:00")
        callback.tag(self.home, "unrelated")
        hits = callback.search(self.home, "espresso")
        self.assertEqual([h["moment"] for h in hits],
                         ["Espresso ran out at 07:00"])
        self.assertEqual(callback.search(self.home, "nothing"), [])

    def test_pipes_in_a_moment_do_not_split_the_row(self):
        callback.tag(self.home, "a|b|c", category="x|y")
        entries = callback.list_all(self.home)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["moment"], "a|b|c")
        self.assertEqual(entries[0]["category"], "x|y")

    def test_empty_moment_is_refused(self):
        with self.assertRaises(ValueError):
            callback.tag(self.home, "   ")
        self.assertFalse(self._file().exists())

    def test_oversized_moment_is_truncated(self):
        callback.tag(self.home, "x" * 500)
        moment = callback.list_all(self.home)[0]["moment"]
        self.assertLessEqual(len(moment), 200)
        self.assertTrue(moment.endswith("..."))

    def test_existing_file_is_appended_not_rewritten(self):
        self._file().parent.mkdir(parents=True)
        self._file().write_text("# my own header\n\n- kept by hand\n")
        callback.tag(self.home, "new one")
        text = self._file().read_text()
        self.assertIn("my own header", text)
        self.assertIn("kept by hand", text)
        self.assertIn("new one", text)

    def test_empty_library_lists_and_searches_as_empty(self):
        self.assertEqual(callback.list_all(self.home), [])
        self.assertEqual(callback.search(self.home, "x"), [])


class TestCli(CallbackCase):
    def test_tag_list_search(self):
        rc, out, _ = self._main(["tag", "shipped the report",
                                 "--cycle", "3", "--category", "work"])
        self.assertEqual(rc, 0)
        self.assertIn("callbacks.md", out)
        rc, out, _ = self._main(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("shipped the report", out)
        rc, out, _ = self._main(["search", "REPORT"])
        self.assertEqual(rc, 0)
        self.assertIn("shipped the report", out)
        rc, out, _ = self._main(["search", "absent"])
        self.assertEqual(rc, 0)
        self.assertNotIn("shipped", out)

    def test_list_filters_by_category_and_limit(self):
        for i in range(5):
            self._main(["tag", "m%d" % i, "--category", "a" if i % 2 else "b"])
        rc, out, _ = self._main(["list", "--category", "a"])
        self.assertEqual(rc, 0)
        self.assertIn("m1", out)
        self.assertNotIn("m0", out)
        rc, out, _ = self._main(["list", "--limit", "1"])
        self.assertIn("m4", out)
        self.assertNotIn("m3", out)

    def test_empty_tag_exits_two(self):
        rc, _, err = self._main(["tag", "  "])
        self.assertEqual(rc, 2)
        self.assertTrue(err)

    def test_home_flag_overrides_env(self):
        other = self.home.parent / "other"
        other.mkdir()
        rc, _, _ = self._main(["--home", str(other), "tag", "elsewhere"])
        self.assertEqual(rc, 0)
        self.assertTrue((other / "memory" / "callbacks.md").exists())
        self.assertFalse(self._file().exists())

    def test_no_context_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rc, _, err = self._main(["list"])
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_HOME", err)


if __name__ == "__main__":
    unittest.main()
