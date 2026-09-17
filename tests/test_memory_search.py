"""Keyword memory search: FTS index, sanitize, staleness, collections
and the CLI surface.

The keyword leg (SQLite FTS5, BM25) is what every install has; the
semantic leg is tested in test_semantic_search. Real databases over
real files.
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.config import MissingConfigError
from cousin_lib.memory_search import build_index, search
from tests._fakes import fake_embedder


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

    def test_search_json_prints_a_list_of_hits(self):
        rc, out, _ = self._main(["search", "claimed set port", "--json"])
        self.assertEqual(rc, 0)
        hits = json.loads(out)
        self.assertIsInstance(hits, list)
        self.assertIn("ports.md", hits[0]["path"])
        self.assertEqual(set(hits[0]),
                         {"path", "collection", "score", "snippet", "chunk",
                          "similarity"})

    def test_search_json_with_no_hits_is_an_empty_list(self):
        rc, out, _ = self._main(["search", "zzz-nothing-here", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [])

    def test_search_collection_filters(self):
        rc, out, _ = self._main(["search", "verified identity write",
                                 "--collection", "notes", "--json"])
        self.assertEqual(rc, 0)
        hits = json.loads(out)
        self.assertTrue(hits)
        self.assertTrue(all(h["collection"] == "notes" for h in hits))
        rc, out, _ = self._main(["search", "verified identity write",
                                 "--collection", "memory", "--json"])
        self.assertEqual(json.loads(out), [])

    def test_reindex_reports_embedding_work_when_configured(self):
        root = self.home.parent.parent
        (root / "config").mkdir()
        with fake_embedder() as url, \
                mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            (root / "config" / "embedding.toml").write_text(
                'url = "%s"\nmodel = "m"\ntimeout_s = 2\n' % url)
            rc, out, err = self._main(["reindex"])
        self.assertEqual(rc, 0)
        self.assertIn("indexed 2 file(s)", out)
        self.assertIn("embedded 2 chunk(s)", out)
        self.assertEqual(err, "")


class TestCollections(SearchCase):
    def test_collection_filter_on_the_keyword_leg(self):
        hits, _ = search("verified identity write", collection="memory")
        self.assertEqual(hits, [])
        hits, _ = search("verified identity write", collection="notes")
        self.assertTrue(hits)
        self.assertTrue(all(h["collection"] == "notes" for h in hits))

    def _root_with_harness(self, template):
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "harness.toml").write_text(
            'auto_memory_dir = "%s"\n' % template)
        return root

    def test_harness_collection_when_configured_and_present(self):
        root = self._root_with_harness(
            str(self.home.parent.parent / "harness") + "/{home_encoded}/memory")
        encoded = str(self.home).replace("/", "-")
        harness = root / "harness" / encoded / "memory"
        harness.mkdir(parents=True)
        (harness / "feedback_terse.md").write_text(
            "# Terse replies\n\nquokka preference: short statuses.\n")
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            hits, _ = search("quokka")
            self.assertTrue(hits)
            self.assertEqual(hits[0]["collection"], "harness")
            self.assertIn("feedback_terse.md", hits[0]["path"])
            only, _ = search("quokka", collection="harness")
            self.assertEqual([h["path"] for h in only],
                             [hits[0]["path"]])

    def test_harness_collection_absent_when_directory_missing(self):
        root = self._root_with_harness(
            str(self.home.parent.parent / "nowhere") + "/{home_encoded}")
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            hits, notice = search("claimed set port")
        self.assertIsNone(notice)
        self.assertTrue(all(h["collection"] != "harness" for h in hits))

    def test_no_root_means_own_files_only(self):
        # Without FRAMEWORK_ROOT there is no config/ to consult: the
        # searchable surface is memory/ and notes/, nothing else.
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["COUSIN_HOME"] = str(self.home)
            hits, notice = search("claimed set port")
        self.assertIsNone(notice)
        self.assertEqual({h["collection"] for h in hits}, {"memory"})


if __name__ == "__main__":
    unittest.main()
