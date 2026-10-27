"""The incremental, self-healing vector index.

Ported behaviours from an earlier version's incremental-reindex
tests, re-expressed over this repository's explicit-argument API: a
stale or empty embeddings index used to be served forever, and the
only rebuild path re-embedded every file. ensure_index embeds only
what changed, reuses the rest, drops what is gone, and survives a
dead embedding service without poisoning what it already had. The
fake embedder is real HTTP on loopback; only the vectors are scripted.
"""
import array
import contextlib
import hashlib
import json
import math
import os
import pathlib
import random
import sqlite3
import tempfile
import threading
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
    """The stored index, whatever the storage is (SQLite now; every
    behavioural assertion below holds for any storage)."""
    return memory_search._load_index(home) or {}


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

    def test_a_tail_that_would_be_mostly_overlap_joins_the_previous_chunk(self):
        # 150 new chars after the first window would make a 350-char
        # tail that is mostly the previous chunk's end; it merges instead
        body = "".join(chr(65 + i % 26) for i in range(2150))
        self.assertEqual(memory_search._chunk_text(body, size=2000, overlap=200), [body])

    def test_a_tail_wholly_inside_the_previous_chunk_is_not_emitted(self):
        body = "".join(chr(65 + i % 26) for i in range(3700))
        chunks = memory_search._chunk_text(body, size=2000, overlap=200)
        self.assertEqual(chunks, [body[:2000], body[1800:]])

    def test_a_tail_with_as_much_new_text_as_the_overlap_stays_its_own_chunk(self):
        body = "".join(chr(65 + i % 26) for i in range(2200))
        chunks = memory_search._chunk_text(body, size=2000, overlap=200)
        self.assertEqual(chunks, [body[:2000], body[1800:]])

    def test_no_chunk_is_mostly_overlap_at_any_length(self):
        size, overlap = 500, 50
        for length in range(1, 2400, 7):
            body = "".join(chr(65 + i % 26) for i in range(length))
            chunks = memory_search._chunk_text(body, size=size, overlap=overlap)
            self.assertEqual("".join(c[:size - overlap] for c in chunks[:-1]) + chunks[-1],
                             body, length)
            for c in chunks[:-1]:
                self.assertEqual(len(c), size, length)
            self.assertLess(len(chunks[-1]), size + overlap, length)
            if len(chunks) > 1:
                # the tail carries at least `overlap` chars the chunk before lacks
                self.assertGreaterEqual(len(chunks[-1]) - overlap, overlap, length)

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
            index_path = home / "memory" / "vectors.db"
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
            memory_search._write_index_atomic(home, {})
            rep = memory_search.ensure_index(home, _cfg(url))
            self.assertIn("empty index", rep["stale_reason"])
            self.assertIn("memory:a.md#0", _index(home))

    def test_unreadable_index_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            (home / "memory" / "vectors.db").write_text("not a database")
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

    def test_a_write_that_dies_leaves_the_previous_index_intact(self):
        """The store changed from a JSON file written then renamed to
        SQLite written in a transaction, so the mechanism is different
        and the property is the same: a reader never sees a partial
        index, and a write that dies half way leaves what was there."""
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            memory_search.ensure_index(home, _cfg(url))
            before = _index(home)
            self.assertIn("memory:a.md#0", before)

            real = memory_search._pack
            calls = []

            def die(vector):
                calls.append(vector)
                if len(calls) > 1:
                    raise RuntimeError("the write dies half way")
                return real(vector)

            with mock.patch.object(memory_search, "_pack", die):
                with self.assertRaises(RuntimeError):
                    memory_search._write_index_atomic(home, {
                        "memory:a.md#0": before["memory:a.md#0"],
                        "memory:b.md#0": {"mtime": 1.0, "text_hash": "h",
                                          "vector": [1.0, 2.0]},
                    })
            self.assertEqual(_index(home).keys(), before.keys(),
                             "a dead write changes nothing")

    def test_a_reader_never_sees_a_partial_index(self):
        """_pack runs inside the write's transaction, after the delete
        that would empty a non-transactional store, so it is where a
        concurrent reader would catch a partial index. It must see the
        previous contents whole."""
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            memory_search.ensure_index(home, _cfg(url))
            seen = []
            real = memory_search._pack

            def observe(vector):
                seen.append(memory_search._load_index(home) or {})
                return real(vector)

            with mock.patch.object(memory_search, "_pack", observe):
                memory_search._write_index_atomic(home, {
                    "memory:z.md#0": {"mtime": 1.0, "text_hash": "h",
                                      "vector": [3.0]}})
            self.assertTrue(seen)
            self.assertIn("memory:a.md#0", seen[0],
                          "mid-transaction, a reader still sees the old one")
            self.assertEqual(set(_index(home)), {"memory:z.md#0"})


class TestSingleFlight(unittest.TestCase):
    """One refresh per home at a time. A recall past its budget keeps
    refreshing on its own thread, so N operator messages used to mean
    N full re-embeds at once (19 of them were seen, the embedding
    service at 18 s a request)."""

    def _hold_lock(self, home):
        import fcntl
        lock = open(memory_search._lock_path(home), "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.addCleanup(lock.close)
        return lock

    def test_held_lock_makes_a_nonwaiting_pass_return_busy_at_once(self):
        calls = []
        with tempfile.TemporaryDirectory() as root, \
                fake_embedder(calls=calls) as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            self._hold_lock(home)
            rep = memory_search.ensure_index(home, _cfg(url), wait=False)
            self.assertTrue(rep["busy"])
            self.assertEqual(calls, [])
            self.assertFalse((home / "memory" / "vectors.db").exists())

    def test_free_lock_runs_the_pass(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            rep = memory_search.ensure_index(home, _cfg(url), wait=False)
            self.assertFalse(rep["busy"])
            self.assertEqual(rep["embedded"], 1)

    def test_search_during_a_refresh_uses_the_index_as_it_stands(self):
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, {"memory/a.md": "alpha"})
            memory_search.ensure_index(home, _cfg(url))
            (home / "memory" / "b.md").write_text("beta")
            self._hold_lock(home)
            hits, report = memory_search._semantic_search(
                "alpha", home, 5, _cfg(url))
            self.assertTrue(report["busy"])
            self.assertEqual([pathlib.Path(h["path"]).name for h in hits],
                             ["a.md"])


class TestCheckpoint(unittest.TestCase):
    def test_a_pass_that_dies_keeps_what_it_embedded(self):
        files = {"memory/f%02d.md" % i: "body %d" % i for i in range(7)}
        with tempfile.TemporaryDirectory() as root, fake_embedder() as url:
            home = _home_with(root, files)
            real = memory_search._embed
            seen = []

            def dying(text, config):
                if len(seen) == 5:
                    raise KeyboardInterrupt  # the process goes away
                seen.append(text)
                return real(text, config)

            with mock.patch.object(memory_search, "_CHECKPOINT_CHUNKS", 2), \
                    mock.patch.object(memory_search, "_embed", dying):
                with self.assertRaises(KeyboardInterrupt):
                    memory_search.ensure_index(home, _cfg(url))
            kept = _index(home)
            self.assertEqual(len(kept), 4)  # two checkpoints of two
            # The next pass embeds only the rest.
            calls = []
            with fake_embedder(calls=calls) as url2:
                rep = memory_search.ensure_index(home, _cfg(url2))
            self.assertEqual(rep["embedded"], 3)
            self.assertEqual(rep["reused"], 4)


class TestServiceOptions(unittest.TestCase):
    def _payload(self, config):
        captured = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"embedding": [1.0]}'

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data)
            return Resp()

        with mock.patch.object(memory_search.urllib.request, "urlopen",
                               fake_urlopen):
            memory_search._embed("hello", config)
        return captured["body"]

    def test_options_table_passes_through(self):
        body = self._payload({"url": "http://x", "model": "m",
                              "options": {"num_thread": 8}})
        self.assertEqual(body["options"], {"num_thread": 8})

    def test_no_options_sends_no_options_key(self):
        body = self._payload({"url": "http://x", "model": "m"})
        self.assertNotIn("options", body)


QUERY = "where does the heron sleep at night?"


def _heron(text):
    """The query and anything about the heron embed alike; everything
    else is orthogonal to them."""
    return [1.0, 0.0, 0.0] if "heron" in text else [0.0, 1.0, 0.0]


def _scattered(text):
    """64 components that depend on the text alone, spread so that every
    product and every sum rounds: where two ways of scoring differ in
    the last bit, these vectors show it."""
    rng = random.Random(hashlib.sha256(text.encode()).digest())
    return [rng.uniform(-1.0, 1.0) for _ in range(64)]


def _raw_line(topic, content, day="2026-10-26"):
    return json.dumps({"timestamp": "%sT10:00:00+02:00" % day,
                       "topic": topic, "content": content,
                       "truth_level": "L3_COUSIN_CONCLUSION",
                       "source": "decision"}) + "\n"


def _cold():
    """Forget every home's cached reads, as a new process starts."""
    with memory_search._CACHE_LOCK:
        memory_search._CACHES.clear()


def _store_reads(home, run):
    """How many times `run` opened the vector store of `home`."""
    store = str(pathlib.Path(home) / "memory" / "vectors.db")
    real = sqlite3.connect
    opened = []

    def connect(database, *args, **kwargs):
        opened.append(str(database))
        return real(database, *args, **kwargs)

    with mock.patch.object(memory_search.sqlite3, "connect", connect):
        run()
    return opened.count(store)


def _raw_reads(run):
    """The raw memory files `run` opened, in order."""
    opened = []

    def tracked(real):
        def opener(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)
        return opener

    with mock.patch.object(memory_search, "open", tracked(open),
                           create=True), \
            mock.patch.object(memory_search.gzip, "open",
                              tracked(memory_search.gzip.open)):
        run()
    return [path for path in opened if "/memory/raw/" in path]


def _uncached_semantic(query_vector, home, top, config, root):
    """The semantic leg's ranking as it was before the cache: the store
    read and unpacked afresh, and both norms worked out for every
    pair."""
    index = memory_search._load_index(home) or {}
    best = {}
    for key, (coll, path, text) in memory_search._chunks(
            home, config, root).items():
        entry = index.get(key)
        if not entry or not entry.get("vector"):
            continue
        a, b = query_vector, entry["vector"]
        dot = sum(x * y for x, y in zip(a, b))
        norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(
            sum(x * x for x in b))
        score = dot / norm if norm else 0.0
        current = best.get(str(path))
        if current is None or score > current["score"]:
            best[str(path)] = {
                "path": str(path), "collection": coll, "score": score,
                "snippet": memory_search._first_line(text),
                "chunk": int(key.rsplit("#", 1)[1]), "similarity": score}
    return sorted(best.values(), key=lambda h: h["score"],
                  reverse=True)[:top]


class TestOneProcessManyLookups(unittest.TestCase):
    """The runner recalls on every turn, from worker threads of one
    long-lived process, and each lookup read the whole store again:
    every raw file parsed and chunked, every vector unpacked, both norms
    of every pair worked out. On a real home of 6,191 chunks that was
    4.5 to 5.6 s against the runner's 4 s recall budget, and 48 of 77
    turns went without recall. A lookup now reads again only what
    changed, sees all of it, and ranks to the last bit as before."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = _home_with(self.root, {
            "cousin.toml": '[cousin]\nslug = "wren"\nname = "Wren"\n',
            "memory/ports.md":
                "# Ports\n\nThe claimed set excludes every port.\n",
            "notes/walk.md": "# Walk\n\nWe walked along the harbour wall.\n",
            "memory/raw/2026-10-25.jsonl": _raw_line(
                "boiler service", "Mallory services the boiler every March.",
                "2026-10-25"),
            "memory/raw/2026-10-26.jsonl": _raw_line(
                "spare keys", "Toki keeps the spare keys."),
        })
        self.config = self._configure(_heron)

    def _configure(self, vector_for):
        ctx = fake_embedder(vector_for=vector_for)
        url = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "embedding.toml").write_text(
            'url = "%s"\nmodel = "m"\ntimeout_s = 5\n' % url)
        return _cfg(url)

    def _recall(self):
        return memory_search.recall_hits(self.home, QUERY, root=self.root)[0]

    def _write_raw(self, day, topic, content):
        with open(self.home / "memory" / "raw" / ("%s.jsonl" % day), "a") as fh:
            fh.write(_raw_line(topic, content, day))

    def _semantic(self, top=20):
        return memory_search._semantic_search(QUERY, self.home, top,
                                              self.config, root=self.root)[0]

    def test_two_lookups_read_the_store_once_and_the_next_sees_a_write(self):
        # The loops daemon's pass: the store is level with the memory
        # before the turns begin.
        memory_search.ensure_index(self.home, self.config, root=self.root)
        recalled = []
        reads = _store_reads(self.home, lambda: recalled.extend(
            [self._recall(), self._recall()]))
        self.assertEqual(recalled, [[], []],
                         "nothing in memory is about the heron yet")
        self.assertEqual(reads, 1, "two lookups over an unchanged memory"
                                   " read and unpack the store once")
        self._write_raw("2026-10-26", "heron roost",
                        "The heron sleeps in the reeds by the sluice.")
        kept = self._recall()
        self.assertTrue(any(k.startswith("heron roost (raw:") for k in kept),
                        kept)

    def test_an_unchanged_raw_file_is_not_read_again(self):
        raw = self.home / "memory" / "raw"
        day1, day2 = str(raw / "2026-10-25.jsonl"), str(raw / "2026-10-26.jsonl")
        self.assertEqual(sorted(_raw_reads(
            lambda: memory_search._raw_entries(self.home))), [day1, day2])
        self.assertEqual(_raw_reads(
            lambda: memory_search._raw_entries(self.home)), [],
            "a second read over unchanged files opens none of them")
        self._write_raw("2026-10-26", "heron roost", "The heron sleeps.")
        rows = []
        self.assertEqual(_raw_reads(
            lambda: rows.extend(memory_search._raw_entries(self.home))),
            [day2], "only the file that changed is read again")
        self.assertEqual([text.split("\n")[1] for _c, _k, text, _m in rows],
                         ["boiler service", "spare keys", "heron roost"])

    def test_the_cached_leg_ranks_to_the_last_bit_as_an_uncached_pass(self):
        self.config = self._configure(_scattered)
        files = {"memory/long.md": "# Long\n\n" + " ".join(
                     "word%d" % i for i in range(1500)),
                 "notes/copy.md":
                     "# Ports\n\nThe claimed set excludes every port.\n"}
        for i in range(12):
            files["notes/n%02d.md" % i] = "# Note %d\n\nheron fact %d\n" % (i, i)
        for rel, body in files.items():
            (self.home / rel).write_text(body)
        for i in range(10):
            self._write_raw("2026-10-26", "entry %d" % i, "fact number %d" % i)
        memory_search.ensure_index(self.home, self.config, root=self.root)
        _cold()
        expected = _uncached_semantic(memory_search._embed(QUERY, self.config),
                                      self.home, 20, self.config, self.root)
        self.assertEqual(len(expected), 20)
        _cold()
        self.assertEqual(self._semantic(), expected, "cold")
        self.assertEqual(self._semantic(), expected, "warm")
        _cold()
        first = memory_search.search(QUERY, home=self.home, root=self.root,
                                     record=False)
        again = memory_search.search(QUERY, home=self.home, root=self.root,
                                     record=False)
        self.assertEqual(first, again)

    def test_a_commit_still_in_the_wal_is_seen(self):
        store = self.home / "memory" / "vectors.db"
        memory_search._write_index_atomic(self.home, {"memory:a.md#0": {
            "mtime": 1.0, "text_hash": "a", "vector": [1.0, 0.0]}})
        self.assertEqual(set(memory_search._cached_index(self.home).entries),
                         {"memory:a.md#0"})
        # Another process commits while a third holds the store open, so
        # the commit stays in vectors.db-wal: vectors.db is untouched.
        reader = sqlite3.connect(store)
        self.addCleanup(reader.close)
        reader.execute("SELECT count(*) FROM vectors").fetchone()
        before = store.stat()
        with contextlib.closing(sqlite3.connect(store)) as writer, writer:
            writer.execute("INSERT INTO vectors VALUES (?, ?, ?, ?)",
                           ("memory:b.md#0", 1.0, "b",
                            memory_search._pack([0.0, 1.0])))
        after = store.stat()
        self.assertEqual((before.st_size, before.st_mtime_ns),
                         (after.st_size, after.st_mtime_ns))
        self.assertEqual(set(memory_search._cached_index(self.home).entries),
                         {"memory:a.md#0", "memory:b.md#0"})

    def test_a_reread_unpacks_only_the_rows_that_changed(self):
        memory_search.ensure_index(self.home, self.config, root=self.root)
        first = memory_search._cached_index(self.home)
        memory_search._index_norms(first, list(first.entries))
        kept = first.entries["memory:ports.md#0"]["vector"]
        (self.home / "notes" / "walk.md").write_text(
            "# Walk\n\nThe heron watched us walk.\n")
        memory_search.ensure_index(self.home, self.config, root=self.root)
        second = memory_search._cached_index(self.home)
        self.assertIsNot(second, first, "the store changed: read again")
        self.assertIs(second.entries["memory:ports.md#0"]["vector"], kept,
                      "an unchanged row keeps its vector")
        self.assertIn("memory:ports.md#0", second.norms, "and its norm")
        self.assertEqual(list(second.entries["notes:walk.md#0"]["vector"]),
                         [1.0, 0.0, 0.0], "a changed row is read anew")
        self.assertNotIn("notes:walk.md#0", second.norms)

    def test_lookups_on_threads_at_once_agree(self):
        memory_search.ensure_index(self.home, self.config, root=self.root)
        self._write_raw("2026-10-26", "heron roost", "The heron sleeps.")
        expected = self._semantic(10)
        self.assertEqual(expected[0]["similarity"], 1.0)
        _cold()
        results, errors = [], []

        def lookup():
            try:
                results.append(self._semantic(10))
            except Exception as err:  # noqa: BLE001 - asserted below
                errors.append(err)

        threads = [threading.Thread(target=lookup) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, [expected] * 4)

    def test_a_process_keeps_the_last_homes_only(self):
        homes = [_home_with(self.root / str(i), {
            "memory/raw/2026-10-26.jsonl": _raw_line("t", "c")})
            for i in range(memory_search._CACHE_HOMES + 2)]
        for home in homes:
            memory_search._raw_entries(home)
        self.assertEqual(list(memory_search._CACHES),
                         [str(h) for h in homes[-memory_search._CACHE_HOMES:]])


class TestCosine(unittest.TestCase):
    def test_the_cosine_is_the_old_arithmetic_to_the_last_bit(self):
        """The norms are kept and the dot product is map(operator.mul),
        not zip: still the same products added in the same order, so
        the same float, to the last bit, for every pair."""
        def old(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(
                sum(x * x for x in b))
            return dot / norm if norm else 0.0

        rng = random.Random(467)
        for _ in range(300):
            n = rng.choice([3, 64, 768])
            query = [rng.uniform(-1.0, 1.0) for _ in range(n)]
            stored = array.array("f", [rng.gauss(0.0, 0.05) for _ in range(n)])
            want = old(query, list(stored))
            self.assertEqual(memory_search._cosine(query, list(stored)), want)
            self.assertEqual(memory_search._cosine_normed(
                query, memory_search._norm(query),
                stored, memory_search._norm(stored)), want)
        for a, b in (([0.0, 0.0], [1.0, 2.0]), ([1, 2, 3], [1.0, 0.5]),
                     ([], []), ([3, 4], [4, 3])):
            self.assertEqual(memory_search._cosine(a, b), old(a, b))


if __name__ == "__main__":
    unittest.main()
