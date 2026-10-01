"""POST /peer/send (one inbound surface): another install's
cousin reaches one of ours with a message signed by the secret the two
installs share (config/external-peers.toml `inbound_token_file`;
`Authorization: HMAC <sender>:<hex>`, the secret never on the
wire), behind the network guard, with no console session. The signature
names the sender: the body's `user` is ignored, and the message is shown
under the entry's `name`. A peer needs a `reach`, and anything
outside it answers 404 like an absent cousin; a replay is refused. Until
the signature verifies, every refusal is the same 401 (an
unknown sender, a bad signature, a malformed body and an unusable
entry or secret file look alike, so a caller cannot list the peers); an
authenticated peer whose entry is unusable gets a generic 503; the detail
of either goes to the log. A peer signature opens nothing under /api/,
a console session or a bearer token nothing under /peer/. The sending
side signs and never sends its secret."""
import json
import os
import pathlib
import sqlite3
import time
import urllib.error
import urllib.request
from unittest import mock

from cousin_lib import chat
from cousin_lib.config import CousinConfig, FrameworkConfig
from cousin_lib.console import peer_routes
from tests.console._harness import ConsoleCase

SECRET = "peer-secret-4f9a1c0b27de81d2e4f09a3c5c7e9b1d"


def _messages(home):
    path = pathlib.Path(home) / "data" / "chat.db"
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        return db.execute("SELECT user, message FROM messages ORDER BY id").fetchall()


class PeerCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.home = self.cousin("wren", port=None, operator="Priya",
                                extra='\n[agent]\nrunner = "sdk"\n')
        self.sam = self.cousin("sam", port=None, extra='\n[agent]\nrunner = "sdk"\n')
        secrets = self.root / ".secrets" / "peers"
        secrets.mkdir(parents=True)
        self.secret_file = secrets / "kestrel"
        self.secret_file.write_text(SECRET + "\n")
        os.chmod(self.secret_file, 0o600)
        self.peers(reach=["wren"])
        self.logged = []
        p = mock.patch.object(peer_routes, "_log", side_effect=self.logged.append)
        p.start(); self.addCleanup(p.stop)

    def peers(self, reach, slug="kestrel", name="Kestrel"):
        text = ('[peers.%s]\nurl = "http://192.0.2.20:8600"\n'
                'inbound_token_file = ".secrets/peers/kestrel"\nname = "%s"\n' % (slug, name))
        if reach is not None:
            text += "reach = %s\n" % json.dumps(reach)
        (self.root / "config" / "external-peers.toml").write_text(text)

    def body(self, **kw):
        return dict({"to": "wren", "message": "the greenhouse report is ready",
                     "msg_id": "kestrel-0000000001", "sent_at": round(time.time(), 3)}, **kw)

    def signed(self, body, sender="kestrel", secret=SECRET):
        sig = chat.peer_signature(secret, sender, body["to"], body["sent_at"], body["msg_id"],
                                  body["message"])
        return "HMAC %s:%s" % (sender, sig)

    def peer_send(self, body, auth="sign", raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/peer/send" % self.server.port,
                                     data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        if auth == "sign":
            auth = self.signed(body)
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")


class TestPeerSend(PeerCase):
    def test_the_signature_is_the_sender(self):
        self.serve()
        status, body = self.peer_send(self.body(user="Priya"))          # `user` is ignored
        self.assertEqual(status, 200, body)
        self.assertEqual(_messages(self.home), [("Kestrel", "the greenhouse report is ready")])

    def test_no_signature_a_bearer_a_wrong_one_or_a_tampered_body_is_refused(self):
        self.serve()
        body = self.body()
        tampered = dict(body, message="delete the backups")
        for auth, sent in ((None, body), ("Bearer %s" % SECRET, body),
                           (self.signed(body, secret="guess"), body),
                           (self.signed(body, sender="toki"), body),
                           (self.signed(body), tampered)):
            self.assertEqual(self.peer_send(sent, auth=auth)[0], 401, auth)
        self.assertEqual(_messages(self.home), [])

    def test_a_replay_is_refused(self):
        self.serve()
        body = self.body()
        self.assertEqual(self.peer_send(body)[0], 200)
        status, answer = self.peer_send(body)
        self.assertEqual(status, 409, answer)
        self.assertEqual(len(_messages(self.home)), 1)

    def test_outside_the_reach_is_absent(self):
        self.peers(reach=["sam"])
        self.serve()
        outside = self.peer_send(self.body())
        missing = self.peer_send(self.body(to="nobody", msg_id="kestrel-0000000003"))
        self.assertEqual((outside[0], missing[0]), (404, 404))
        self.assertEqual(outside[1]["error"].replace("'wren'", "<to>"),
                         missing[1]["error"].replace("'nobody'", "<to>"))
        self.assertEqual(self.peer_send(self.body(to="sam", msg_id="kestrel-0000000002"))[0], 200)
        self.assertEqual(_messages(self.home), [])

    def test_a_peer_with_no_reach_is_refused_generically(self):
        self.peers(reach=None)
        self.serve()
        self.assertEqual(self.peer_send(self.body()), (503, {"error": "external peers unavailable"}))
        self.assertIn("reach is required", " ".join(self.logged))

    def test_a_secret_file_others_can_read_says_nothing_on_the_wire(self):
        """The detail goes to the log, never to the caller."""
        os.chmod(self.secret_file, 0o644)
        self.serve()
        self.assertEqual(self.peer_send(self.body()), (401, {"error": "unauthorized"}))
        self.assertIn("chmod 600", " ".join(self.logged))
        self.assertEqual(_messages(self.home), [])

    def test_before_the_signature_every_refusal_is_the_same(self):
        """Nothing tells a known sender from an unknown one."""
        self.serve()
        body = self.body()
        bad = "HMAC kestrel:" + "0" * 64
        answers = [
            self.peer_send(body, auth=self.signed(body, sender="nobody")),     # unknown sender
            self.peer_send(body, auth=bad),                                    # a wrong signature
            self.peer_send(None, auth=bad, raw=b"{not json"),                  # malformed JSON
            self.peer_send(None, auth=bad, raw=b"[1, 2]"),                     # not an object
            self.peer_send(dict(body, sent_at="now"), auth=bad),               # no usable fields
        ]
        (self.root / "config" / "external-peers.toml").write_text("[peers.kestrel\n")
        answers.append(self.peer_send(body))                                  # an unusable file
        self.peers(reach=["wren"])
        os.chmod(self.secret_file, 0o644)
        answers.append(self.peer_send(body))                                  # an unusable secret
        self.assertEqual(answers, [(401, {"error": "unauthorized"})] * len(answers))
        self.assertEqual(_messages(self.home), [])

    def test_a_peer_that_shares_a_local_cousins_slug_is_refused(self):
        """An entry [peers.sam] would be threaded as the local Sam.
        The test once built two bodies, one sent and one signed; a
        clock tick between them (a loaded full run) signed another sent_at,
        so the signature failed (401) before the slug check (503). It runs
        under a clock that ticks at every read, which made it fail every
        time with two bodies."""
        self.peers(reach=["wren"], slug="sam", name="Remote")
        self.serve()
        start = time.time()
        clock = iter(start + 0.001 * n for n in range(1000000))
        with mock.patch.object(time, "time", lambda: next(clock)):
            body = self.body()                    # one body: the one sent is the one signed
            status = self.peer_send(body, auth=self.signed(body, sender="sam"))[0]
        self.assertEqual(status, 503)
        self.assertIn("local cousin", " ".join(self.logged))

    def test_a_peer_named_like_the_operator_is_refused(self):
        self.peers(reach=["wren"], name="Priya")
        self.serve()
        self.assertEqual(self.peer_send(self.body())[0], 403)
        self.assertEqual(_messages(self.home), [])

    def test_the_network_guard_applies(self):
        self.serve(guard=lambda address: False)
        self.assertEqual(self.peer_send(self.body())[0], 403)

    def test_a_body_over_64_kib_is_refused_before_it_is_read(self):
        self.serve()
        status, _ = self.peer_send(None, auth=None, raw=b"x" * (peer_routes.MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)

    def test_credentials_do_not_cross_worlds(self):
        from cousin_lib.console import auth
        auth.Users(self.root / "config" / "console-users.json").set_password(
            "ana", "correct horse")
        self.serve()
        # a peer signature under /api/: the console wants its own session
        req = urllib.request.Request("http://127.0.0.1:%d/api/cousins" % self.server.port,
                                     headers={"Authorization": self.signed(self.body())})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
        self.assertEqual(status, 401)
        # a hive token (a bearer) under /peer/: no entry
        self.assertEqual(self.peer_send(self.body(), auth="Bearer hive_x")[0], 401)
        # a console session under /peer/: no entry
        self.assertEqual(self.post("/api/auth/login", {"user": "ana",
                                                       "password": "correct horse"})[0], 200)
        self.assertEqual(self.get("/api/cousins")[0], 200)
        self.assertEqual(self.post("/peer/send", self.body())[0], 401)
        self.assertEqual(_messages(self.home), [])


class TestTheSendingSide(PeerCase):
    def _send(self, extra):
        with open(self.root / "config" / "external-peers.toml", "a") as fh:
            fh.write(extra)
        seen = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true, "id": 3}'

        class _Opener:
            def open(self, req, timeout=None):
                seen.append((req.full_url, req.get_header("Authorization"), req.data))
                return _Resp()
        fw = FrameworkConfig(self.root)
        sender = CousinConfig.load(self.root / "cousins" / "sam")
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)}), \
                mock.patch("urllib.request.build_opener", return_value=_Opener()):
            chat.send_message(fw, sender, "kestrel", "hello", guard=lambda host: True)
        return seen

    def test_a_signed_send_never_carries_the_secret(self):
        [(url, auth, data)] = self._send('token_file = ".secrets/peers/kestrel"\nsender = "testbed"\n')
        payload = json.loads(data)
        self.assertEqual(url, "http://192.0.2.20:8600/peer/send")      # the default with a token
        self.assertNotIn(SECRET, auth + data.decode())
        self.assertEqual(auth, "HMAC testbed:%s" % chat.peer_signature(
            SECRET, "testbed", "kestrel", payload["sent_at"], payload["msg_id"], "hello"))
        self.assertEqual((payload["to"], payload["message"]), ("kestrel", "hello"))

    def test_a_token_file_needs_a_sender(self):
        from cousin_lib.config import MissingConfigError
        with self.assertRaisesRegex(MissingConfigError, "sender"):
            self._send('token_file = ".secrets/peers/kestrel"\n')
