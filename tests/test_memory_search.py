"""Keyword memory search: FTS index, sanitize, staleness.

The v1 tier is keyword-only (SQLite FTS5, BM25); semantic search is
the declared M2 seam and nothing here pretends otherwise. Real
databases over real files.
"""
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.config import MissingConfigError
from cousin_lib.memory_search import build_index, search


class SearchCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        (self.home / "notes").mkdir()
        (self.home / "memory" / "ports.md").write_text(
            "# Port allocation\n\nThe claimed set excludes every port"
            " in any cousin.toml.\n")
        (self.home / "notes" / "flip-plan.md").write_text(
            "# Flip plan\n\nThe generation boundary needs a verified"
            " identity write.\n")
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestContext(SearchCase):
    def test_no_context_refuses_loudly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingConfigError):
                search("anything")


class TestSearch(SearchCase):
    def test_finds_the_right_file_by_keyword(self):
        build_index()
        hits, _ = search("claimed set port")
        self.assertTrue(hits)
        self.assertIn("ports.md", hits[0]["path"])
        self.assertEqual(hits[0]["collection"], "memory")

    def test_notes_are_a_tagged_collection(self):
        build_index()
        hits, _ = search("verified identity write")
        self.assertIn("flip-plan.md", hits[0]["path"])
        self.assertEqual(hits[0]["collection"], "notes")

    def test_fts_syntax_characters_do_not_crash_the_query(self):
        # FTS5 treats -, /, ', ( as query syntax; a raw natural-language
        # query used to error and the keyword leg swallowed it silently.
        build_index()
        hits, _ = search("what's the claimed-set (port) rule?")
        self.assertTrue(hits)
        self.assertIn("ports.md", hits[0]["path"])

    def test_new_files_are_picked_up_without_explicit_reindex(self):
        build_index()
        time.sleep(0.05)
        (self.home / "memory" / "fresh.md").write_text(
            "# Fresh\n\nzanzibar considerations\n")
        hits, _ = search("zanzibar")
        self.assertTrue(hits)
        self.assertIn("fresh.md", hits[0]["path"])


class TestCliWiring(SearchCase):
    def _main(self, argv):
        import contextlib
        import io

        from cousin_lib.memory import memory_main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memory_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_search_subcommand_returns_ranked_results(self):
        rc, out, _ = self._main(["search", "claimed set port"])
        self.assertEqual(rc, 0)
        self.assertIn("ports.md", out)

    def test_reindex_subcommand_rebuilds(self):
        rc, out, _ = self._main(["reindex"])
        self.assertEqual(rc, 0)
        self.assertIn("indexed", out.lower())


if __name__ == "__main__":
    unittest.main()
