"""Speech to text: the [transcribe] provider seam, tested off-first.

No config means no request; a configured provider gets the OpenAI
transcription request (multipart: file, model, language,
response_format) over a real loopback socket, and its failure is a
MediaError naming it, never a reroute.
"""
import email.parser
import email.policy
import http.server
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from cousin_lib.media import MediaError, NoProviderConfigured
from cousin_lib.transcribe import transcribe


class _FakeWhisper(http.server.BaseHTTPRequestHandler):
    answer = (200, "application/json", b'{"text": " hello wren "}')
    last = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
        parts = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            parts[name] = (part.get_filename(), part.get_payload(decode=True))
        type(self).last = {"parts": parts, "auth": self.headers.get("Authorization")}
        status, ctype, body = type(self).answer
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TranscribeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        patcher = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        _FakeWhisper.answer = (200, "application/json", b'{"text": " hello wren "}')
        _FakeWhisper.last = None
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _FakeWhisper)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d/inference" % self.server.server_port

    def _config(self, extra=""):
        (self.root / "config" / "media.toml").write_text(
            '[transcribe]\nurl = "%s"\n%s' % (self.url, extra))


class TestOffByDefault(TranscribeCase):
    def test_no_section_means_no_request(self):
        with self.assertRaises(NoProviderConfigured):
            transcribe(b"OGG", "voice/file_7.oga")
        self.assertIsNone(_FakeWhisper.last)


class TestTheWire(TranscribeCase):
    def test_the_audio_and_its_fields_go_to_the_provider(self):
        self._config('model = "large-v3-turbo"\nlanguage = "da"\n')
        self.assertEqual(transcribe(b"OGGBYTES", "voice/file_7.oga"), "hello wren")
        parts = _FakeWhisper.last["parts"]
        self.assertEqual(parts["file"], ("file_7.oga", b"OGGBYTES"))
        self.assertEqual(parts["model"][1], b"large-v3-turbo")
        self.assertEqual(parts["language"][1], b"da")
        self.assertEqual(parts["response_format"][1], b"json")
        self.assertIsNone(_FakeWhisper.last["auth"])

    def test_no_language_means_the_provider_detects_it(self):
        self._config()
        transcribe(b"OGG", "voice/file_7.oga")
        self.assertNotIn("language", _FakeWhisper.last["parts"])

    def test_a_prompt_rides_along(self):
        self._config('prompt = "Sam, wren"\n')
        transcribe(b"OGG", "a.oga")
        self.assertEqual(_FakeWhisper.last["parts"]["prompt"][1], "Sam, wren".encode())

    def test_a_key_file_is_sent_as_bearer(self):
        (self.root / "config" / "stt.key").write_text("k-123\n")
        self._config('key_file = "config/stt.key"\n')
        transcribe(b"OGG", "a.oga")
        self.assertEqual(_FakeWhisper.last["auth"], "Bearer k-123")

    def test_a_plain_text_answer_is_the_text(self):
        self._config()
        _FakeWhisper.answer = (200, "text/plain", b"hello there\n")
        self.assertEqual(transcribe(b"OGG", "a.oga"), "hello there")


class TestFailures(TranscribeCase):
    def test_an_http_error_names_the_provider(self):
        self._config()
        _FakeWhisper.answer = (500, "application/json", b'{"error": "boom"}')
        with self.assertRaises(MediaError) as caught:
            transcribe(b"OGG", "a.oga")
        self.assertIn(self.url, str(caught.exception))

    def test_an_unreachable_provider_is_a_media_error(self):
        self._config()
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(MediaError):
            transcribe(b"OGG", "a.oga")

    def test_json_without_text_is_refused(self):
        self._config()
        _FakeWhisper.answer = (200, "application/json", json.dumps({"segments": []}).encode())
        with self.assertRaises(MediaError):
            transcribe(b"OGG", "a.oga")


if __name__ == "__main__":
    unittest.main()
