"""The semantic search leg, its fusion with the keyword leg, and the
degrade contract.

The contract, in order of importance: soft-degrade is silent ONLY when
nothing was promised. No embedding config means keyword, silently -
the install never claimed semantic search. Embedding configured but
unreachable means keyword PLUS a notice - a quietly dead semantic leg
leaves someone believing they have semantic recall until the belief
costs them a lookup.

The semantic tests run against a fake embedding service over real
HTTP: the framework's embedding contract is exercised end to end,
only the vectors are scripted. The scripted rule: any text mentioning
sunrise or dawn embeds to vector A, everything else to vector B, so
cosine ranks the sunrise document first for a query the keyword leg
cannot match.
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.memory_search import search
from tests._fakes import fake_embedder

VECTOR_A = [1.0, 0.0, 0.0]
VECTOR_B = [0.0, 1.0, 0.0]


def _sunrise_vectors(text):
    return VECTOR_A if ("sunrise" in text or "dawn" in text) else VECTOR_B


class SemanticCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        (self.home / "memory" / "sky.md").write_text(
            "# Sky notes\n\nThe sunrise over the ridge was violet.\n")
        (self.home / "memory" / "ports.md").write_text(
            "# Ports\n\nThe claimed set excludes every port.\n")
        patcher = mock.patch.dict(os.environ, {
            "COUSIN_HOME": str(self.home),
            "FRAMEWORK_ROOT": str(self.root),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _configure_embedding(self, url, extra=""):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "embedding.toml").write_text(
            'url = "%s"\nmodel = "test-embed"\ntimeout_s = 2\n%s'
            % (url, extra))

    def _serve_fake(self, vector_for=_sunrise_vectors):
        ctx = fake_embedder(vector_for=vector_for)
        url = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return url


class TestDegradeContract(SemanticCase):
    def test_no_config_is_keyword_silently(self):
        # Nothing was promised; absence is a genuine no-op.
        hits, notice = search("claimed set port")
        self.assertIsNone(notice)
        self.assertIn("ports.md", hits[0]["path"])

    def test_configured_but_dead_is_keyword_plus_notice(self):
        # A configured-and-dead semantic leg changes the result set;
        # that is not a no-op and must never be silent.
        self._configure_embedding("http://127.0.0.1:9/nothing-here")
        hits, notice = search("claimed set port")
        self.assertIn("ports.md", hits[0]["path"])
        self.assertIsNotNone(notice)
        self.assertIn("keyword-only", notice)

    def test_unparsable_config_also_notices(self):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "embedding.toml").write_text("url = [broken")
        hits, notice = search("claimed set port")
        self.assertIn("ports.md", hits[0]["path"])
        self.assertIsNotNone(notice)

    def test_partial_embed_failure_is_noticed_not_swallowed(self):
        # The service answers the query but chokes on one file: the
        # result is still hybrid, and the caller is told what is
        # missing rather than left to believe the index is whole.
        (self.home / "memory" / "bad.md").write_text("poison pill text\n")
        self._configure_embedding(self._serve_fake(
            lambda t: None if "poison" in t else _sunrise_vectors(t)))
        hits, notice = search("dawn")
        self.assertIn("sky.md", hits[0]["path"])
        self.assertIsNotNone(notice)
        self.assertIn("1 chunk", notice)


class TestSemanticLeg(SemanticCase):
    def test_finds_by_meaning_what_keyword_cannot(self):
        # "dawn" appears nowhere in the corpus; the fake embedder maps
        # dawn and sunrise to the same vector, so only the semantic
        # leg can surface sky.md.
        self._configure_embedding(self._serve_fake())
        hits, notice = search("dawn")
        self.assertIsNone(notice)
        self.assertTrue(hits, "semantic leg found nothing")
        self.assertIn("sky.md", hits[0]["path"])

    def test_merged_results_keep_keyword_hits(self):
        self._configure_embedding(self._serve_fake())
        hits, notice = search("claimed set port")
        self.assertIn("ports.md", hits[0]["path"])

    def test_hits_carry_the_full_shape(self):
        self._configure_embedding(self._serve_fake())
        hits, _ = search("sunrise")
        self.assertEqual(set(hits[0]),
                         {"path", "collection", "score", "snippet", "chunk",
                          "similarity"})
        self.assertIsInstance(hits[0]["chunk"], int)

    def test_similarity_is_the_cosine_or_none(self):
        # The fused score is reciprocal-rank (about 1/60 at best), so a
        # cosine threshold can never be compared against it; every hit
        # carries the semantic leg's cosine as "similarity" (None when
        # only the keyword leg found it), and a path both legs found
        # keeps the cosine whichever leg was seen first.
        self._configure_embedding(self._serve_fake())
        hits, _ = search("sunrise")
        by_name = {pathlib.Path(h["path"]).name: h for h in hits}
        # sky.md: keyword hit AND semantic winner -> cosine 1.0 kept.
        self.assertAlmostEqual(by_name["sky.md"]["similarity"], 1.0,
                               places=6)
        # ports.md: semantic leg only, orthogonal vector -> cosine 0.0.
        self.assertAlmostEqual(by_name["ports.md"]["similarity"], 0.0,
                               places=6)
        # Keyword-only install: no semantic leg, similarity is None.
        (self.root / "config" / "embedding.toml").unlink()
        hits, _ = search("claimed set port")
        self.assertIn("ports.md", hits[0]["path"])
        self.assertIsNone(hits[0]["similarity"])

    def test_fusion_ranks_a_file_both_legs_agree_on_first(self):
        # "sunrise" is a keyword hit AND the semantic winner for sky.md;
        # ports.md appears in the semantic leg only (rank 2). Reciprocal
        # rank fusion: 1/60 + 1/60 for sky.md beats 1/61 for ports.md.
        self._configure_embedding(self._serve_fake())
        hits, _ = search("sunrise")
        self.assertIn("sky.md", hits[0]["path"])
        self.assertAlmostEqual(hits[0]["score"], 2 / 60, places=6)
        self.assertLess(hits[1]["score"], hits[0]["score"])

    def test_collection_filter_applies_to_both_legs(self):
        (self.home / "notes").mkdir()
        (self.home / "notes" / "walk.md").write_text(
            "# Walk\n\nWe walked at sunrise along the harbour.\n")
        self._configure_embedding(self._serve_fake())
        # Semantic-only query: "dawn" matches nothing by keyword.
        hits, _ = search("dawn", collection="notes")
        self.assertTrue(hits)
        self.assertTrue(all(h["collection"] == "notes" for h in hits))
        # Keyword-and-semantic query, filtered to memory.
        hits, _ = search("sunrise", collection="memory")
        self.assertTrue(hits)
        self.assertTrue(all(h["collection"] == "memory" for h in hits))
        self.assertIn("sky.md", hits[0]["path"])

    def test_top_bounds_the_fused_list(self):
        for i in range(4):
            (self.home / "memory" / ("extra%d.md" % i)).write_text(
                "# Extra %d\n\nsunrise number %d\n" % (i, i))
        self._configure_embedding(self._serve_fake())
        hits, _ = search("sunrise", top=2)
        self.assertEqual(len(hits), 2)

    def test_deep_chunk_is_found_and_named(self):
        # The fact lives past the first chunk; only chunk 1 embeds to
        # vector A, so a hit that names chunk 1 proves the tail was
        # embedded rather than truncated away.
        (self.home / "memory" / "long.md").write_text(
            "# Long\n\n" + "filler " * 400 + "\nthe sunrise fact is here\n")
        self._configure_embedding(self._serve_fake(),
                                  extra="chunk_chars = 2000\n"
                                        "chunk_overlap = 200\n")
        hits, _ = search("dawn", collection="memory")
        by_path = {pathlib.Path(h["path"]).name: h for h in hits}
        self.assertIn("long.md", by_path)
        self.assertGreaterEqual(by_path["long.md"]["chunk"], 1)
        self.assertTrue(by_path["long.md"]["snippet"])


if __name__ == "__main__":
    unittest.main()
