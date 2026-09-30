"""Proactive recall: a colleague remembers without being asked.

An operator message long enough to carry meaning is searched against
the cousin's own memory, and the best hits ride along as one
`[fw-recall] ...` line (memory_search.recall_context: the gates and the
line the runner's prompt hook adds as context, runner/hooks.py). The
stored message never changes: the history is what the operator said,
not what the cousin was reminded of.

Threshold rule: with the embedding seam configured a hit needs its
semantic similarity at or above `[recall] min_score`; keyword-only
installs keep every FTS hit, because a match there already means a
term matched. Thresholds come from `config/embedding.toml [recall]`,
defaults when absent; the per-cousin switch is `cousin.toml [memory]
proactive_recall`.
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory_search
from cousin_lib.config import CousinConfig
from tests._fakes import fake_embedder

VECTOR_A = [1.0, 0.0, 0.0]
VECTOR_B = [0.0, 1.0, 0.0]
LONG = "a long enough operator message that would trigger recall here"


def _sunrise_vectors(text):
    return VECTOR_A if ("sunrise" in text or "dawn" in text) else VECTOR_B



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

    def _boot(self, *, operator="Sam", recall=None, keyword_only=False):
        toml = '[cousin]\nslug = "wren"\nname = "Wren"\n'
        if operator:
            toml += '[operator]\nname = "%s"\n' % operator
        if recall is not None or keyword_only:
            toml += "[memory]\n"
        if recall is not None:
            toml += "proactive_recall = %s\n" % ("true" if recall else "false")
        if keyword_only:
            toml += "recall_keyword_only = true\n"
        (self.home / "cousin.toml").write_text(toml)
        self.calls = []
        return CousinConfig.load(self.home)

    def _send(self, config, message):
        """What the cousin is handed for an operator message: the message,
        and the recall line as its context."""
        line = memory_search.recall_context(self.home, message, config=config)
        self.calls.append({"message": message, "context": line or ""})
        return 200, {}

    def _delivered(self):
        """The message and its recall line, `message + " " + context`:
        every assertion below reads what the cousin is handed."""
        self.assertEqual(len(self.calls), 1)
        return self._text(self.calls[0])

    @staticmethod
    def _text(call):
        context = call.get("context") or ""
        return call["message"] + (" " + context if context else "")

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
        config = self._boot()
        message = "tell me again about the sunrise over the ridge"
        status, _ = self._send(config, message)
        self.assertEqual(status, 200)
        delivered = self._delivered()
        self.assertEqual(
            delivered,
            message + " [fw-recall] possibly relevant from your memory:"
            " Sky notes (memory:sky.md) - cousin-memory search for"
            " details; ignore if not.")
        self.assertNotIn("\n", delivered)
        self.assertNotIn("Ports", delivered)  # similarity 0 < min_score

    def test_keyword_only_install_is_silent_unless_opted_in(self):
        # No embedding seam and no opt-in: an OR-joined keyword match is
        # too loose to interrupt with, so nothing is appended.
        (self.home / "memory" / "upkeep.md").write_text(
            "# Grinder upkeep\n\nThe burr grinder needs descaling.\n")
        config = self._boot()
        message = "when did the burr grinder last get descaling done?"
        self._send(config, message)
        self.assertEqual(self._delivered(), message)

    def test_keyword_only_install_keeps_every_fts_hit_when_opted_in(self):
        # No embedding seam, [memory] recall_keyword_only = true: an FTS
        # match already means a term matched.
        (self.home / "memory" / "upkeep.md").write_text(
            "# Grinder upkeep\n\nThe burr grinder needs descaling.\n")
        (self.home / "notes").mkdir()
        (self.home / "notes" / "2026-01-01-ports.md").write_text(
            "The claimed set excludes every port.\n")
        config = self._boot(keyword_only=True)
        message = "when did the burr grinder last get descaling done?"
        self._send(config, message)
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
        config = self._boot(keyword_only=True)
        self._send(config, "when did the burr grinder last get descaling?")
        self.assertIn("2026-01-01-grinder (notes:2026-01-01-grinder.md)",
                      self._delivered())


class TestTriggers(RecallCase):
    def _never(self, *args, **kw):
        raise AssertionError("search must not run")

    def test_short_message_is_not_searched(self):
        config = self._boot()
        short = "x" * 23
        with mock.patch.object(memory_search, "search", self._never):
            self._send(config, short)
        self.assertEqual(self._delivered(), short)

    def test_opt_out_in_cousin_toml_is_honored(self):
        config = self._boot(recall=False)
        with mock.patch.object(memory_search, "search", self._never):
            self._send(config, LONG)
        self.assertEqual(self._delivered(), LONG)

    def test_nothing_above_threshold_appends_nothing(self):
        self._configure_embedding("http://127.0.0.1:9/unused")
        (self.home / "memory" / "a.md").write_text("# A\n\nbody\n")
        config = self._boot()
        hits = [self._hit("a.md", similarity=0.1)]
        with mock.patch.object(memory_search, "search",
                               return_value=(hits, None)):
            self._send(config, LONG)
        self.assertEqual(self._delivered(), LONG)

    def test_configured_seam_ignores_hits_without_similarity(self):
        # The semantic leg was promised; a keyword-only hit under it
        # (service down) has nothing to say about meaning.
        self._configure_embedding("http://127.0.0.1:9/unused")
        (self.home / "memory" / "a.md").write_text("# A\n\nbody\n")
        config = self._boot()
        hits = [self._hit("a.md")]
        with mock.patch.object(memory_search, "search",
                               return_value=(hits, "service unreachable")):
            self._send(config, LONG)
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
        config = self._boot(keyword_only=True)
        seen, search = self._capture([self._hit("a.md"), self._hit("b.md")])
        with mock.patch.object(memory_search, "search", search):
            self._send(config, "x" * 23)
            self._send(config, "y" * 24)
        self.assertEqual(len(seen), 1)
        query, kw = seen[0]
        self.assertEqual(query, "y" * 24)
        self.assertEqual(kw["top"], 3)
        self.assertEqual(pathlib.Path(kw["home"]), self.home)
        self.assertEqual(
            self._text(self.calls[1]),
            "y" * 24 + " [fw-recall] possibly relevant from your memory:"
            " a (memory:a.md); b (memory:b.md) - cousin-memory search"
            " for details; ignore if not.")

    def test_thresholds_come_from_the_recall_table(self):
        self._configure_embedding(
            "http://127.0.0.1:9/unused",
            "[recall]\nmin_chars = 40\nmin_score = 0.9\ntop = 1\n")
        for name in ("a.md", "b.md"):
            (self.home / "memory" / name).write_text("# %s\n" % name[0])
        config = self._boot()
        seen, search = self._capture([self._hit("a.md", similarity=0.95),
                                      self._hit("b.md", similarity=0.5)])
        with mock.patch.object(memory_search, "search", search):
            self._send(config, "z" * 39)
            self._send(config, "z" * 40)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1]["top"], 1)
        delivered = self._text(self.calls[1])
        self.assertIn("a (memory:a.md)", delivered)
        self.assertNotIn("b (memory:b.md)", delivered)


if __name__ == "__main__":
    unittest.main()
