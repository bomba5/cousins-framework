"""The hive: an authenticated cross-machine message bus.

docs/reference/hive-api.md is the contract. Off by default: no queen runs and
no token is minted until an operator starts one (a standalone
`cousin-hive serve`, or the console with config/hive.toml enabled).
The design's spine is one authenticated route set and no others -
every request's identity comes from its bearer token, never from the
body, so a node cannot claim to be another; and the two
unauthenticated LAN-trust shortcuts an earlier version used do not
exist here.

The route logic lives once, in `handle_request`: the standalone queen
(`build_queen`) and the console (cousin_lib/console/hive.py) both call
it, so the two hosts cannot drift.

The client fails toward local: a cousin with no reachable queen raises
HiveError so the caller behaves single-machine, never crashing.
"""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from cousin_lib.sqlite_util import add_columns, wal
from secrets import token_urlsafe

SCOPES = ("own", "shared")
DEFAULT_CHECKIN_SECONDS = 60
MIN_CHECKIN_SECONDS = 5
MAX_INBOX_WAIT_SECONDS = 30.0
DEFAULT_RECALL_K = 3
MAX_RECALL_K = 50
DEFAULT_MIN_SCORE = 0.45
# The largest request body a queen host reads (memories and messages
# are text); a bigger one is refused before it is read.
MAX_BODY_BYTES = 1024 * 1024
# A node is online when its last checkin is within this many checkin
# periods: one missed checkin is jitter, two and a half is gone.
ONLINE_FACTOR = 2.5
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
# Where `cousin-hive send` and `recall` find their token when no
# --token-file is given: the node's own (node.env sets it).
TOKEN_ENV = "HIVE_TOKEN"


class HiveError(Exception):
    """A hive call could not complete; the caller falls back to local."""


class HiveConfigError(Exception):
    """config/hive.toml is present but unusable; the message says why."""


# ---- config/hive.toml ----------------------------------------------------

def hive_config(root):
    """config/hive.toml as a dict, or None when the hive is off (the
    file is absent, or `enabled` is not true). Raises HiveConfigError
    when the file is present but unusable, so a caller can refuse
    loudly instead of reading a typo as "off".

    Keys: enabled (bool), public_url (the queen as nodes reach it,
    required when enabled), checkin_seconds (default 60, at least 5),
    home_cousin (optional: the local cousin a node's [tell-home: ...]
    reaches through the queen's authenticated POST /hive/tell-home, phase
    10a). The legacy home_chat_url (a per-cousin chat server) is not
    read: 2.0.0 runs none."""
    path = Path(root) / "config" / "hive.toml"
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise HiveConfigError("%s is unusable: %s" % (path, err))
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HiveConfigError("%s: enabled must be true or false" % path)
    if not enabled:
        return None
    public_url = data.get("public_url")
    if not isinstance(public_url, str) or not re.match(
            r"^https?://[^/\s]+", public_url or ""):
        raise HiveConfigError(
            "%s: public_url must be the queen's http(s) URL as nodes"
            " reach it, e.g. http://192.0.2.10:8600" % path)
    checkin = data.get("checkin_seconds", DEFAULT_CHECKIN_SECONDS)
    if isinstance(checkin, bool) or not isinstance(checkin, int) \
            or checkin < MIN_CHECKIN_SECONDS:
        raise HiveConfigError("%s: checkin_seconds must be an integer >= %d"
                              % (path, MIN_CHECKIN_SECONDS))
    home_cousin = data.get("home_cousin") or ""
    if not isinstance(home_cousin, str) or (
            home_cousin and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", home_cousin)):
        raise HiveConfigError("%s: home_cousin must be a cousin's slug" % path)
    return {"enabled": True, "public_url": public_url.rstrip("/"),
            "checkin_seconds": checkin, "home_cousin": home_cousin}


# ---- the embedder --------------------------------------------------------

class Embedder:
    """The semantic leg, over the same config/embedding.toml contract
    cousin-memory search uses. `embed` raises on any failure; after
    one, the embedder stays down for `cooldown` seconds so a dead
    service costs one timeout, not one per recall."""

    def __init__(self, config, *, embed_fn=None, cooldown=30.0,
                 clock=time.time):
        from cousin_lib import memory_search
        self.config = config
        self.model = str(config.get("model", ""))
        self._embed = embed_fn or memory_search._embed
        self.cooldown = cooldown
        self.clock = clock
        self._down_until = 0.0
        recall = config.get("recall") or {}
        try:
            self.min_score = float(recall.get("min_score", DEFAULT_MIN_SCORE))
        except (TypeError, ValueError):
            self.min_score = DEFAULT_MIN_SCORE

    @property
    def available(self):
        return self.clock() >= self._down_until

    def embed(self, text):
        if not self.available:
            raise HiveError("embedder cooling down after a failure")
        try:
            vec = self._embed(text, self.config)
        except Exception as err:  # noqa: BLE001 - any failure degrades
            self._down_until = self.clock() + self.cooldown
            raise HiveError("embedding failed: %s" % err)
        if not isinstance(vec, list) or not vec:
            self._down_until = self.clock() + self.cooldown
            raise HiveError("embedding service returned no vector")
        return [float(x) for x in vec]


def embedder_for_root(root):
    """An Embedder when config/embedding.toml is present and parses,
    else None (substring recall)."""
    from cousin_lib import memory_search
    config = memory_search._embedding_config(root)
    if not isinstance(config, dict):
        return None
    return Embedder(config)


class EmbedderSource:
    """A QueenContext `embedder` callable over a framework root: the
    Embedder for config/embedding.toml, rebuilt only when the file
    changes, so its failure cooldown survives from one recall to the
    next. None while the file is absent or unusable."""

    def __init__(self, root):
        self.path = Path(root) / "config" / "embedding.toml"
        self.root = root
        self._stamp = object()
        self._embedder = None
        self._lock = threading.Lock()

    def __call__(self):
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            stamp = None
        with self._lock:
            if stamp != self._stamp:
                self._stamp = stamp
                self._embedder = (embedder_for_root(self.root)
                                  if stamp is not None else None)
            return self._embedder


def default_min_score(root):
    """embedding.toml [recall] min_score, else 0.45."""
    from cousin_lib import memory_search
    try:
        config = memory_search._embedding_config(root)
    except Exception:  # noqa: BLE001
        config = None
    if isinstance(config, dict):
        try:
            return float((config.get("recall") or {}).get(
                "min_score", DEFAULT_MIN_SCORE))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MIN_SCORE


def _cosine(a, b):
    from cousin_lib import memory_search
    return memory_search._cosine(a, b)


# ---- the store -----------------------------------------------------------

class HiveStore:
    """Durable queen state: tokens (identity + scope + revocation),
    per-slug inboxes, the memory corpus with cached vectors, and the
    nodes that checked in. The token is the identity; scope on the
    token gates the corpus.

    Schema changes are additive (new columns and tables only), so a
    hive.db written by an earlier release opens and keeps working."""

    def __init__(self, root):
        self.path = Path(root)
        self.path.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path / "hive.db",
                                    check_same_thread=False, timeout=10)
        wal(self.conn)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        with self._lock:
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
                " scope TEXT NOT NULL, ts REAL);"
                "CREATE TABLE IF NOT EXISTS nodes ("
                " slug TEXT PRIMARY KEY, name TEXT, role TEXT,"
                " host TEXT, port INTEGER, version TEXT,"
                " last_seen REAL, first_seen REAL);")
            self._add_columns("tokens", {"revoked": "INTEGER DEFAULT 0",
                                         "created": "REAL",
                                         "name": "TEXT", "role": "TEXT"})
            self._add_columns("memory", {"kind": "TEXT", "vec": "TEXT",
                                         "vec_model": "TEXT",
                                         "origin": "TEXT"})
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS memory_origin"
                " ON memory(origin)")
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()

    def _add_columns(self, table, columns):
        add_columns(self.conn, table, columns)

    # -- tokens

    def mint_token(self, slug, *, scope=("own",), name=None, role=None):
        """Mint a bearer token for a node. Idempotent per slug: the
        same slug keeps its live token so re-minting does not orphan a
        deployed node. A slug whose tokens are all revoked gets a new
        one."""
        with self._lock:
            row = self.conn.execute(
                "SELECT token FROM tokens WHERE slug=? AND"
                " COALESCE(revoked, 0)=0 ORDER BY rowid LIMIT 1", (slug,)
            ).fetchone()
            if row:
                if name is not None or role is not None:
                    self.conn.execute(
                        "UPDATE tokens SET name=COALESCE(?, name),"
                        " role=COALESCE(?, role) WHERE token=?",
                        (name, role, row["token"]))
                    self.conn.commit()
                return row["token"]
            token = "hive_" + token_urlsafe(24)
            self.conn.execute(
                "INSERT INTO tokens (token, slug, scope, revoked, created,"
                " name, role) VALUES (?, ?, ?, 0, ?, ?, ?)",
                (token, slug, json.dumps(list(scope)), time.time(), name,
                 role))
            self.conn.commit()
            return token

    def import_token(self, token, slug, scope):
        """Insert an existing token string (a legacy queen's) as is.
        'added' when inserted, 'present' when the same token already
        maps to the same slug, 'conflict' when it maps to another."""
        with self._lock:
            row = self.conn.execute(
                "SELECT slug FROM tokens WHERE token=?", (token,)
            ).fetchone()
            if row:
                return "present" if row["slug"] == slug else "conflict"
            self.conn.execute(
                "INSERT INTO tokens (token, slug, scope, revoked, created)"
                " VALUES (?, ?, ?, 0, ?)",
                (token, slug, json.dumps(list(scope)), time.time()))
            self.conn.commit()
            return "added"

    def resolve(self, token):
        """token -> {slug, scope}, or None (unknown or revoked). The
        identity of every request is this, never the request body."""
        if not token:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT slug, scope FROM tokens WHERE token=? AND"
                " COALESCE(revoked, 0)=0", (token,)
            ).fetchone()
        if not row:
            return None
        return {"slug": row["slug"], "scope": set(json.loads(row["scope"]))}

    def token_for(self, slug):
        """The slug's live token, or None. Used by the console to
        authenticate its chat proxy to the node; never printed."""
        with self._lock:
            row = self.conn.execute(
                "SELECT token FROM tokens WHERE slug=? AND"
                " COALESCE(revoked, 0)=0 ORDER BY rowid LIMIT 1", (slug,)
            ).fetchone()
        return row["token"] if row else None

    def minted_name(self, slug):
        """The name the operator gave `slug`'s live token when it was minted
        (the build dialog, cousin-spawn-node), or None. Never the name a
        node reports at checkin: that one is the node's own claim."""
        with self._lock:
            row = self.conn.execute(
                "SELECT name FROM tokens WHERE slug=? AND COALESCE(revoked, 0)=0"
                " ORDER BY created DESC LIMIT 1", (slug,)).fetchone()
        return (row["name"] or None) if row else None

    def revoke(self, slug):
        """Revoke every live token of the slug; returns how many. The
        rows stay (marked) so the console can show the node as
        revoked until it is forgotten."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE tokens SET revoked=1 WHERE slug=? AND"
                " COALESCE(revoked, 0)=0", (slug,))
            self.conn.commit()
            return cur.rowcount

    def forget(self, slug):
        """Remove a revoked node: its node row and its revoked tokens.
        Refuses (ValueError) while the slug still has a live token:
        revoke first. Memory and inbox rows stay; they are the fleet's
        record, not the node's."""
        with self._lock:
            if self.token_for(slug):
                raise ValueError("%s still has a live token; revoke it first"
                                 % slug)
            tokens = self.conn.execute(
                "DELETE FROM tokens WHERE slug=?", (slug,)).rowcount
            nodes = self.conn.execute(
                "DELETE FROM nodes WHERE slug=?", (slug,)).rowcount
            self.conn.commit()
            return {"tokens": tokens, "nodes": nodes}

    # -- nodes

    def checkin(self, slug, *, name, role, host, port, version=None,
                now=None):
        """Record a node's checkin; returns the previous last_seen
        (None on the first checkin)."""
        now = time.time() if now is None else now
        with self._lock:
            row = self.conn.execute(
                "SELECT last_seen FROM nodes WHERE slug=?", (slug,)
            ).fetchone()
            previous = row["last_seen"] if row else None
            self.conn.execute(
                "INSERT INTO nodes (slug, name, role, host, port, version,"
                " last_seen, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(slug) DO UPDATE SET name=excluded.name,"
                " role=excluded.role, host=excluded.host,"
                " port=excluded.port, version=excluded.version,"
                " last_seen=excluded.last_seen",
                (slug, name, role, host, port, version, now, now))
            self.conn.commit()
            return previous

    def node(self, slug):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM nodes WHERE slug=?", (slug,)).fetchone()
        return dict(row) if row else None

    def nodes(self):
        """Every node the queen knows: each checked-in node, plus each
        slug with a token and no node row yet ("built, not checked
        in"). Rows carry token state (live / revoked), never a token."""
        with self._lock:
            node_rows = {r["slug"]: dict(r) for r in self.conn.execute(
                "SELECT * FROM nodes ORDER BY slug")}
            token_rows = self.conn.execute(
                "SELECT slug, COALESCE(revoked, 0) AS revoked, created,"
                " name, role FROM tokens ORDER BY rowid").fetchall()
        tokens = {}
        for r in token_rows:
            entry = tokens.setdefault(r["slug"], {
                "live": 0, "revoked": 0, "created": r["created"],
                "name": r["name"], "role": r["role"]})
            entry["revoked" if r["revoked"] else "live"] += 1
            entry["name"] = entry["name"] or r["name"]
            entry["role"] = entry["role"] or r["role"]
        out = []
        for slug in sorted(set(node_rows) | set(tokens)):
            node = node_rows.get(slug)
            tok = tokens.get(slug, {"live": 0, "revoked": 0,
                                    "created": None, "name": None,
                                    "role": None})
            out.append({
                "slug": slug,
                "name": (node or {}).get("name") or tok["name"] or slug,
                "role": (node or {}).get("role") or tok["role"] or "",
                "host": (node or {}).get("host"),
                "port": (node or {}).get("port"),
                "version": (node or {}).get("version"),
                "last_seen": (node or {}).get("last_seen"),
                "first_seen": (node or {}).get("first_seen"),
                "checked_in": node is not None,
                "revoked": tok["live"] == 0 and tok["revoked"] > 0,
                "has_token": tok["live"] > 0,
                "created": tok["created"],
            })
        return out

    # -- inbox

    def deliver(self, *, sender, recipient, msg_id, body):
        with self._cond:
            self.conn.execute(
                "INSERT OR IGNORE INTO inbox"
                " (recipient, sender, msg_id, body, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (recipient, sender, msg_id, body, time.time()))
            self.conn.commit()
            self._cond.notify_all()

    def read_inbox(self, slug, *, since=0):
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, sender, body FROM inbox"
                " WHERE recipient=? AND id>? ORDER BY id",
                (slug, since)).fetchall()
        return [{"id": r["id"], "from": r["sender"], "body": r["body"]}
                for r in rows]

    def wait_inbox(self, slug, *, since=0, wait=0.0, slice_seconds=1.0):
        """read_inbox, long-polled: block up to `wait` seconds until a
        message past `since` arrives. Woken at once by a delivery in
        this process, and re-reads every `slice_seconds` so a delivery
        written by another process (a CLI, a second queen host on the
        same file) is seen too. Only the calling request thread waits."""
        deadline = time.time() + max(0.0, wait)
        with self._cond:
            while True:
                rows = self.read_inbox(slug, since=since)
                remaining = deadline - time.time()
                if rows or remaining <= 0:
                    return rows
                self._cond.wait(min(remaining, slice_seconds))

    # -- memory

    def append_memory(self, slug, text, *, scope="own", kind=None, ts=None,
                      origin=None):
        """Append one memory row; returns its id, or None when `origin`
        names a row already imported (idempotent imports)."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO memory (slug, text, scope, ts, kind,"
                " origin) VALUES (?, ?, ?, ?, ?, ?)",
                (slug, text, scope, time.time() if ts is None else ts, kind,
                 origin))
            self.conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def _readable(self, *, scopes, slug):
        """(where-clause, params) for the rows a caller may read, or
        None when it may read none. 'own' means the caller's OWN rows
        (and needs 'own' on the token); any other scope is readable by
        every token carrying it."""
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
            return None
        return " OR ".join(clauses), params

    def _rows(self, *, scopes, slug):
        where = self._readable(scopes=scopes, slug=slug)
        if where is None:
            return []
        with self._lock:
            return self.conn.execute(
                "SELECT id, slug, text, scope, ts, vec, vec_model FROM memory"
                " WHERE %s ORDER BY id DESC" % where[0], where[1]).fetchall()

    def recall(self, query, *, scopes, slug):
        """Substring recall over the memories the caller may read,
        newest first, as texts. `slug` is the caller's identity (from
        its token) and is required. Filtered in SQL so no row outside
        the boundary is even loaded."""
        return [r["text"] for r in self.recall_scored(
            query, scopes=scopes, slug=slug, k=None)]

    def recall_scored(self, query, *, scopes, slug, k=DEFAULT_RECALL_K,
                      min_score=DEFAULT_MIN_SCORE, embedder=None):
        """[{text, score, slug, ts}] for the caller's readable rows.

        Semantic when `embedder` is given and answers: cosine of the
        query against each row's cached vector (computed now for a row
        that has none, or one embedded by another model, and stored),
        kept at `min_score` and above, best first. Otherwise, or when
        the embedder fails anywhere in the pass, substring: every hit
        scores 1.0, newest first. `k` None means unbounded."""
        rows = self._rows(scopes=scopes, slug=slug)
        if embedder is not None and query.strip():
            try:
                return self._semantic(query, rows, k, min_score, embedder)
            except HiveError:
                pass
        needle = query.lower()
        hits = [{"text": r["text"], "score": 1.0, "slug": r["slug"],
                 "ts": r["ts"]} for r in rows if needle in r["text"].lower()]
        return hits if k is None else hits[:k]

    def _semantic(self, query, rows, k, min_score, embedder):
        qvec = embedder.embed(query)
        scored = []
        for r in rows:
            vec = None
            if r["vec"] and r["vec_model"] == embedder.model:
                try:
                    vec = json.loads(r["vec"])
                except ValueError:
                    vec = None
            if vec is None:
                vec = embedder.embed(r["text"])
                self._store_vec(r["id"], vec, embedder.model)
            score = _cosine(qvec, vec)
            if score >= min_score:
                scored.append({"text": r["text"], "score": round(score, 4),
                               "slug": r["slug"], "ts": r["ts"]})
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored if k is None else scored[:k]

    def _store_vec(self, row_id, vec, model):
        with self._lock:
            self.conn.execute(
                "UPDATE memory SET vec=?, vec_model=? WHERE id=?",
                (json.dumps(vec), model, row_id))
            self.conn.commit()

    def embed_row(self, row_id, embedder):
        """Best-effort: compute and cache one row's vector (the append
        path calls this off the request thread)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT text, vec_model FROM memory WHERE id=?", (row_id,)
            ).fetchone()
        if not row or row["vec_model"] == embedder.model:
            return False
        try:
            vec = embedder.embed(row["text"])
        except HiveError:
            return False
        self._store_vec(row_id, vec, embedder.model)
        return True


# ---- the route logic, shared by every queen host ---------------------------

class QueenContext:
    """What `handle_request` needs besides the request: the store,
    the checkin period it hands to nodes, the embedder (a callable
    returning an Embedder or None, asked per recall so an edited
    embedding.toml applies without a restart), the default min_score,
    and an optional `on_checkin(slug, previous_last_seen)` hook the
    console uses to push a card update, and `tell_home(slug, body) ->
    (status, payload)`, the delivery of a node's tell-home (None: the
    route answers 404, no home cousin is configured)."""

    def __init__(self, store, *, checkin_seconds=DEFAULT_CHECKIN_SECONDS,
                 embedder=None, min_score=None, on_checkin=None,
                 embed_in_background=True, tell_home=None):
        self.store = store
        self.tell_home = tell_home
        self.checkin_seconds = checkin_seconds
        self.embedder = embedder or (lambda: None)
        self.min_score = min_score
        self.on_checkin = on_checkin
        self.embed_in_background = embed_in_background


def _bearer(headers):
    auth = (headers.get("Authorization") or "") if headers else ""
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def _q(query, name, default=None):
    values = query.get(name)
    return values[0] if values else default


def handle_request(ctx, method, path, query_string, headers, raw_body,
                   client_host):
    """One queen request -> (status, json payload). The identity is
    the bearer token's slug and nothing else; `client_host` is the
    peer address as the host server saw it (the checkin records it)."""
    if method == "GET" and path == "/hive/health":
        return 200, {"status": "ok"}
    identity = ctx.store.resolve(_bearer(headers))
    if identity is None:
        return 401, {"error": "unauthorized"}
    slug = identity["slug"]
    query = urllib.parse.parse_qs(query_string or "")
    if method == "GET":
        if path == "/hive/inbox":
            try:
                since = int(_q(query, "since", "0") or 0)
                wait = float(_q(query, "wait", "0") or 0)
            except ValueError:
                return 400, {"error": "since must be an integer and wait"
                                      " a number"}
            wait = max(0.0, min(wait, MAX_INBOX_WAIT_SECONDS))
            if wait > 0:
                messages = ctx.store.wait_inbox(slug, since=since, wait=wait)
            else:
                messages = ctx.store.read_inbox(slug, since=since)
            return 200, {"messages": messages}
        if path == "/hive/recall":
            q = _q(query, "q", "") or ""
            try:
                k = int(_q(query, "k", str(DEFAULT_RECALL_K)))
                raw_min = _q(query, "min_score")
                min_score = (float(raw_min) if raw_min not in (None, "")
                             else ctx.min_score)
            except ValueError:
                return 400, {"error": "k must be an integer and min_score"
                                      " a number"}
            k = max(1, min(k, MAX_RECALL_K))
            embedder = ctx.embedder()
            if min_score is None:
                min_score = (embedder.min_score if embedder is not None
                             else DEFAULT_MIN_SCORE)
            results = ctx.store.recall_scored(
                q, scopes=identity["scope"], slug=slug, k=k,
                min_score=min_score, embedder=embedder)
            return 200, {"memories": [r["text"] for r in results],
                         "results": results}
        return 404, {"error": "not found"}
    if method != "POST":
        return 404, {"error": "not found"}
    try:
        body = json.loads(raw_body) if raw_body and raw_body.strip() else {}
    except ValueError:
        return 400, {"error": "malformed JSON"}
    if not isinstance(body, dict):
        return 400, {"error": "JSON body must be an object"}
    if path == "/hive/msg":
        to, msg_id, text = body.get("to"), body.get("id"), body.get("body")
        if not isinstance(to, str) or not to or not isinstance(
                msg_id, (str, int)) or msg_id == "" \
                or not isinstance(text, str):
            return 400, {"error": "to, id and body are required"}
        # Sender is the token's slug, NEVER the body - a node cannot
        # claim to be another.
        ctx.store.deliver(sender=slug, recipient=to, msg_id=str(msg_id),
                          body=text)
        return 200, {"ok": True}
    if path == "/hive/tell-home":
        # The node's [tell-home: ...]: to the install's home cousin only,
        # as the token's slug (peer_inbound.accept: window, dedupe, rate).
        if ctx.tell_home is None:
            return 404, {"error": "no home cousin: config/hive.toml sets no home_cousin"}
        return ctx.tell_home(slug, body)
    if path == "/hive/memory":
        scope = body.get("scope", "own")
        text = body.get("text")
        kind = body.get("kind")
        if not isinstance(text, str) or not text.strip():
            return 400, {"error": "text is required"}
        if kind is not None and not isinstance(kind, str):
            return 400, {"error": "kind must be a string"}
        # Writes are scope-gated like reads: a token may only write
        # into a tier it could read, so an own-only node cannot plant
        # text in the shared corpus or invent a tier.
        if scope not in identity["scope"]:
            return 403, {"error": "scope %r not on this token" % (scope,)}
        row_id = ctx.store.append_memory(slug, text, scope=scope, kind=kind)
        embedder = ctx.embedder()
        if embedder is not None and row_id is not None:
            if ctx.embed_in_background:
                threading.Thread(target=ctx.store.embed_row,
                                 args=(row_id, embedder),
                                 daemon=True).start()
            else:
                ctx.store.embed_row(row_id, embedder)
        return 200, {"ok": True, "id": row_id}
    if path == "/hive/checkin":
        port = body.get("port")
        name = body.get("name")
        role = body.get("role", "")
        version = body.get("version")
        if isinstance(port, bool) or not isinstance(port, int) \
                or not 0 < port < 65536:
            return 400, {"error": "port must be an integer 1-65535"}
        if not isinstance(name, str) or not name.strip():
            return 400, {"error": "name is required"}
        if not isinstance(role, str) or (
                version is not None and not isinstance(version, str)):
            return 400, {"error": "role and version must be strings"}
        previous = ctx.store.checkin(
            slug, name=name.strip()[:64], role=role[:500], host=client_host,
            port=port, version=(version or "")[:32] or None)
        if ctx.on_checkin is not None:
            try:
                ctx.on_checkin(slug, previous)
            except Exception:  # noqa: BLE001 - a hook never fails a checkin
                pass
        return 200, {"ok": True, "checkin_seconds": ctx.checkin_seconds}
    return 404, {"error": "not found"}


def is_online(last_seen, checkin_seconds, *, now=None):
    """A node is online when it checked in within ONLINE_FACTOR
    checkin periods."""
    if not last_seen:
        return False
    now = time.time() if now is None else now
    return now - last_seen <= ONLINE_FACTOR * checkin_seconds


# ---- the standalone queen ------------------------------------------------

def build_queen(store, *, host="127.0.0.1", port=0,
                checkin_seconds=DEFAULT_CHECKIN_SECONDS, embedder=None,
                min_score=None):
    return _Queen(store, host=host, port=port,
                  context=QueenContext(store, checkin_seconds=checkin_seconds,
                                       embedder=embedder,
                                       min_score=min_score))


class _Queen:
    def __init__(self, store, *, host="127.0.0.1", port=0, context=None):
        self.store = store
        self.context = context or QueenContext(store)

        class Handler(_QueenHandler):
            queen_context = self.context

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
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
    queen_context = None

    def log_message(self, fmt, *args):
        pass

    def _serve(self, method):
        path, _, query = self.path.partition("?")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            status, payload = 413, {"error": "body too large"}
        else:
            raw = self.rfile.read(length) if length else b""
            status, payload = handle_request(
                self.queen_context, method, path, query, self.headers, raw,
                self.client_address[0])
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


# ---- the client ----------------------------------------------------------

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
    return _client_call(
        queen_url,
        "/hive/recall?q=" + urllib.parse.quote(query), token
    ).get("memories", [])


# ---- import from the previous framework's queen -----------------------------

def map_legacy_scope(scope):
    """The previous queen's scope list -> this queen's, plus what was
    dropped. Its vocabulary was the same two words, 'own' and 'shared',
    with a missing scope meaning both; any other word has no meaning
    here and is dropped. A token left with nothing gets 'own' (the
    narrowest scope that still lets the node remember)."""
    if scope is None:
        scope = ["own", "shared"]
    if isinstance(scope, str):
        scope = [scope]
    kept, dropped = [], []
    for word in scope if isinstance(scope, list) else []:
        if word in SCOPES and word not in kept:
            kept.append(word)
        elif word not in SCOPES:
            dropped.append(word)
    return (kept or ["own"]), dropped


def import_legacy(store, tokens_path, memory_dir=None, *, shared_slugs=()):
    """Import the previous framework's queen into `store`.

    tokens.json maps token -> {"slug", "scope"}: each token is inserted
    with the SAME string (a deployed node then only changes its queen
    URL), scope mapped by `map_legacy_scope`. The memory directory
    holds <slug>/memory.jsonl append-only records ({text, kind, ts,
    seq, embedding}); each becomes a memory row for that slug with its
    original ts and kind, in 'own' scope (or 'shared' for a slug in
    `shared_slugs`, the old queen's shared-corpus list). The old
    vectors are not carried over (another model and task prefix);
    rows are re-embedded lazily. Idempotent: every row carries an
    origin key, and re-running imports nothing new.

    Returns counts; never a token value."""
    report = {"tokens_added": 0, "tokens_present": 0, "token_conflicts": [],
              "tokens_skipped": [], "scope_dropped": {},
              "memory_added": 0, "memory_present": 0, "memory_skipped": 0,
              "slugs": []}
    try:
        data = json.loads(Path(tokens_path).read_text())
    except (OSError, ValueError) as err:
        raise HiveError("cannot read %s: %s" % (tokens_path, err))
    if not isinstance(data, dict):
        raise HiveError("%s is not a JSON object of token -> entry"
                        % tokens_path)
    for index, (token, entry) in enumerate(data.items()):
        slug = entry.get("slug") if isinstance(entry, dict) else None
        if not isinstance(token, str) or not token or not isinstance(
                slug, str) or not _SLUG_RE.match(slug):
            report["tokens_skipped"].append("entry %d" % (index + 1))
            continue
        scope, dropped = map_legacy_scope(entry.get("scope"))
        if dropped:
            report["scope_dropped"][slug] = dropped
        outcome = store.import_token(token, slug, scope)
        if outcome == "added":
            report["tokens_added"] += 1
        elif outcome == "present":
            report["tokens_present"] += 1
        else:
            report["token_conflicts"].append(slug)
        if slug not in report["slugs"]:
            report["slugs"].append(slug)
    if memory_dir:
        base = Path(memory_dir)
        for sub in sorted(p for p in base.iterdir() if p.is_dir()):
            slug = sub.name
            path = sub / "memory.jsonl"
            if not _SLUG_RE.match(slug) or not path.is_file():
                continue
            scope = "shared" if slug in shared_slugs else "own"
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        report["memory_skipped"] += 1
                        continue
                    text = rec.get("text") if isinstance(rec, dict) else None
                    if not isinstance(text, str) or not text.strip():
                        report["memory_skipped"] += 1
                        continue
                    seq = rec.get("seq")
                    key = ("seq:%s" % seq if seq is not None else
                           "sha:" + hashlib.sha256(line.encode()).hexdigest())
                    ts = rec.get("ts")
                    row = store.append_memory(
                        slug, text, scope=scope,
                        kind=rec.get("kind") if isinstance(
                            rec.get("kind"), str) else None,
                        ts=float(ts) if isinstance(ts, (int, float)) else None,
                        origin="legacy:%s:%s" % (slug, key))
                    if row is None:
                        report["memory_present"] += 1
                    else:
                        report["memory_added"] += 1
    return report


# ---- the CLI -------------------------------------------------------------

def _root_from_env():
    """The queen's root for the operator subcommands: the shared
    discovery a typed command uses (FRAMEWORK_ROOT, COUSIN_HOME, else
    the checkout the operator is in)."""
    from cousin_lib.config import FrameworkConfig
    return FrameworkConfig.for_command().root


def _store_from_env():
    return HiveStore(_root_from_env() / "shared" / "hive")


def read_token_file(path, stdin=None):
    """The token in the file at `path`, or on standard input when `path` is
    `-`, stripped. ValueError, never quoting the content, when it cannot be
    read or is not one word. This is how a CLI takes a token: on its
    command line, every local user could read it (`ps`,
    /proc/<pid>/cmdline)."""
    where = "standard input" if path == "-" else path
    try:
        if path == "-":
            text = (stdin if stdin is not None else sys.stdin).read()
        else:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
    except (OSError, UnicodeDecodeError) as err:
        raise ValueError("cannot read the token from %s: %s"
                         % (where, getattr(err, "strerror", None) or type(err).__name__))
    token = text.strip()
    if len(token.split()) != 1:
        raise ValueError("%s does not hold one token" % where)
    return token


def warn_token_flag(prog):
    """The one line a deprecated `--token T` prints; the flag still works."""
    print("%s: --token is deprecated: the token is on the command line, where every"
          " local user can read it; use --token-file PATH (- for stdin)" % prog,
          file=sys.stderr)


def _client_token(args, environ=None):
    """send's and recall's token: --token-file, else the deprecated --token,
    else HIVE_TOKEN. ValueError when none gives one."""
    if args.token_file is not None:
        return read_token_file(args.token_file)
    if args.token is not None:
        warn_token_flag("cousin-hive")
        return args.token
    token = (os.environ if environ is None else environ).get(TOKEN_ENV, "").strip()
    if not token:
        raise ValueError("no token: give --token-file PATH (- for stdin) or set %s" % TOKEN_ENV)
    return token


def _fmt_age(seconds):
    if seconds is None:
        return "never"
    seconds = int(max(0, seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return "%d%s ago" % (seconds // size, unit)
    return "%ds ago" % seconds


def _node_state(row, checkin_seconds, now):
    if row["revoked"]:
        return "revoked"
    if not row["checked_in"]:
        return "built, not checked in"
    return "online" if is_online(row["last_seen"], checkin_seconds,
                                 now=now) else "offline"


def hive_main(argv=None):
    """Console entry point. `mint`, `serve`, `revoke`, `forget`, `nodes`
    and `import-legacy` are operator acts on the queen machine;
    `send`/`recall` are node-side client calls.

    Enrolling a node is deliberately manual (mint here, move the token
    to the node) - a framework that pushed itself onto another machine
    would be a larger trust surface than the hive needs."""
    import argparse

    from cousin_lib.config import MissingConfigError

    parser = argparse.ArgumentParser(prog="cousin-hive")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint")
    m.add_argument("slug")
    m.add_argument("--scope", default="own,shared")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8101)
    s.add_argument("--checkin-seconds", type=int, default=None)
    r = sub.add_parser("revoke", help="revoke a node's token (401 from now)")
    r.add_argument("slug")
    f = sub.add_parser("forget", help="drop a revoked node's rows")
    f.add_argument("slug")
    sub.add_parser("nodes", help="list the nodes the queen knows")
    il = sub.add_parser("import-legacy",
                        help="import the previous framework's queen")
    il.add_argument("--tokens", required=True,
                    help="the old queen's tokens.json")
    il.add_argument("--memory-dir",
                    help="the old queen's store directory holding"
                         " <slug>/memory.jsonl")
    il.add_argument("--shared-slugs", default="",
                    help="comma-separated slugs whose memory was the old"
                         " shared corpus (imported in shared scope)")
    for name in ("send", "recall"):
        p = sub.add_parser(name)
        p.add_argument("--queen", required=True)
        given = p.add_mutually_exclusive_group()
        given.add_argument("--token-file", metavar="PATH",
                           help="read the token from PATH (- for stdin); without"
                                " it, %s from the environment" % TOKEN_ENV)
        given.add_argument("--token",
                           help="deprecated: the token on the command line, where"
                                " every local user can read it")
        if name == "send":
            p.add_argument("--to", required=True)
            p.add_argument("--id", required=True)
            p.add_argument("body")
        else:
            p.add_argument("query")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "mint":
            with contextlib.closing(_store_from_env()) as store:
                token = store.mint_token(args.slug, scope=args.scope.split(","))
            print(token)
            return 0
        if args.cmd == "revoke":
            with contextlib.closing(_store_from_env()) as store:
                count = store.revoke(args.slug)
            if not count:
                print("cousin-hive: %s has no live token" % args.slug,
                      file=sys.stderr)
                return 1
            print("revoked %s (%d token%s); it now gets 401"
                  % (args.slug, count, "" if count == 1 else "s"))
            return 0
        if args.cmd == "forget":
            try:
                with contextlib.closing(_store_from_env()) as store:
                    out = store.forget(args.slug)
            except ValueError as err:
                print("cousin-hive: %s" % err, file=sys.stderr)
                return 1
            print("forgot %s (%d token row%s, %d node row%s)"
                  % (args.slug, out["tokens"],
                     "" if out["tokens"] == 1 else "s", out["nodes"],
                     "" if out["nodes"] == 1 else "s"))
            return 0
        if args.cmd == "nodes":
            root = _root_from_env()
            try:
                cfg = hive_config(root)
            except HiveConfigError:
                cfg = None
            period = (cfg or {}).get("checkin_seconds",
                                     DEFAULT_CHECKIN_SECONDS)
            now = time.time()
            with contextlib.closing(HiveStore(root / "shared" / "hive")) as store:
                rows = store.nodes()
            if not rows:
                print("no nodes")
                return 0
            for row in rows:
                where = ("%s:%s" % (row["host"], row["port"])
                         if row["checked_in"] else "-")
                seen = (_fmt_age(now - row["last_seen"])
                        if row["last_seen"] else "never")
                print("%-16s %-22s %-24s last seen %s"
                      % (row["slug"], _node_state(row, period, now), where,
                         seen))
            return 0
        if args.cmd == "import-legacy":
            shared = [x for x in args.shared_slugs.split(",") if x]
            with contextlib.closing(_store_from_env()) as store:
                report = import_legacy(store, args.tokens, args.memory_dir,
                                       shared_slugs=shared)
            print("tokens: %d added, %d already present"
                  % (report["tokens_added"], report["tokens_present"]))
            if report["token_conflicts"]:
                print("tokens NOT imported (the string maps to another"
                      " slug here): %s" % ", ".join(report["token_conflicts"]))
            if report["tokens_skipped"]:
                print("unusable entries skipped: %s"
                      % ", ".join(report["tokens_skipped"]))
            for slug, dropped in sorted(report["scope_dropped"].items()):
                print("%s: unknown scope words dropped: %s"
                      % (slug, ", ".join(map(str, dropped))))
            if args.memory_dir:
                print("memory: %d added, %d already present, %d unusable"
                      % (report["memory_added"], report["memory_present"],
                         report["memory_skipped"]))
            return 0
        if args.cmd == "serve":
            root = _root_from_env()
            try:
                cfg = hive_config(root)
            except HiveConfigError as err:
                print("cousin-hive: %s" % err, file=sys.stderr)
                return 2
            period = args.checkin_seconds or (cfg or {}).get(
                "checkin_seconds", DEFAULT_CHECKIN_SECONDS)
            queen = build_queen(
                HiveStore(root / "shared" / "hive"), host=args.host,
                port=args.port, checkin_seconds=period,
                embedder=EmbedderSource(root), min_score=None)
            print("cousin-hive: queen on %s:%d" % (args.host, queen.port),
                  flush=True)
            try:
                queen.httpd.serve_forever()
            except KeyboardInterrupt:
                queen.stop()
            return 0
    except MissingConfigError as err:
        print("cousin-hive: %s" % err, file=sys.stderr)
        return 2
    except HiveError as err:
        print("cousin-hive: %s" % err, file=sys.stderr)
        return 1
    try:
        token = _client_token(args)
    except ValueError as err:
        print("cousin-hive: %s" % err, file=sys.stderr)
        return 2
    try:
        if args.cmd == "send":
            hive_send(queen_url=args.queen, token=token,
                      to=args.to, body=args.body, msg_id=args.id)
            print("sent")
        else:
            for text in hive_recall(queen_url=args.queen,
                                    token=token, query=args.query):
                print("- " + text)
    except HiveError as err:
        print("cousin-hive: %s" % err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - the -m launcher
    sys.exit(hive_main())
