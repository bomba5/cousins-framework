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
        store = HiveStore(self.root / "hive")
        self.addCleanup(store.close)
        return store

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


class _FakeEmbed:
    """A deterministic embedder: a vector of keyword hits, so meaning
    is 'shares a topic word' and a test can reason about scores."""
    TOPICS = ("kettle", "garden", "boat")

    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, text, config):
        self.calls.append(text)
        if self.fail:
            raise OSError("embedding service down")
        lowered = text.lower()
        return [1.0 if t in lowered else 0.0 for t in self.TOPICS] + [0.1]


def _embedder(fake, **config):
    from cousin_lib.hive import Embedder
    cfg = {"url": "http://embed.example.invalid", "model": "m1",
           "recall": {"min_score": 0.45}}
    cfg.update(config)
    return Embedder(cfg, embed_fn=fake)


class TestMigration(HiveCase):
    def test_a_database_from_the_first_release_opens_and_works(self):
        import sqlite3
        path = self.root / "hive"
        path.mkdir()
        conn = sqlite3.connect(path / "hive.db")
        conn.executescript(
            "CREATE TABLE tokens (token TEXT PRIMARY KEY, slug TEXT NOT NULL,"
            " scope TEXT NOT NULL);"
            "CREATE TABLE inbox (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " recipient TEXT NOT NULL, sender TEXT NOT NULL,"
            " msg_id TEXT NOT NULL, body TEXT NOT NULL, ts REAL,"
            " UNIQUE(recipient, msg_id));"
            "CREATE TABLE memory (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " slug TEXT NOT NULL, text TEXT NOT NULL, scope TEXT NOT NULL,"
            " ts REAL);")
        conn.execute("INSERT INTO tokens VALUES ('hive_old', 'wren',"
                     " '[\"own\", \"shared\"]')")
        conn.execute("INSERT INTO memory (slug, text, scope, ts) VALUES"
                     " ('wren', 'old kettle note', 'own', 1.0)")
        conn.commit()
        conn.close()
        store = HiveStore(path)
        self.addCleanup(store.close)
        self.assertEqual(store.resolve("hive_old")["slug"], "wren")
        self.assertEqual(store.recall("kettle", scopes={"own"}, slug="wren"),
                         ["old kettle note"])
        self.assertEqual(store.mint_token("wren"), "hive_old")
        columns = {r[1] for r in store.conn.execute(
            "PRAGMA table_info(memory)")}
        self.assertTrue({"kind", "vec", "vec_model", "origin"} <= columns)
        # Reopening an already migrated file is a no-op.
        HiveStore(path).close()


class TestRevocation(HiveCase):
    def test_revoked_is_401_everywhere_and_a_rebuild_gets_a_new_token(self):
        store = self._store()
        token = self._mint(store, "wren")
        url = self._queen(store)
        self.assertEqual(store.revoke("wren"), 1)
        self.assertIsNone(store.resolve(token))
        for method, path, body in (
                ("GET", "/hive/inbox?since=0", None),
                ("GET", "/hive/recall?q=x", None),
                ("POST", "/hive/memory", {"text": "x"}),
                ("POST", "/hive/msg", {"to": "a", "id": "1", "body": "x"}),
                ("POST", "/hive/checkin", {"port": 1, "name": "W"})):
            self.assertEqual(self._call(url, path, token=token, method=method,
                                        body=body)[0], 401, path)
        fresh = self._mint(store, "wren")
        self.assertNotEqual(fresh, token)
        self.assertEqual(store.resolve(fresh)["slug"], "wren")

    def test_forget_refuses_a_live_token(self):
        store = self._store()
        self._mint(store, "wren")
        with self.assertRaises(ValueError):
            store.forget("wren")
        store.revoke("wren")
        self.assertEqual(store.forget("wren"), {"tokens": 1, "nodes": 0})
        self.assertEqual(store.nodes(), [])


class TestCheckin(HiveCase):
    def test_checkin_records_the_peer_address_and_the_body(self):
        store = self._store()
        token = self._mint(store, "wren")
        url = self._queen(store)
        status, body = self._call(url, "/hive/checkin", token=token,
                                  method="POST",
                                  body={"port": 8210, "name": "Wren",
                                        "role": "garden", "version": "0.2.0",
                                        "slug": "kestrel"})
        self.assertEqual((status, body), (200, {"ok": True,
                                                "checkin_seconds": 60}))
        node = store.node("wren")
        self.assertEqual((node["host"], node["port"], node["name"],
                          node["role"], node["version"]),
                         ("127.0.0.1", 8210, "Wren", "garden", "0.2.0"))
        # Identity is the token's: the body's slug is ignored.
        self.assertIsNone(store.node("kestrel"))

    def test_bad_checkins_are_400(self):
        store = self._store()
        token = self._mint(store, "wren")
        url = self._queen(store)
        for body in ({"name": "W"}, {"port": "8210", "name": "W"},
                     {"port": 0, "name": "W"}, {"port": 8210},
                     {"port": 8210, "name": "W", "version": 2}):
            self.assertEqual(self._call(url, "/hive/checkin", token=token,
                                        method="POST", body=body)[0], 400,
                             body)

    def test_nodes_lists_built_and_checked_in(self):
        store = self._store()
        self._mint(store, "kestrel")
        store.mint_token("wren", scope=["own"], name="Wren", role="garden")
        store.checkin("kestrel", name="Kestrel", role="", host="198.51.100.7",
                      port=8210, now=100.0)
        rows = {r["slug"]: r for r in store.nodes()}
        self.assertTrue(rows["kestrel"]["checked_in"])
        self.assertEqual(rows["kestrel"]["last_seen"], 100.0)
        self.assertFalse(rows["wren"]["checked_in"])
        self.assertEqual((rows["wren"]["name"], rows["wren"]["role"]),
                         ("Wren", "garden"))
        for row in rows.values():
            self.assertNotIn("token", row)

    def test_online_is_two_and_a_half_periods(self):
        from cousin_lib.hive import is_online
        self.assertTrue(is_online(1000, 60, now=1150))
        self.assertFalse(is_online(1000, 60, now=1151))
        self.assertFalse(is_online(None, 60, now=1000))


class TestRecallContract(HiveCase):
    def test_recall_answers_memories_and_scored_results(self):
        store = self._store()
        token = self._mint(store, "wren")
        url = self._queen(store)
        for text in ("kettle one", "kettle two", "kettle three",
                     "kettle four"):
            self._call(url, "/hive/memory", token=token, method="POST",
                       body={"text": text, "kind": "fact"})
        status, body = self._call(url, "/hive/recall?q=kettle", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["memories"],
                         ["kettle four", "kettle three", "kettle two"])
        self.assertEqual(set(body["results"][0]), {"text", "score", "slug",
                                                   "ts"})
        self.assertEqual(body["results"][0]["score"], 1.0)
        self.assertEqual(body["results"][0]["slug"], "wren")
        _, body = self._call(url, "/hive/recall?q=kettle&k=10", token=token)
        self.assertEqual(len(body["memories"]), 4)
        self.assertEqual(self._call(url, "/hive/recall?q=x&k=many",
                                    token=token)[0], 400)

    def test_memory_needs_text_and_keeps_kind(self):
        store = self._store()
        token = self._mint(store, "wren")
        url = self._queen(store)
        self.assertEqual(self._call(url, "/hive/memory", token=token,
                                    method="POST", body={"text": " "})[0], 400)
        status, body = self._call(url, "/hive/memory", token=token,
                                  method="POST",
                                  body={"text": "a", "kind": "decision"})
        self.assertEqual(status, 200)
        row = store.conn.execute("SELECT kind FROM memory WHERE id=?",
                                 (body["id"],)).fetchone()
        self.assertEqual(row[0], "decision")

    def test_msg_needs_to_id_and_body(self):
        store = self._store()
        token = self._mint(store, "wren")
        url = self._queen(store)
        for body in ({"id": "1", "body": "x"}, {"to": "a", "body": "x"},
                     {"to": "a", "id": "1"}):
            self.assertEqual(self._call(url, "/hive/msg", token=token,
                                        method="POST", body=body)[0], 400)


class TestSemanticRecall(HiveCase):
    def test_scores_rank_and_threshold(self):
        store = self._store()
        fake = _FakeEmbed()
        store.append_memory("wren", "the kettle whistles", scope="own")
        store.append_memory("wren", "tomatoes in the garden", scope="own")
        store.append_memory("wren", "a kettle in the garden", scope="own")
        hits = store.recall_scored("kettle", scopes={"own"}, slug="wren",
                                   k=3, min_score=0.45,
                                   embedder=_embedder(fake))
        self.assertEqual([h["text"] for h in hits],
                         ["the kettle whistles", "a kettle in the garden"])
        self.assertGreater(hits[0]["score"], hits[1]["score"])
        self.assertLess(hits[1]["score"], 1.0)

    def test_vectors_are_cached_per_model(self):
        store = self._store()
        fake = _FakeEmbed()
        store.append_memory("wren", "the kettle whistles", scope="own")
        store.recall_scored("kettle", scopes={"own"}, slug="wren",
                            embedder=_embedder(fake))
        self.assertEqual(len(fake.calls), 2)  # the query and the row
        store.recall_scored("kettle", scopes={"own"}, slug="wren",
                            embedder=_embedder(fake))
        self.assertEqual(len(fake.calls), 3)  # the query only
        store.recall_scored("kettle", scopes={"own"}, slug="wren",
                            embedder=_embedder(fake, model="m2"))
        self.assertEqual(len(fake.calls), 5)  # another model re-embeds

    def test_semantic_keeps_the_scope_boundary(self):
        store = self._store()
        fake = _FakeEmbed()
        store.append_memory("testa", "testa kettle secret", scope="own")
        store.append_memory("testa", "fleet kettle fact", scope="shared")
        hits = store.recall_scored("kettle", scopes={"own", "shared"},
                                   slug="wren", embedder=_embedder(fake))
        self.assertEqual([h["text"] for h in hits], ["fleet kettle fact"])
        self.assertNotIn("testa kettle secret", fake.calls)

    def test_a_failing_embedder_falls_back_to_substring(self):
        store = self._store()
        fake = _FakeEmbed()
        fake.fail = True
        store.append_memory("wren", "the kettle whistles", scope="own")
        hits = store.recall_scored("kettle", scopes={"own"}, slug="wren",
                                   embedder=_embedder(fake))
        self.assertEqual(hits[0]["score"], 1.0)

    def test_the_queen_route_uses_the_embedder_and_the_default_threshold(self):
        store = self._store()
        token = self._mint(store, "wren")
        fake = _FakeEmbed()
        embedder = _embedder(fake, recall={"min_score": 0.9})
        server = build_queen(store, embedder=lambda: embedder)
        server.context.embed_in_background = False
        server.start()
        self.addCleanup(server.stop)
        url = "http://127.0.0.1:%d" % server.port
        for text in ("the kettle whistles", "a kettle in the garden"):
            self._call(url, "/hive/memory", token=token, method="POST",
                       body={"text": text})
        # Embedded on append: two rows, two vectors cached.
        self.assertEqual(store.conn.execute(
            "SELECT COUNT(*) FROM memory WHERE vec IS NOT NULL").fetchone()[0],
            2)
        _, body = self._call(url, "/hive/recall?q=kettle", token=token)
        self.assertEqual(body["memories"], ["the kettle whistles"])
        _, body = self._call(url, "/hive/recall?q=kettle&min_score=0.1",
                             token=token)
        self.assertEqual(len(body["memories"]), 2)


class TestInboxWait(HiveCase):
    def test_a_waiting_poll_returns_when_a_message_lands(self):
        import threading
        import time
        store = self._store()
        bob = self._mint(store, "bob")
        alice = self._mint(store, "alice")
        url = self._queen(store)
        out = {}

        def poll():
            started = time.time()
            out["body"] = self._call(url, "/hive/inbox?since=0&wait=20",
                                     token=bob)[1]
            out["took"] = time.time() - started

        thread = threading.Thread(target=poll)
        thread.start()
        time.sleep(0.3)
        self._call(url, "/hive/msg", token=alice, method="POST",
                   body={"to": "bob", "id": "m1", "body": "wake"})
        thread.join(10)
        self.assertEqual(out["body"]["messages"][0]["body"], "wake")
        self.assertLess(out["took"], 5)

    def test_wait_is_capped(self):
        from cousin_lib import hive
        store = self._store()
        token = self._mint(store, "bob")
        seen = {}
        store.wait_inbox = lambda slug, since, wait: seen.update(
            wait=wait) or []
        ctx = hive.QueenContext(store)
        status, _ = hive.handle_request(
            ctx, "GET", "/hive/inbox", "since=0&wait=500",
            {"Authorization": "Bearer " + token}, b"", "127.0.0.1")
        self.assertEqual((status, seen["wait"]), (200, 30.0))


class TestHiveConfig(HiveCase):
    def _write(self, text):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "hive.toml").write_text(text)

    def test_absent_and_disabled_are_off(self):
        from cousin_lib.hive import hive_config
        self.assertIsNone(hive_config(self.root))
        self._write('enabled = false\npublic_url = "http://x.invalid:1"\n')
        self.assertIsNone(hive_config(self.root))

    def test_enabled_needs_a_public_url_and_fills_defaults(self):
        from cousin_lib.hive import HiveConfigError, hive_config
        self._write("enabled = true\n")
        with self.assertRaises(HiveConfigError):
            hive_config(self.root)
        self._write('enabled = true\npublic_url = "http://q.invalid:8600/"\n')
        self.assertEqual(hive_config(self.root), {
            "enabled": True, "public_url": "http://q.invalid:8600",
            "checkin_seconds": 60, "home_cousin": ""})

    def test_home_chat_url_is_not_read(self):
        # the legacy unauthenticated home chat server is gone; a
        # node reaches its home cousin through the queen (home_cousin)
        from cousin_lib.hive import hive_config
        self._write('enabled = true\npublic_url = "http://q.invalid:8600"\n'
                    'home_chat_url = "http://h.invalid:8090"\nhome_cousin = "wren"\n')
        cfg = hive_config(self.root)
        self.assertNotIn("home_chat_url", cfg)
        self.assertEqual(cfg["home_cousin"], "wren")
        self._write('enabled = true\npublic_url = "http://q.invalid:8600"\n'
                    'home_chat_url = 7\n')
        self.assertNotIn("home_chat_url", hive_config(self.root))

    def test_unusable_values_are_loud(self):
        from cousin_lib.hive import HiveConfigError, hive_config
        for text in ('enabled = "yes"\n', "enabled = [\n",
                     'enabled = true\npublic_url = "http://q.invalid"\n'
                     'checkin_seconds = 2\n'):
            self._write(text)
            with self.assertRaises(HiveConfigError, msg=text):
                hive_config(self.root)


class TestImportLegacy(HiveCase):
    def _fixtures(self):
        legacy = self.root / "legacy"
        (legacy / "kestrel").mkdir(parents=True)
        (legacy / "wren").mkdir()
        (legacy / "tokens.json").write_text(json.dumps({
            "hive_legacyKestrelToken0000000000000": {
                "slug": "kestrel", "scope": ["own", "shared"]},
            "hive_legacyWrenToken00000000000000000": {"slug": "wren"},
            "hive_legacyOwlToken000000000000000000": {
                "slug": "owl", "scope": ["own", "roster"]},
            "hive_legacyBadToken000000000000000000": {"slug": "Bad Slug"},
        }))
        rows = [{"text": "kestrel fact one", "kind": "fact", "seq": 1,
                 "ts": 1700000000.5, "embedding": [0.1, 0.2]},
                {"text": "kestrel decided a thing", "kind": "decision",
                 "seq": 2, "ts": 1700000100.0, "embedding": [0.3]},
                {"kind": "fact", "seq": 3}]
        (legacy / "kestrel" / "memory.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        (legacy / "wren" / "memory.jsonl").write_text(json.dumps(
            {"text": "wren shared note", "kind": "fact", "seq": 1,
             "ts": 5.0}) + "\n")
        return legacy

    def test_map_legacy_scope(self):
        from cousin_lib.hive import map_legacy_scope
        self.assertEqual(map_legacy_scope(None), (["own", "shared"], []))
        self.assertEqual(map_legacy_scope(["own"]), (["own"], []))
        self.assertEqual(map_legacy_scope(["shared", "own"]),
                         (["shared", "own"], []))
        self.assertEqual(map_legacy_scope(["roster"]), (["own"], ["roster"]))
        self.assertEqual(map_legacy_scope("shared"), (["shared"], []))

    def test_tokens_keep_their_strings_and_memory_its_ts_and_kind(self):
        from cousin_lib.hive import import_legacy
        legacy = self._fixtures()
        store = self._store()
        report = import_legacy(store, legacy / "tokens.json", legacy,
                               shared_slugs=["wren"])
        self.assertEqual(report["tokens_added"], 3)
        self.assertEqual(report["tokens_skipped"], ["entry 4"])
        self.assertEqual(report["scope_dropped"], {"owl": ["roster"]})
        self.assertEqual(store.resolve(
            "hive_legacyKestrelToken0000000000000"),
            {"slug": "kestrel", "scope": {"own", "shared"}})
        self.assertEqual(store.resolve(
            "hive_legacyWrenToken00000000000000000")["scope"],
            {"own", "shared"})
        self.assertEqual(store.resolve(
            "hive_legacyOwlToken000000000000000000")["scope"], {"own"})
        rows = [dict(r) for r in store.conn.execute(
            "SELECT slug, text, scope, ts, kind, vec FROM memory ORDER BY id")]
        self.assertEqual(rows, [
            {"slug": "kestrel", "text": "kestrel fact one", "scope": "own",
             "ts": 1700000000.5, "kind": "fact", "vec": None},
            {"slug": "kestrel", "text": "kestrel decided a thing",
             "scope": "own", "ts": 1700000100.0, "kind": "decision",
             "vec": None},
            {"slug": "wren", "text": "wren shared note", "scope": "shared",
             "ts": 5.0, "kind": "fact", "vec": None}])
        self.assertEqual(report["memory_skipped"], 2)
        again = import_legacy(store, legacy / "tokens.json", legacy,
                              shared_slugs=["wren"])
        self.assertEqual((again["tokens_added"], again["tokens_present"],
                          again["memory_added"], again["memory_present"]),
                         (0, 3, 0, 3))

    def test_an_imported_token_works_against_the_queen(self):
        from cousin_lib.hive import import_legacy
        legacy = self._fixtures()
        store = self._store()
        import_legacy(store, legacy / "tokens.json", legacy)
        url = self._queen(store)
        status, body = self._call(
            url, "/hive/recall?q=decided",
            token="hive_legacyKestrelToken0000000000000")
        self.assertEqual((status, body["memories"]),
                         (200, ["kestrel decided a thing"]))

    def test_a_token_string_owned_by_another_slug_is_a_conflict(self):
        from cousin_lib.hive import import_legacy
        legacy = self._fixtures()
        store = self._store()
        store.import_token("hive_legacyWrenToken00000000000000000", "owl",
                           ["own"])
        report = import_legacy(store, legacy / "tokens.json")
        self.assertEqual(report["token_conflicts"], ["wren"])
        self.assertEqual(store.resolve(
            "hive_legacyWrenToken00000000000000000")["slug"], "owl")


class TestOperatorCli(HiveCase):
    def _main(self, argv):
        import contextlib
        import io

        from cousin_lib.hive import hive_main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = hive_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_import_legacy_never_prints_a_token(self):
        legacy = TestImportLegacy._fixtures(self)
        rc, out, err = self._main(["import-legacy", "--tokens",
                                   str(legacy / "tokens.json"),
                                   "--memory-dir", str(legacy)])
        self.assertEqual(rc, 0, err)
        self.assertIn("tokens: 3 added", out)
        self.assertIn("memory: 3 added, 0 already present, 2 unusable", out)
        self.assertNotIn("hive_legacy", out + err)
        rc, out, _ = self._main(["import-legacy", "--tokens",
                                 str(legacy / "tokens.json"),
                                 "--memory-dir", str(legacy)])
        self.assertIn("tokens: 0 added, 3 already present", out)

    def test_revoke_nodes_forget(self):
        rc, out, _ = self._main(["mint", "kestrel"])
        token = out.strip()
        store = HiveStore(self.root / "shared" / "hive")
        self.addCleanup(store.close)
        store.checkin("kestrel", name="Kestrel", role="", host="198.51.100.7",
                      port=8210)
        rc, out, _ = self._main(["nodes"])
        self.assertEqual(rc, 0)
        self.assertIn("kestrel", out)
        self.assertIn("online", out)
        self.assertIn("198.51.100.7:8210", out)
        self.assertNotIn(token, out)
        self.assertEqual(self._main(["forget", "kestrel"])[0], 1)
        self.assertEqual(self._main(["revoke", "kestrel"])[0], 0)
        self.assertIsNone(store.resolve(token))
        self.assertIn("revoked", self._main(["nodes"])[1])
        self.assertEqual(self._main(["revoke", "kestrel"])[0], 1)
        self.assertEqual(self._main(["forget", "kestrel"])[0], 0)
        self.assertEqual(self._main(["nodes"])[1].strip(), "no nodes")


if __name__ == "__main__":
    unittest.main()
