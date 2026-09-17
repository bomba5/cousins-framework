"""Proactive recall on /api/send: a colleague remembers without being
asked.

An operator message long enough to carry meaning is searched against
the cousin's own memory, and the best hits ride along on the DELIVERED
line as one `[fw-recall] ...` suffix. The stored message never changes:
the history is what the operator said, not what the cousin was
reminded of. Best-effort by contract: a dead search costs nothing but
the suffix.

Threshold rule: with the embedding seam configured a hit needs its
semantic similarity at or above `[recall] min_score`; keyword-only
installs keep every FTS hit, because a match there already means a
term matched. Thresholds come from `config/embedding.toml [recall]`,
defaults when absent; the per-cousin switch is `cousin.toml [memory]
proactive_recall`.
"""
import json
import os
import pathlib
import tempfile
import unittest
import urllib.request
from unittest import mock

from cousin_lib import memory_search
from cousin_lib.config import CousinConfig
from cousin_lib.server import app
from cousin_lib.server.app import ChatServer
from tests._fakes import fake_embedder

VECTOR_A = [1.0, 0.0, 0.0]
VECTOR_B = [0.0, 1.0, 0.0]
LONG = "a long enough operator message that would trigger recall here"


def _sunrise_vectors(text):
    return VECTOR_A if ("sunrise" in text or "dawn" in text) else VECTOR_B


def _bridge_similarity(real_search):
    """TEMPORARY, until the search module's hits carry "similarity"
    (owned by the usage-weighted recall task): fill the key from the
    scripted vector rule when the real search left it out. A no-op once
    the real search supplies it, so the end-to-end test then runs
    unbridged; delete this helper at that point."""
    def search(query, **kw):
        hits, notice = real_search(query, **kw)
        for hit in hits:
            if "similarity" not in hit:
                body = pathlib.Path(hit["path"]).read_text()
                hit["similarity"] = 1.0 if "sunrise" in body else 0.0
        return hits, notice
    return search


class RecallCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        # No framework root unless a test declares one: the default
        # install promised no semantic leg.
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["PATH"] = "/usr/bin:/bin"

    def _boot(self, *, operator="Sam", recall=None):
        toml = ('[cousin]\nslug = "wren"\nname = "Wren"\n'
                "[chat]\nport = 0\n")
        if operator:
            toml += '[operator]\nname = "%s"\n' % operator
        if recall is not None:
            toml += "[memory]\nproactive_recall = %s\n" % (
                "true" if recall else "false")
        (self.home / "cousin.toml").write_text(toml)
        self.calls = []
        server = ChatServer(CousinConfig.load(self.home),
                            deliver=lambda **kw: self.calls.append(kw))
        server.start()
        self.addCleanup(server.stop)
        return server

    def _send(self, server, message, user="Sam"):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/send" % server.port,
            data=json.dumps({"user": user, "message": message}).encode())
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())

    def _history(self, server, user="Sam"):
        url = "http://127.0.0.1:%d/api/history?user=%s" % (server.port,
                                                           user)
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())["messages"]

    def _delivered(self):
        self.assertEqual(len(self.calls), 1)
        return self.calls[0]["message"]

    def _configure_embedding(self, url, extra=""):
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "embedding.toml").write_text(
            'url = "%s"\nmodel = "test-embed"\ntimeout_s = 2\n%s'
            % (url, extra))

    def _serve_fake(self, vector_for=_sunrise_vectors):
        ctx = fake_embedder(vector_for=vector_for)
        url = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return url

    def _hit(self, name, similarity=None, collection="memory"):
        path = self.home / collection / name
        hit = {"path": str(path), "collection": collection, "score": 0.02,
               "snippet": "", "chunk": 0}
        if similarity is not None:
            hit["similarity"] = similarity
        return hit


class TestEndToEnd(RecallCase):
    def test_planted_memory_rides_the_delivery_and_never_the_store(self):
        (self.home / "memory" / "sky.md").write_text(
            "# Sky notes\n\nThe sunrise over the ridge was violet.\n")
        (self.home / "memory" / "ports.md").write_text(
            "# Ports\n\nThe claimed set excludes every port.\n")
        self._configure_embedding(self._serve_fake())
        server = self._boot()
        message = "tell me again about the sunrise over the ridge"
        with mock.patch.object(app.memory_search, "search",
                               _bridge_similarity(memory_search.search)):
            status, _ = self._send(server, message)
        self.assertEqual(status, 200)
        delivered = self._delivered()
        self.assertEqual(
            delivered,
            message + " [fw-recall] possibly relevant from your memory:"
            " Sky notes (memory:sky.md) - cousin-memory search for"
            " details; ignore if not.")
        self.assertNotIn("\n", delivered)
        self.assertNotIn("Ports", delivered)  # similarity 0 < min_score
        self.assertEqual([m["message"] for m in self._history(server)],
                         [message])

    def test_keyword_only_install_keeps_every_fts_hit(self):
        # No embedding seam: an FTS match already means a term matched.
        (self.home / "memory" / "upkeep.md").write_text(
            "# Grinder upkeep\n\nThe burr grinder needs descaling.\n")
        (self.home / "notes").mkdir()
        (self.home / "notes" / "2026-01-01-ports.md").write_text(
            "The claimed set excludes every port.\n")
        server = self._boot()
        message = "when did the burr grinder last get descaling done?"
        self._send(server, message)
        delivered = self._delivered()
        self.assertTrue(delivered.startswith(message + " [fw-recall] "))
        self.assertIn("Grinder upkeep (memory:upkeep.md)", delivered)
        self.assertTrue(delivered.endswith(
            " - cousin-memory search for details; ignore if not."))
        self.assertEqual(delivered.count("[fw-recall]"), 1)

    def test_heading_less_file_is_named_by_its_stem(self):
        (self.home / "notes").mkdir()
        (self.home / "notes" / "2026-01-01-grinder.md").write_text(
            "The burr grinder needs descaling every 200 shots.\n")
        server = self._boot()
        self._send(server, "when did the burr grinder last get descaling?")
        self.assertIn("2026-01-01-grinder (notes:2026-01-01-grinder.md)",
                      self._delivered())


class TestTriggers(RecallCase):
    def _never(self, *args, **kw):
        raise AssertionError("search must not run")

    def test_short_message_is_not_searched(self):
        server = self._boot()
        short = "x" * 23
        with mock.patch.object(app.memory_search, "search", self._never):
            self._send(server, short)
        self.assertEqual(self._delivered(), short)

    def test_opt_out_in_cousin_toml_is_honored(self):
        server = self._boot(recall=False)
        with mock.patch.object(app.memory_search, "search", self._never):
            self._send(server, LONG)
        self.assertEqual(self._delivered(), LONG)

    def test_non_operator_sender_is_not_searched(self):
        server = self._boot()
        with mock.patch.object(app.memory_search, "search", self._never):
            self._send(server, LONG, user="Peer")
        self.assertEqual(self._delivered(), LONG)

    def test_no_configured_operator_means_no_recall(self):
        server = self._boot(operator=None)
        with mock.patch.object(app.memory_search, "search", self._never):
            self._send(server, LONG)
        self.assertEqual(self._delivered(), LONG)

    def test_search_failure_is_swallowed_and_delivery_proceeds(self):
        server = self._boot()
        with mock.patch.object(app.memory_search, "search",
                               side_effect=RuntimeError("index on fire")):
            status, _ = self._send(server, LONG)
        self.assertEqual(status, 200)
        self.assertEqual(self._delivered(), LONG)

    def test_nothing_above_threshold_appends_nothing(self):
        self._configure_embedding("http://127.0.0.1:9/unused")
        (self.home / "memory" / "a.md").write_text("# A\n\nbody\n")
        server = self._boot()
        hits = [self._hit("a.md", similarity=0.1)]
        with mock.patch.object(app.memory_search, "search",
                               return_value=(hits, None)):
            self._send(server, LONG)
        self.assertEqual(self._delivered(), LONG)

    def test_configured_seam_ignores_hits_without_similarity(self):
        # The semantic leg was promised; a keyword-only hit under it
        # (service down) has nothing to say about meaning.
        self._configure_embedding("http://127.0.0.1:9/unused")
        (self.home / "memory" / "a.md").write_text("# A\n\nbody\n")
        server = self._boot()
        hits = [self._hit("a.md")]
        with mock.patch.object(app.memory_search, "search",
                               return_value=(hits, "service unreachable")):
            self._send(server, LONG)
        self.assertEqual(self._delivered(), LONG)


class TestThresholds(RecallCase):
    def _capture(self, hits):
        seen = []

        def search(query, **kw):
            seen.append((query, kw))
            return hits, None
        return seen, search

    def test_defaults_when_no_embedding_config(self):
        for name in ("a.md", "b.md", "c.md"):
            (self.home / "memory" / name).write_text("# %s\n" % name[0])
        server = self._boot()
        seen, search = self._capture([self._hit("a.md"), self._hit("b.md")])
        with mock.patch.object(app.memory_search, "search", search):
            self._send(server, "x" * 23)
            self._send(server, "y" * 24)
        self.assertEqual(len(seen), 1)
        query, kw = seen[0]
        self.assertEqual(query, "y" * 24)
        self.assertEqual(kw["top"], 3)
        self.assertEqual(pathlib.Path(kw["home"]), self.home)
        self.assertEqual(
            self.calls[1]["message"],
            "y" * 24 + " [fw-recall] possibly relevant from your memory:"
            " a (memory:a.md); b (memory:b.md) - cousin-memory search"
            " for details; ignore if not.")

    def test_thresholds_come_from_the_recall_table(self):
        self._configure_embedding(
            "http://127.0.0.1:9/unused",
            "[recall]\nmin_chars = 40\nmin_score = 0.9\ntop = 1\n")
        for name in ("a.md", "b.md"):
            (self.home / "memory" / name).write_text("# %s\n" % name[0])
        server = self._boot()
        seen, search = self._capture([self._hit("a.md", similarity=0.95),
                                      self._hit("b.md", similarity=0.5)])
        with mock.patch.object(app.memory_search, "search", search):
            self._send(server, "z" * 39)
            self._send(server, "z" * 40)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1]["top"], 1)
        delivered = self.calls[1]["message"]
        self.assertIn("a (memory:a.md)", delivered)
        self.assertNotIn("b (memory:b.md)", delivered)


if __name__ == "__main__":
    unittest.main()
