"""The hive: an authenticated cross-machine message bus.

docs/hive-spec.md is the contract. Off by default: no queen runs and
no token is minted until an operator starts one. The design's spine is
one authenticated route and no others - every request's identity comes
from its bearer token, never from the body, so a node cannot claim to
be another; and the two unauthenticated LAN-trust shortcuts the source
framework used do not exist here.

The client fails toward local: a cousin with no reachable queen raises
HiveError so the caller behaves single-machine, never crashing.
"""
import json
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe


class HiveError(Exception):
    """A hive call could not complete; the caller falls back to local."""


class HiveStore:
    """Durable queen state: tokens (identity + scope), per-slug
    inboxes, and the shared memory corpus. The token is the identity;
    scope on the token gates the shared corpus."""

    def __init__(self, root):
        self.path = Path(root)
        self.path.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path / "hive.db",
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS tokens ("
            " token TEXT PRIMARY KEY, slug TEXT NOT NULL,"
            " scope TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS inbox ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " recipient TEXT NOT NULL, sender TEXT NOT NULL,"
            " msg_id TEXT NOT NULL, body TEXT NOT NULL, ts REAL,"
            " UNIQUE(recipient, msg_id));"
            "CREATE TABLE IF NOT EXISTS memory ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " slug TEXT NOT NULL, text TEXT NOT NULL,"
            " scope TEXT NOT NULL, ts REAL);")
        self.conn.commit()

    def mint_token(self, slug, *, scope=("own",)):
        """Mint a bearer token for a node. Idempotent per slug: the
        same slug keeps its token so re-minting does not orphan a
        deployed node."""
        with self._lock:
            row = self.conn.execute(
                "SELECT token FROM tokens WHERE slug=?", (slug,)
            ).fetchone()
            if row:
                return row["token"]
            token = "hive_" + token_urlsafe(24)
            self.conn.execute(
                "INSERT INTO tokens (token, slug, scope) VALUES (?, ?, ?)",
                (token, slug, json.dumps(list(scope))))
            self.conn.commit()
            return token

    def resolve(self, token):
        """token -> {slug, scope}, or None. The identity of every
        request is this, never the request body."""
        if not token:
            return None
        row = self.conn.execute(
            "SELECT slug, scope FROM tokens WHERE token=?", (token,)
        ).fetchone()
        if not row:
            return None
        return {"slug": row["slug"], "scope": set(json.loads(row["scope"]))}

    def deliver(self, *, sender, recipient, msg_id, body):
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO inbox"
                " (recipient, sender, msg_id, body, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (recipient, sender, msg_id, body, time.time()))
            self.conn.commit()

    def read_inbox(self, slug, *, since=0):
        rows = self.conn.execute(
            "SELECT id, sender, body FROM inbox"
            " WHERE recipient=? AND id>? ORDER BY id",
            (slug, since)).fetchall()
        return [{"id": r["id"], "from": r["sender"], "body": r["body"]}
                for r in rows]

    def append_memory(self, slug, text, *, scope="own"):
        with self._lock:
            self.conn.execute(
                "INSERT INTO memory (slug, text, scope, ts)"
                " VALUES (?, ?, ?, ?)",
                (slug, text, scope, time.time()))
            self.conn.commit()

    def recall(self, query, *, scopes, slug):
        """Substring recall over the memories the caller may read.

        `slug` is the caller's identity (from its token) and is
        required: 'own' means the caller's OWN memories only, never
        every node's, and it still needs 'own' in the token's scope.
        Any other scope ('shared') is readable by every token carrying
        it. Filtered in SQL so no row outside the boundary is even
        loaded."""
        scopes = set(scopes)
        other = sorted(s for s in scopes if s != "own")
        clauses, params = [], []
        if "own" in scopes:
            clauses.append("(scope='own' AND slug=?)")
            params.append(slug)
        if other:
            clauses.append("(scope IN (%s))" % ",".join("?" * len(other)))
            params.extend(other)
        if not clauses:
            return []
        rows = self.conn.execute(
            "SELECT text FROM memory WHERE %s ORDER BY id DESC"
            % " OR ".join(clauses), params).fetchall()
        needle = query.lower()
        return [r["text"] for r in rows if needle in r["text"].lower()]


def build_queen(store, *, host="127.0.0.1", port=0):
    return _Queen(store, host=host, port=port)


class _Queen:
    def __init__(self, store, *, host="127.0.0.1", port=0):
        self.store = store
        queen = self

        class Handler(_QueenHandler):
            queen_store = store

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self._thread = None

    @property
    def port(self):
        return self.httpd.server_address[1]

    def start(self):
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)


class _QueenHandler(BaseHTTPRequestHandler):
    queen_store = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _identity(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        return self.queen_store.resolve(token)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/hive/health":
            self._json(200, {"status": "ok"})
            return
        identity = self._identity()
        if identity is None:
            self._json(401, {"error": "unauthorized"})
            return
        if path == "/hive/inbox":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                          if "?" in self.path else "")
            since = int((query.get("since") or ["0"])[0])
            self._json(200, {"messages": self.queen_store.read_inbox(
                identity["slug"], since=since)})
        elif path == "/hive/recall":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                          if "?" in self.path else "")
            q = (query.get("q") or [""])[0]
            self._json(200, {"memories": self.queen_store.recall(
                q, scopes=identity["scope"], slug=identity["slug"])})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        identity = self._identity()
        if identity is None:
            self._json(401, {"error": "unauthorized"})
            return
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/hive/msg":
            # Sender is the token's slug, NEVER the body - a node
            # cannot claim to be another.
            self.queen_store.deliver(
                sender=identity["slug"], recipient=body.get("to"),
                msg_id=body.get("id"), body=body.get("body", ""))
            self._json(200, {"ok": True})
        elif path == "/hive/memory":
            scope = body.get("scope", "own")
            # Writes are scope-gated like reads: a token may only write
            # into a tier it could read, so an own-only node cannot
            # plant text in the shared corpus or invent a tier.
            if scope not in identity["scope"]:
                self._json(403, {"error": "scope %r not on this token"
                                 % scope})
                return
            self.queen_store.append_memory(
                identity["slug"], body.get("text", ""), scope=scope)
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})


def _client_call(queen_url, path, token, *, method="GET", body=None):
    request = urllib.request.Request(
        queen_url + path,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={"Authorization": "Bearer %s" % token})
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as err:
        raise HiveError("hive unreachable (%s); falling back to local"
                        % err)


def hive_send(*, queen_url, token, to, body, msg_id):
    return _client_call(queen_url, "/hive/msg", token, method="POST",
                        body={"to": to, "id": msg_id, "body": body})


def hive_recall(*, queen_url, token, query):
    import urllib.parse
    return _client_call(
        queen_url,
        "/hive/recall?q=" + urllib.parse.quote(query), token
    ).get("memories", [])


def _store_from_env():
    from cousin_lib.config import FrameworkConfig
    return HiveStore(FrameworkConfig.from_env().root / "shared" / "hive")


def hive_main(argv=None):
    """Console entry point. `mint` and `serve` are operator acts on the
    queen machine; `send`/`recall` are node-side client calls.

    Enrolling a node is deliberately manual (mint here, move the token
    to the node) - a framework that pushed itself onto another machine
    would be a larger trust surface than the hive needs."""
    import argparse
    import sys

    from cousin_lib.config import FrameworkConfig, MissingConfigError

    parser = argparse.ArgumentParser(prog="cousin-hive")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint")
    m.add_argument("slug")
    m.add_argument("--scope", default="own,shared")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8101)
    for name in ("send", "recall"):
        p = sub.add_parser(name)
        p.add_argument("--queen", required=True)
        p.add_argument("--token", required=True)
        if name == "send":
            p.add_argument("--to", required=True)
            p.add_argument("--id", required=True)
            p.add_argument("body")
        else:
            p.add_argument("query")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "mint":
            token = _store_from_env().mint_token(
                args.slug, scope=args.scope.split(","))
            print(token)
            return 0
        if args.cmd == "serve":
            queen = build_queen(_store_from_env(), host=args.host,
                                port=args.port)
            print("cousin-hive: queen on %s:%d"
                  % (args.host, queen.port))
            try:
                queen.httpd.serve_forever()
            except KeyboardInterrupt:
                queen.stop()
            return 0
    except MissingConfigError as err:
        print("cousin-hive: %s" % err, file=sys.stderr)
        return 2
    try:
        if args.cmd == "send":
            hive_send(queen_url=args.queen, token=args.token,
                      to=args.to, body=args.body, msg_id=args.id)
            print("sent")
        else:
            for text in hive_recall(queen_url=args.queen,
                                    token=args.token, query=args.query):
                print("- " + text)
    except HiveError as err:
        print("cousin-hive: %s" % err, file=sys.stderr)
        return 1
    return 0
