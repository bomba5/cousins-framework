"""The console as the supervisor and the image start it: `python -m
cousin_lib.console.app serve`. A route's own refusal answers with its
status there too, not a 500 (one HttpError class, not two)."""
import http.client
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from cousin_lib.console import auth
from tests._hermetic import HermeticCase

REPO = pathlib.Path(__file__).resolve().parents[2]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestConsoleAsAModule(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "cousins").mkdir()
        auth.Users(self.root / "config" / "console-users.json").set_password("ana", "a-long-password")
        self.port = _free_port()
        env = dict(os.environ, FRAMEWORK_ROOT=str(self.root), PYTHONPATH=str(REPO))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "cousin_lib.console.app", "serve", "--root", str(self.root),
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self._stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if self.call("GET", "/api/version")[0] == 200:
                    return
            except OSError:
                pass
            if self.proc.poll() is not None:
                self.fail("the console exited: %s" % self.proc.stderr.read().decode()[-800:])
            time.sleep(0.1)
        self.fail("the console never answered on port %d" % self.port)

    def _stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stderr.close()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_a_wrong_password_is_401_not_500(self):
        status, body = self.call("POST", "/api/auth/login",
                                 {"user": "ana", "password": "not-it"})
        self.assertEqual(status, 401, body)

    def test_a_route_refusal_keeps_its_status(self):
        status, body = self.call("POST", "/api/auth/login", {"user": "ana"})
        self.assertEqual(status, 400, body)
