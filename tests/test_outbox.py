"""The outbox (#245): a message to a signed external peer that could not
be confirmed is kept and sent again under the SAME msg_id until the peer
takes it (2xx, or 409: it had landed), answers for good (another 4xx),
or the 14-minute window closes. The sending cousin hears how it ended."""
import email.message
import io
import json
import os
import pathlib
import socket
import urllib.error
from unittest import mock

from cousin_lib import chat, outbox
from cousin_lib.config import CousinConfig, FrameworkConfig
from tests._hermetic import HermeticCase

SECRET = "peer-secret-4f9a1c0b27de81d2e4f09a3c5c7e9b1d"


def http_error(code):
    return urllib.error.HTTPError("http://192.0.2.20:8600/peer/send", code, "x",
                                  email.message.Message(), io.BytesIO(b"{}"))


class OutboxCase(HermeticCase):
    def setUp(self):
        super().setUp()
        from tests.runner._home import temp_home
        self.home = temp_home(self, runner="fake")
        self.root = self.home.parent.parent
        (self.root / "config").mkdir(exist_ok=True)
        secrets = self.root / ".secrets" / "peers"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / "kestrel").write_text(SECRET + "\n")
        os.chmod(secrets / "kestrel", 0o600)
        self.peers()
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root),
                                         "COUSIN_HOME": str(self.home)})
        p.start()
        self.addCleanup(p.stop)

    def peers(self, signed=True):
        text = '[peers.kestrel]\nurl = "http://192.0.2.20:8600"\nreach = []\n'
        if signed:
            text += 'token_file = ".secrets/peers/kestrel"\nsender = "testbed"\n'
        (self.root / "config" / "external-peers.toml").write_text(text)

    def send(self, effect):
        """send_message to kestrel with post_signed's attempt replaced by
        `effect` (an exception to raise, or a body to return)."""
        seen = []

        def attempt(root, peer, message, msg_id, guard=None):
            seen.append(msg_id)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        fw = FrameworkConfig(self.root)
        sender = CousinConfig.load(self.home)
        with mock.patch.object(chat, "post_signed", attempt):
            out = chat.send_message(fw, sender, "kestrel", "the greenhouse report",
                                    guard=lambda host: True)
        return out, seen

    def _raw_inbox(self):
        import sqlite3
        if not (self.home / "data" / "inbox.db").exists():
            return []
        db = sqlite3.connect(self.home / "data" / "inbox.db")
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute("SELECT * FROM inbox WHERE source = 'outbox'")]
        finally:
            db.close()


class TestClassify(OutboxCase):
    def test_the_classes(self):
        self.assertEqual(outbox.classify(http_error(409)), outbox.DELIVERED_NOW)
        for code in (429, 500, 502, 503, 504):
            self.assertEqual(outbox.classify(http_error(code)), outbox.QUEUED_FOR_RETRY, code)
        for code in (400, 401, 403, 404, 413):
            self.assertEqual(outbox.classify(http_error(code)), outbox.PERMANENT, code)
        self.assertEqual(outbox.classify(urllib.error.URLError("refused")), outbox.QUEUED_FOR_RETRY)
        self.assertEqual(outbox.classify(socket.timeout("timed out")), outbox.QUEUED_FOR_RETRY)
        self.assertEqual(outbox.classify(ValueError("x")), outbox.PERMANENT)


class TestFirstAttempt(OutboxCase):
    def test_a_confirmed_send_keeps_nothing(self):
        out, _ = self.send({"ok": True, "id": 3})
        self.assertEqual(out["id"], 3)
        self.assertEqual(outbox.list_rows(self.root), [])

    def test_a_transient_failure_is_queued_under_its_msg_id(self):
        out, seen = self.send(http_error(504))
        self.assertTrue(out["queued"])
        self.assertEqual(out["msg_id"], seen[0])
        self.assertIn("14 minutes", out["note"])
        [row] = outbox.list_rows(self.root)
        self.assertEqual((row["msg_id"], row["state"], row["sender"], row["dest"], row["attempts"]),
                         (seen[0], "pending", self.home.name, "kestrel", 1))

    def test_a_409_on_the_first_attempt_is_delivered(self):
        out, _ = self.send(http_error(409))
        self.assertTrue(out["ok"])
        self.assertNotIn("queued", out)
        self.assertEqual(outbox.list_rows(self.root), [])

    def test_a_permanent_failure_raises_and_keeps_nothing(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.send(http_error(401))
        self.assertEqual(outbox.list_rows(self.root), [])

    def test_a_legacy_peer_is_never_queued(self):
        self.peers(signed=False)
        with mock.patch.object(chat, "_post", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(urllib.error.URLError):
                chat.send_message(FrameworkConfig(self.root), CousinConfig.load(self.home),
                                  "kestrel", "hi", guard=lambda host: True)
        self.assertEqual(outbox.list_rows(self.root), [])

    def test_post_signed_signs_the_given_id_with_a_fresh_time(self):
        posted = []
        with mock.patch.object(chat, "_post", lambda peer, payload, headers:
                               posted.append((payload, headers)) or {}):
            peer = chat.load_external_peers(self.root)["kestrel"]
            chat.post_signed(self.root, peer, "hi", "same-id-0001", guard=lambda host: True)
            chat.post_signed(self.root, peer, "hi", "same-id-0001", guard=lambda host: True)
        self.assertEqual([p["msg_id"] for p, _ in posted], ["same-id-0001", "same-id-0001"])
        for payload, headers in posted:
            self.assertEqual(headers["Authorization"], "HMAC testbed:%s" % chat.peer_signature(
                SECRET, "testbed", "kestrel", payload["sent_at"], "same-id-0001", "hi"))


class TestDrain(OutboxCase):
    def queued(self):
        _, seen = self.send(http_error(503))
        return seen[0]

    def drain(self, effect, at):
        sent = []

        def send(peer, message, msg_id):
            sent.append(msg_id)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        row = outbox.list_rows(self.root)[0]
        return outbox.drain(self.root, send=send, now=row["created"] + at), sent

    def test_nothing_is_sent_before_it_is_due(self):
        self.queued()
        out, sent = self.drain({"ok": True}, at=1)
        self.assertEqual((out["delivered"], sent), ([], []))

    def test_a_retry_reuses_the_msg_id_and_delivers(self):
        msg_id = self.queued()
        out, sent = self.drain({"ok": True}, at=20)
        self.assertEqual(sent, [msg_id])
        self.assertEqual(out["delivered"], [msg_id])
        [row] = outbox.list_rows(self.root)
        self.assertEqual((row["state"], row["attempts"]), ("delivered", 2))
        [item] = self._raw_inbox()
        self.assertIn("was delivered on attempt 2", item["body"])

    def test_a_409_on_a_retry_means_it_had_landed(self):
        msg_id = self.queued()
        out, _ = self.drain(http_error(409), at=20)
        self.assertEqual(out["delivered"], [msg_id])

    def test_a_transient_retry_waits_longer(self):
        msg_id = self.queued()
        out, _ = self.drain(urllib.error.URLError("refused"), at=20)
        self.assertEqual(out["retrying"], [msg_id])
        [row] = outbox.list_rows(self.root)
        self.assertEqual(row["attempts"], 2)
        self.assertAlmostEqual(row["next_at"] - (row["created"] + 20), outbox.BACKOFF_S[1], delta=1)
        self.assertEqual(self._raw_inbox(), [])

    def test_a_permanent_answer_gives_up_and_tells_the_sender(self):
        msg_id = self.queued()
        out, _ = self.drain(http_error(403), at=20)
        self.assertEqual(out["gave_up"], [msg_id])
        [item] = self._raw_inbox()
        self.assertIn("was NOT delivered", item["body"])
        self.assertIn("HTTP 403", item["body"])
        self.assertEqual(item["thread_id"], "system")

    def test_past_the_deadline_it_gives_up_without_sending(self):
        msg_id = self.queued()
        out, sent = self.drain({"ok": True}, at=outbox.DEADLINE_S + 1)
        self.assertEqual((out["gave_up"], sent), ([msg_id], []))

    def test_the_deadline_stays_inside_the_gates_memory_of_an_id(self):
        from cousin_lib import peer_inbound
        self.assertLess(outbox.DEADLINE_S, peer_inbound.SEEN_KEEP_S)

    def test_a_peer_removed_from_the_config_gives_up(self):
        msg_id = self.queued()
        (self.root / "config" / "external-peers.toml").write_text("")
        out, _ = self.drain({"ok": True}, at=20)
        self.assertEqual(out["gave_up"], [msg_id])

    def test_an_empty_install_drains_nothing(self):
        self.assertEqual(outbox.drain(self.root),
                         {"delivered": [], "gave_up": [], "retrying": []})


class TestSurfaces(OutboxCase):
    def _chat(self, argv):
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.chat_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_cousin_chat_send_says_queued(self):
        with mock.patch.object(chat, "post_signed", side_effect=http_error(502)), \
                mock.patch("cousin_lib.server.netguard.NetGuard.from_config",
                           return_value=lambda host: True):
            rc, out, _ = self._chat(["send", "kestrel", "hi"])
        self.assertEqual(rc, 0)
        body = json.loads(out)
        self.assertTrue(body["queued"])
        self.assertIn("not confirmed yet", body["note"])

    def test_cousin_chat_outbox_lists_rows(self):
        _, seen = self.send(http_error(502))
        rc, out, _ = self._chat(["outbox"])
        self.assertEqual(rc, 0)
        self.assertIn(seen[0][:8], out)
        self.assertIn("pending", out)

    def test_the_console_route_lists_rows(self):
        from cousin_lib import outbox as ob
        _, seen = self.send(http_error(502))
        self.assertEqual([r["msg_id"] for r in ob.list_rows(self.root, state="pending")], seen)
        self.assertEqual(ob.list_rows(self.root, state="delivered"), [])


class TestReviewFixes(OutboxCase):
    """#245 review: a fresh clock per row, a pass budget, one failure per
    peer per pass, the window counted from the first attempt's start, a
    broken-off answer retried, a refused cousin final at the gate."""

    def _rows(self, n):
        ids = []
        for _ in range(n):
            _, seen = self.send(http_error(503))
            ids.append(seen[0])
        return ids

    def test_a_slow_send_does_not_let_the_next_row_out_past_the_deadline(self):
        ids = self._rows(2)
        created = max(r["created"] for r in outbox.list_rows(self.root))
        ticks = [created + 800, created + 800, created + 845]

        def clock():
            return ticks.pop(0) if ticks else created + 846
        sent = []

        def slow(peer, message, msg_id):
            sent.append(msg_id)             # delivered, but it took 45 s
        with mock.patch("cousin_lib.outbox.time.time", clock):
            out = outbox.drain(self.root, send=slow, budget_s=1000)
        # the second row is past created+840 by the time its turn comes
        self.assertEqual(sent, [ids[0]])
        self.assertIn(ids[1], out["gave_up"])

    def test_a_pass_stops_at_its_budget(self):
        ids = self._rows(3)
        created = outbox.list_rows(self.root)[-1]["created"]
        out = outbox.drain(self.root, send=lambda *a: None, now=created + 20, budget_s=-1)
        self.assertEqual(out, {"delivered": [], "gave_up": [], "retrying": []})
        self.assertEqual(len(outbox.list_rows(self.root, state="pending")), 3)

    def test_one_failure_to_a_peer_skips_its_other_rows_this_pass(self):
        ids = self._rows(3)
        created = outbox.list_rows(self.root)[-1]["created"]
        sent = []

        def down(peer, message, msg_id):
            sent.append(msg_id)
            raise urllib.error.URLError("refused")
        out = outbox.drain(self.root, send=down, now=created + 20)
        self.assertEqual(len(sent), 1)
        self.assertEqual(out["retrying"], sent)

    def test_the_window_starts_when_the_first_attempt_started(self):
        with mock.patch("time.time", side_effect=[1000.0] + [1012.0] * 50):
            self.send(http_error(504))
        [row] = outbox.list_rows(self.root)
        self.assertEqual(row["created"], 1000.0)

    def test_a_broken_off_answer_is_retried(self):
        import http.client
        self.assertEqual(outbox.classify(http.client.IncompleteRead(b"")), outbox.QUEUED_FOR_RETRY)

    def test_nobody_is_told_twice(self):
        self._rows(1)
        row = outbox.list_rows(self.root)[0]
        conn = outbox._db(self.root)
        try:
            self.assertEqual(outbox._finish(conn, row, outbox.DELIVERED, None, row["created"]), 1)
            self.assertEqual(outbox._finish(conn, row, outbox.GAVE_UP, "x", row["created"]), 0)
        finally:
            conn.close()

    def test_a_409_on_a_retry_says_the_peer_already_had_it(self):
        self._rows(1)
        row = outbox.list_rows(self.root)[0]
        outbox.drain(self.root, send=lambda *a: (_ for _ in ()).throw(http_error(409)),
                     now=row["created"] + 20)
        [item] = self._raw_inbox()
        self.assertIn("already had this message id", item["body"])

    def test_the_gate_refuses_a_cousin_with_no_runner_for_good(self):
        import time as _t
        from cousin_lib import peer_inbound
        from cousin_lib.chat import DeliveryRefused
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "Wren"\npeer_visible = true\n[agent]\nrunner = "fake"\n'
            % self.home.name)
        with mock.patch("cousin_lib.chat.deliver_to", side_effect=DeliveryRefused("no runner")):
            with self.assertRaises(peer_inbound.Refused) as caught:
                peer_inbound.accept(self.root, identity="kestrel", display="Kestrel",
                                    to=self.home.name, message="hi", msg_id="kestrel-0000000009",
                                    sent_at=_t.time(), allowed=lambda s: True)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(outbox.classify(http_error(404)), outbox.PERMANENT)


class TestTheTickDoesNotPinTheClock(OutboxCase):
    """#245 re-review: loops.tick drains with the outbox's own clock."""

    def test_drain_gets_no_now(self):
        from cousin_lib import loops
        seen = []
        with mock.patch("cousin_lib.outbox.drain", lambda root, **kw: seen.append(kw) or {}):
            loops.tick(deliver=lambda *a, **k: None, is_alive=lambda slug: False, now=12345.0)
        self.assertEqual(seen, [{}])
