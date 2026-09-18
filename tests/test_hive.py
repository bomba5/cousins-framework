"""The hive: an authed cross-machine message bus, off by default.

Perimeter tests lead: identity comes from the token not the body (a
node cannot claim to be another), scope gates the shared corpus, and
the client fails toward local when there is no queen. The queen is a
real server on a loopback port; the node reaches it outbound only.
"""
import json
import os
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from cousin_lib.hive import (
    HiveError,
    HiveStore,
    build_queen,
    hive_recall,
    hive_send,
)


class HiveCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _store(self):
        return HiveStore(self.root / "hive")

    def _mint(self, store, slug, scope=("own", "shared")):
        return store.mint_token(slug, scope=list(scope))

    def _queen(self, store):
        server = build_queen(store)
        server.start()
        self.addCleanup(server.stop)
        return "http://127.0.0.1:%d" % server.port

    def _call(self, url, path, token=None, method="GET", body=None):
        req = urllib.request.Request(
            url + path,
            data=json.dumps(body).encode() if body else None,
            method=method)
        if token:
            req.add_header("Authorization", "Bearer %s" % token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())


class TestTokenIdentity(HiveCase):
    def test_sender_is_the_token_not_the_body(self):
        store = self._store()
        alice = self._mint(store, "alice")
        self._mint(store, "bob")
        url = self._queen(store)
        # Alice's token, but the body claims to be from bob. The stored
        # sender must be alice.
        status, _ = self._call(
            url, "/hive/msg", token=alice, method="POST",
            body={"to": "bob", "from": "bob", "id": "m1",
                  "body": "spoofed"})
        self.assertEqual(status, 200)
        inbox = store.read_inbox("bob", since=0)
        self.assertEqual(inbox[0]["from"], "alice")

    def test_missing_token_is_401(self):
        store = self._store()
        self._mint(store, "bob")
        url = self._queen(store)
        status, _ = self._call(url, "/hive/inbox?since=0")
        self.assertEqual(status, 401)

    def test_health_is_the_only_unauthenticated_route(self):
        store = self._store()
        url = self._queen(store)
        status, body = self._call(url, "/hive/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")


class TestInboxOutboundOnly(HiveCase):
    def test_a_node_reads_only_its_own_inbox(self):
        store = self._store()
        alice = self._mint(store, "alice")
        bob = self._mint(store, "bob")
        url = self._queen(store)
        self._call(url, "/hive/msg", token=alice, method="POST",
                   body={"to": "bob", "id": "m1", "body": "hi bob"})
        # bob's token reads bob's inbox; the slug comes from the token.
        status, body = self._call(url, "/hive/inbox?since=0", token=bob)
        self.assertEqual([m["body"] for m in body["messages"]],
                         ["hi bob"])

    def test_send_is_idempotent_on_message_id(self):
        store = self._store()
        alice = self._mint(store, "alice")
        self._mint(store, "bob")
        url = self._queen(store)
        for _ in range(2):
            self._call(url, "/hive/msg", token=alice, method="POST",
                       body={"to": "bob", "id": "same", "body": "x"})
        self.assertEqual(len(store.read_inbox("bob", since=0)), 1)


class TestScopeGate(HiveCase):
    def test_shared_recall_needs_shared_scope(self):
        store = self._store()
        own_only = self._mint(store, "alice", scope=("own",))
        store.append_memory("bob", "the shared fact", scope="shared")
        url = self._queen(store)
        status, body = self._call(
            url, "/hive/recall?q=shared", token=own_only)
        # own-only scope sees no shared corpus.
        self.assertEqual(body["memories"], [])


class TestOwnMemoriesStayWithTheirNode(HiveCase):
    """'own' scope means the caller's own slug, never every node's own
    memories. Canary: recall filtered on scope only, so any token
    holding 'own' read every node's private memories."""

    def _recall(self, url, token, q):
        status, body = self._call(url, "/hive/recall?q=" + q, token=token)
        self.assertEqual(status, 200)
        return body["memories"]

    def test_a_node_cannot_read_another_nodes_own_memories(self):
        store = self._store()
        wren = self._mint(store, "wren")
        testa = self._mint(store, "testa")
        store.append_memory("testa", "testa secret: the lockbox code",
                            scope="own")
        url = self._queen(store)
        self.assertEqual(self._recall(url, wren, "secret"), [])
        self.assertEqual(self._recall(url, testa, "secret"),
                         ["testa secret: the lockbox code"])

    def test_own_and_shared_still_read_through_the_queen(self):
        store = self._store()
        wren = self._mint(store, "wren")
        self._mint(store, "testa")
        url = self._queen(store)
        status, _ = self._call(url, "/hive/memory", token=wren,
                               method="POST",
                               body={"text": "wren note: kettle",
                                     "scope": "own"})
        self.assertEqual(status, 200)
        store.append_memory("testa", "fleet note: kettle", scope="shared")
        store.append_memory("testa", "testa note: kettle", scope="own")
        self.assertEqual(sorted(self._recall(url, wren, "kettle")),
                         ["fleet note: kettle", "wren note: kettle"])

    def test_own_scope_is_still_required_for_own_memories(self):
        store = self._store()
        shared_only = self._mint(store, "wren", scope=("shared",))
        store.append_memory("wren", "wren note", scope="own")
        url = self._queen(store)
        self.assertEqual(self._recall(url, shared_only, "note"), [])

    def test_recall_requires_the_callers_slug(self):
        store = self._store()
        with self.assertRaises(TypeError):
            store.recall("x", scopes={"own"})

    def test_a_write_outside_the_tokens_scope_is_refused(self):
        store = self._store()
        own_only = self._mint(store, "wren", scope=("own",))
        url = self._queen(store)
        for scope in ("shared", "kestrel"):
            status, _ = self._call(url, "/hive/memory", token=own_only,
                                   method="POST",
                                   body={"text": "planted", "scope": scope})
            self.assertEqual(status, 403, scope)
        rows = store.conn.execute("SELECT COUNT(*) FROM memory").fetchone()
        self.assertEqual(rows[0], 0)


class TestClientFailsLocal(HiveCase):
    def test_send_with_no_queen_raises_hive_error_for_local_fallback(self):
        # A cousin with no reachable queen behaves single-machine: the
        # client raises HiveError so the caller falls back to local,
        # never crashes.
        with self.assertRaises(HiveError):
            hive_send(queen_url="http://127.0.0.1:9", token="x",
                      to="bob", body="hi", msg_id="m1")

    def test_recall_with_no_queen_raises_hive_error(self):
        with self.assertRaises(HiveError):
            hive_recall(queen_url="http://127.0.0.1:9", token="x",
                        query="anything")


class TestCli(HiveCase):
    def _main(self, argv):
        import contextlib
        import io

        from cousin_lib.hive import hive_main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = hive_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_mint_prints_a_token(self):
        rc, out, _ = self._main(["mint", "alice"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().startswith("hive_"))

    def test_send_with_no_queen_exits_one_local_fallback(self):
        rc, _, err = self._main(
            ["send", "--queen", "http://127.0.0.1:9",
             "--token", "x", "--to", "bob", "--id", "m1", "hi"])
        self.assertEqual(rc, 1)
        self.assertIn("local", err)


if __name__ == "__main__":
    unittest.main()
