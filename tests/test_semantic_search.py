"""The semantic search leg and its degrade contract.

The contract, in order of importance: soft-degrade is silent ONLY when
nothing was promised. No embedding config means keyword, silently -
the install never claimed semantic search. Embedding configured but
unreachable means keyword PLUS a notice - a quietly dead semantic leg
leaves someone believing they have semantic recall until the belief
costs them a lookup.

The semantic tests run against a fake embedding service over real
HTTP: the framework's embedding contract is exercised end to end,
only the vectors are scripted.
"""
import http.server
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from cousin_lib.memory_search import search


class _FakeEmbedder(http.server.BaseHTTPRequestHandler):
    """Returns vector A for text mentioning sunrise, else vector B -
    enough for cosine to rank the sunrise document first for a query
    the keyword leg cannot match."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        text = body.get("prompt", "")
        vector = ([1.0, 0.0, 0.0] if ("sunrise" in text or "dawn" in text)
                  else [0.0, 1.0, 0.0])
        payload = json.dumps({"embedding": vector}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


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

    def _configure_embedding(self, url):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "embedding.toml").write_text(
            'url = "%s"\nmodel = "test-embed"\ntimeout_s = 2\n' % url)

    def _serve_fake(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _FakeEmbedder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/embed" % server.server_address[1]


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


if __name__ == "__main__":
    unittest.main()
