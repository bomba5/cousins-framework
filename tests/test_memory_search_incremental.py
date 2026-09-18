"""The incremental, self-healing vector index.

Ported behaviours from an earlier version's incremental-reindex
tests, re-expressed over this repository's explicit-argument API: a
stale or empty embeddings index used to be served forever, and the
only rebuild path re-embedded every file. ensure_index embeds only
what changed, reuses the rest, drops what is gone, and survives a
dead embedding service without poisoning what it already had. The
fake embedder is real HTTP on loopback; only the vectors are scripted.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory_search
from tests._fakes import fake_embedder

DEAD_URL = "http://127.0.0.1:9/nothing-here"


def _home_with(root, files):
    home = pathlib.Path(root) / "cousins" / "wren"
    for rel, body in files.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    (home / "memory").mkdir(parents=True, exist_ok=True)
    return home


def _cfg(url):
    return {"url": url, "model": "m", "timeout_s": 5}


def _index(home):
    return json.loads((home / "memory" / "embeddings.json").read_text())


class TestChunkText(unittest.TestCase):
    def test_short_body_is_one_chunk(self):
        self.assertEqual(memory_search._chunk_text("short body"),
                         ["short body"])

    def test_long_body_is_split_with_overlap(self):
        body = "".join("w%05d " % i for i in range(1000))  # ~7KB
        chunks = memory_search._chunk_text(body, size=2000, overlap=200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 2000 for c in chunks))
        self.assertTrue(all(chunks))
        # Overlap: each chunk starts inside the tail of the previous.
        for prev, nxt in zip(chunks, chunks[1:]):
            self.assertEqual(prev[-200:], nxt[:200])
        # Every byte of the body is covered by some chunk.
        self.assertEqual("".join(c[:1800] for c in chunks[:-1])
                         + chunks[-1], body)

    def test_exact_multiple_leaves_no_empty_chunk(self):
        body = "x" * 4000
        chunks = memory_search._chunk_text(body, size=2000, overlap=0)
        self.assertEqual(chunks, ["x" * 2000, "x" * 2000])

    def test_empty_body_yields_no_chunks(self):
        self.assertEqual(memory_search._chunk_text(""), [])

    def test_overlap_never_stalls(self):
        chunks = memory_search._chunk_text("abcdef", size=2, overlap=5)
        self.assertGreater(len(chunks), 1)
        self.assertLess(len(chunks), 20)


class TestEnsureIndex(unittest.TestCase):
    def test_first_pass_embeds_everything_and_persists(self):
        calls = []
        with tempfile.TemporaryDirectory() as root, \
                fake_embedder(calls=calls) as url:
            home = _home_with(root, {"memory/a.md": "alpha",
                                     "notes/n.md": "note body"})
            rep = memory_search.ensure_index(home, _cfg(url))
            self.assertEqual(rep["embedded"], 2)
            self.assertEqual(rep["reused"], 0)
            self.assertEqual(rep["failed"], 0)
            self.assertEqual(rep["stale_reason"], "no index")
            self.assertEqual(len(calls), 2)
            index = _index(home)
            self.assertEqual(set(index), {"memory:a.md#0", "notes:n.md#0"})
            entry = index["memory:a.md#0"]
            self.assertEqual(set(entry), {"mtime", "vector", "text_hash"})
            self.assertTrue(entry["vector"])

    def test_incremental_reembeds_only_changed_files(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha",
                                     "memory/b.md": "beta"})
            cfg = _cfg(url)
            rep = memory_search.ensure_index(home, cfg)
            self.assertEqual((rep["embedded"], rep["reused"]), (2, 0))
            (home / "memory" / "a.md").write_text("alpha changed")
            rep = memory_search.ensure_index(home, cfg)
            self.assertEqual((rep["embedded"], rep["reused"]), (1, 1))
            (home / "memory" / "b.md").unlink()
            rep = memory_search.ensure_index(home, cfg)
            self.assertEqual(rep["dropped"], 1)
            self.assertEqual(set(_index(home)), {"memory:a.md#0"})

    def test_new_file_is_embedded_incrementally(self):
        calls = []
        with tempfile.TemporaryDirectory() as root, \
                fake_embedder(calls=calls) as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            memory_search.ensure_index(home, _cfg(url))
            calls.clear()
            (home / "memory" / "b.md").write_text("beta")
            rep = memory_search.ensure_index(home, _cfg(url))
            self.assertEqual(calls, ["beta"])
            self.assertEqual((rep["embedded"], rep["reused"]), (1, 1))

    def test_touched_but_identical_content_is_reused(self):
        # An mtime bump with the same bytes is not a change worth an
        # embed; the text hash is the truth, the mtime a hint.
        calls = []
        with tempfile.TemporaryDirectory() as root, \
                fake_embedder(calls=calls) as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            memory_search.ensure_index(home, _cfg(url))
            calls.clear()
            path = home / "memory" / "a.md"
            os.utime(path, (path.stat().st_mtime + 60,) * 2)
            rep = memory_search.ensure_index(home, _cfg(url))
            self.assertEqual(calls, [])
            self.assertEqual(rep["reused"], 1)

    def test_noop_pass_does_not_rewrite_the_index(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            memory_search.ensure_index(home, _cfg(url))
            index_path = home / "memory" / "embeddings.json"
            before = index_path.stat().st_mtime_ns
            rep = memory_search.ensure_index(home, _cfg(url))
            self.assertIsNone(rep["stale_reason"])
            self.assertEqual(index_path.stat().st_mtime_ns, before)

    def test_long_file_gets_one_key_per_chunk(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            deep = "the zebra passphrase lives here"
            home = _home_with(root, {"memory/long.md": "filler " * 600
                                     + deep})
            cfg = dict(_cfg(url), chunk_chars=2000, chunk_overlap=200)
            rep = memory_search.ensure_index(home, cfg)
            keys = sorted(_index(home))
            self.assertIn("memory:long.md#0", keys)
            self.assertIn("memory:long.md#1", keys)
            self.assertEqual(rep["embedded"], len(keys))

    def test_empty_index_over_sources_is_stale(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            (home / "memory" / "embeddings.json").write_text("{}")
            rep = memory_search.ensure_index(home, _cfg(url))
            self.assertIn("empty index", rep["stale_reason"])
            self.assertIn("memory:a.md#0", _index(home))

    def test_unreadable_index_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            (home / "memory" / "embeddings.json").write_text("{ broken")
            rep = memory_search.ensure_index(home, _cfg(url))
            self.assertEqual(rep["embedded"], 1)
            self.assertIn("memory:a.md#0", _index(home))

    def test_force_reembeds_everything(self):
        calls = []
        with tempfile.TemporaryDirectory() as root, \
                fake_embedder(calls=calls) as url:
            home = _home_with(root, {"memory/a.md": "alpha",
                                     "memory/b.md": "beta"})
            memory_search.ensure_index(home, _cfg(url))
            calls.clear()
            rep = memory_search.ensure_index(home, _cfg(url), force=True)
            self.assertEqual(len(calls), 2)
            self.assertEqual((rep["embedded"], rep["reused"]), (2, 0))
            self.assertEqual(rep["stale_reason"], "forced")

    def test_failed_embed_keeps_the_prior_vector(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            memory_search.ensure_index(home, _cfg(url))
            before = _index(home)["memory:a.md#0"]["vector"]
            (home / "memory" / "a.md").write_text("alpha v2")
            rep = memory_search.ensure_index(home, _cfg(DEAD_URL))
            self.assertEqual(rep["failed"], 1)
            self.assertEqual(rep["embedded"], 0)
            after = _index(home)["memory:a.md#0"]
            self.assertEqual(after["vector"], before,
                             "stale-but-working beats invisible")

    def test_failed_embed_of_a_new_file_leaves_no_poisoned_entry(self):
        with tempfile.TemporaryDirectory() as root:
            home = _home_with(root, {"memory/a.md": "alpha"})
            rep = memory_search.ensure_index(home, _cfg(DEAD_URL))
            self.assertEqual(rep["failed"], 1)
            self.assertNotIn("memory:a.md#0", _index(home))

    def test_index_write_is_atomic(self):
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((str(src), str(dst)))
            return real_replace(src, dst)

        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            with mock.patch.object(memory_search.os, "replace",
                                            spy):
                memory_search.ensure_index(home, _cfg(url))
            self.assertEqual(len(seen), 1)
            src, dst = seen[0]
            self.assertTrue(src.endswith(".tmp"))
            self.assertTrue(dst.endswith("embeddings.json"))
            self.assertFalse(pathlib.Path(src).exists())


if __name__ == "__main__":
    unittest.main()
