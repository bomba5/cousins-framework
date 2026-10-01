"""Media generation: the provider seam, tested off-first.

The perimeter properties lead, because they are the spec's spine: no
config means no generation and no network; a configured-but-unreachable
provider refuses loudly and never reroutes; a request goes to the
declared provider or nowhere. The provider is a fake HTTP service over
a real loopback socket - the wire contract is exercised end to end,
only the asset bytes are scripted.
"""
import http.server
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from cousin_lib.media import (
    MediaError,
    NoProviderConfigured,
    generate,
    load_provider,
)


class _FakeProvider(http.server.BaseHTTPRequestHandler):
    # 1x1 PNG bytes, returned for any image request.
    _PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        _FakeProvider.last_request = body
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self._PNG)))
        self.end_headers()
        self.wfile.write(self._PNG)

    def log_message(self, *args):
        pass


class MediaCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "chat").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "COUSIN_HOME": str(self.home),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _configure(self, url, *, key_file=None):
        (self.root / "config").mkdir(exist_ok=True)
        block = 'url = "%s"\nmodel = "test-model"\ntimeout_s = 5\n' % url
        if key_file:
            block += 'key_file = "%s"\n' % key_file
        (self.root / "config" / "media.toml").write_text(
            "[image]\n" + block)

    def _serve(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _FakeProvider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/gen" % server.server_address[1]


class TestOffByDefault(MediaCase):
    def test_no_config_is_a_refusal_naming_the_file(self):
        with self.assertRaises(NoProviderConfigured) as ctx:
            generate("image", "a cat")
        self.assertIn("config/media.toml", str(ctx.exception))

    def test_no_config_reaches_no_network(self):
        # The refusal must happen before any socket work: load_provider
        # returns None, and generate raises without a request.
        self.assertIsNone(load_provider("image"))


class TestProviderContract(MediaCase):
    def test_generate_posts_the_wire_contract_and_writes_under_home(self):
        self._configure(self._serve())
        path = generate("image", "a violet sunrise")
        self.assertTrue(path.is_file())
        self.assertTrue(str(path).startswith(str(self.home / "chat")))
        self.assertEqual(_FakeProvider.last_request["model"], "test-model")
        self.assertEqual(_FakeProvider.last_request["prompt"],
                         "a violet sunrise")

    def test_key_file_is_sent_as_bearer_not_inlined(self):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "k.key").write_text("s3cret\n")
        self._configure(self._serve(), key_file="config/k.key")
        generate("image", "x")
        # The key is transport auth, never body content.
        self.assertNotIn("s3cret",
                         json.dumps(_FakeProvider.last_request))


class TestNoSilentFallback(MediaCase):
    def test_unreachable_provider_refuses_loudly(self):
        self._configure("http://127.0.0.1:9/nothing-here")
        with self.assertRaises(MediaError) as ctx:
            generate("image", "a cat")
        # Named provider, no reroute to some other vendor.
        self.assertIn("image", str(ctx.exception))

    def test_a_kind_with_no_section_refuses(self):
        self._configure(self._serve())  # only [image]
        with self.assertRaises(NoProviderConfigured):
            generate("voice", "hello")


class TestCli(MediaCase):
    def _main(self, argv):
        import contextlib
        import io

        from cousin_lib.media import image_main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = image_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_gen_writes_a_file_and_does_not_post(self):
        self._configure(self._serve())
        posted = []
        with mock.patch("cousin_lib.media._post_reply",
                        side_effect=lambda config, **kw: posted.append(kw)):
            rc, out, _ = self._main(["gen", "a cat"])
        self.assertEqual(rc, 0)
        self.assertEqual(posted, [])
        self.assertIn("chat/images", out)

    def test_chat_generates_and_posts_with_the_attachment(self):
        """The reply is stored in the cousin's own chat.db, in this
        process: no chat server runs here."""
        import sqlite3
        self._configure(self._serve())
        rc, _, err = self._main(["chat", "a cat", "--user", "Sam"])
        self.assertEqual(rc, 0, err)
        with sqlite3.connect(self.home / "data" / "chat.db") as db:
            rows = db.execute("SELECT reply_to_user, attachment_kind, attachment_path"
                              " FROM messages").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], ("Sam", "image"))
        self.assertIn("chat/images", rows[0][2])

    def test_a_blocked_caption_is_refused_before_anything_is_generated(self):
        # the filter runs first: no provider call, no file, no job row,
        # no post (docs/media.md)
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "outbound-filter.json").write_text(
            '{"terms": ["kestrel-secret"]}')
        url = self._serve()
        self._configure(url)
        calls = []
        with mock.patch("cousin_lib.media.generate",
                        side_effect=lambda *a, **kw: calls.append(a)), \
                mock.patch("cousin_lib.media._post_reply",
                           side_effect=lambda config, **kw: calls.append(kw)):
            rc, _, err = self._main(["chat", "a cat", "--user", "Sam",
                                     "--caption", "about kestrel-secret"])
        self.assertEqual(rc, 3, err)
        self.assertEqual(calls, [])
        self.assertEqual(list((self.home / "chat").rglob("*.*")), [])
        from cousin_lib.jobs import list_jobs
        self.assertEqual(list_jobs(), [])

    def test_unconfigured_cli_refuses_with_exit_2(self):
        rc, _, err = self._main(["gen", "a cat"])
        self.assertEqual(rc, 2)
        self.assertIn("config/media.toml", err)


class TestJobTracking(MediaCase):
    """Every generation the media CLIs run is a row in the jobs store,
    so a slow render shows in the Jobs view like any other job."""

    _main = TestCli._main

    def _jobs(self):
        from cousin_lib.jobs import list_jobs
        return list_jobs()

    def test_a_generation_creates_and_finishes_a_media_job(self):
        self._configure(self._serve())
        rc, out, _ = self._main(["gen", "a cat on a mat"])
        self.assertEqual(rc, 0)
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["kind"], "media")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["spawned_by"], "wren")
        self.assertIn("image", job["title"])
        self.assertIn("a cat on a mat", job["title"])
        self.assertIn(out.strip(), job["result_summary"])

    def test_a_provider_error_finishes_the_job_failed(self):
        self._configure("http://127.0.0.1:9/nothing-here")
        rc, _, _ = self._main(["gen", "a cat"])
        self.assertEqual(rc, 4)
        job = self._jobs()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("MediaError", job["result_summary"])

    def test_an_unconfigured_kind_records_no_job(self):
        # A refusal before any work is not a job.
        rc, _, _ = self._main(["gen", "a cat"])
        self.assertEqual(rc, 2)
        self.assertEqual(self._jobs(), [])


if __name__ == "__main__":
    unittest.main()
