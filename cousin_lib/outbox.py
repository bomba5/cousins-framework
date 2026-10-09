"""The outbox: a message to an external peer that could not be confirmed
is kept and sent again, under the same message id, until the peer takes
it or the retry window closes.

Only a signed peer (one with a `token_file`, whose console takes
`POST /peer/send`) is retried: its gate, peer_inbound, delivers an
(identity, msg_id) pair at most once and remembers the id for
SEEN_KEEP_S (900 s), so a retry of a message that did land answers 409
and is counted delivered, never shown twice. Every retry is signed again
with a fresh `sent_at` (the gate's window is 300 s) and the SAME
`msg_id`. A legacy peer (no token_file) has no id to dedup by and is
never retried.

What counts as what, for one attempt:
- 2xx, or 409 (already delivered): delivered;
- 429, 5xx, a connection error or a timeout: transient, retried;
- any other 4xx (a bad signature, an unknown cousin, a refused name):
  permanent, given up at once.

The retry schedule is BACKOFF_S after the first attempt (each wait at
least that: retries ride the loops tick), and nothing is sent after
DEADLINE_S from the moment the first attempt started (inside SEEN_KEEP_S,
so a late retry can never land twice; one send takes at most ~10 s). The loops daemon drains the outbox on every tick. The
sending cousin hears the end either way: a `system` item from
`framework`, delivered after a retry or given up with the last error.

The store is `<root>/data/outbox.db`, mode 0600."""
import os
import socket
import sqlite3
import time
import urllib.error
from pathlib import Path

from cousin_lib.crashpoint import crashpoint

BACKOFF_S = (15, 30, 60, 120, 240, 300)   # waits between attempts, the last one repeating
DEADLINE_S = 840.0                          # 14 min: inside the gate's 900 s memory of an id
PASS_BUDGET_S = 20.0                        # a drain pass leaves the rest for the next tick
PENDING, DELIVERED, GAVE_UP = "pending", "delivered", "gave_up"
DELIVERED_NOW, QUEUED_FOR_RETRY, PERMANENT = "delivered", "queued", "permanent"


def store_path(root):
    return Path(root) / "data" / "outbox.db"


def _db(root):
    path = store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " msg_id TEXT NOT NULL UNIQUE,"
        " sender TEXT NOT NULL,"          # the sending cousin's slug
        " dest TEXT NOT NULL,"            # the external peer's slug
        " message TEXT NOT NULL,"
        " created REAL NOT NULL,"         # the first attempt
        " attempts INTEGER NOT NULL,"
        " next_at REAL,"
        " state TEXT NOT NULL,"
        " last_error TEXT,"
        " finished REAL)")
    return conn


def classify(err):
    """The outcome class of one failed attempt: QUEUED_FOR_RETRY for what
    may pass later (a request that went out but whose answer broke off,
    http.client's IncompleteRead and kin, included: the same-id retry
    settles it), PERMANENT for what will not."""
    import http.client
    if isinstance(err, urllib.error.HTTPError):
        if err.code == 409:
            return DELIVERED_NOW
        if err.code == 429 or err.code >= 500:
            return QUEUED_FOR_RETRY
        return PERMANENT
    if isinstance(err, (urllib.error.URLError, TimeoutError, socket.timeout,
                        ConnectionError, http.client.HTTPException)):
        return QUEUED_FOR_RETRY
    return PERMANENT


def describe(err):
    if isinstance(err, urllib.error.HTTPError):
        return "HTTP %d %s" % (err.code, err.reason)
    return "%s: %s" % (type(err).__name__, err)


def _delay(attempts):
    return BACKOFF_S[min(max(attempts, 1), len(BACKOFF_S)) - 1]


def enqueue(root, *, msg_id, sender, dest, message, error, created=None, now=None):
    """Keep a message whose first attempt was transient; returns its row
    as a dict. `created` is when that first attempt STARTED (the peer may
    have recorded the id from then on): the window is counted from it."""
    now = time.time() if now is None else now
    created = now if created is None else created
    conn = _db(root)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO outbox (msg_id, sender, dest, message, created,"
            " attempts, next_at, state, last_error) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (msg_id, sender, dest, message, created, now + _delay(1), PENDING, error))
        return dict(conn.execute("SELECT * FROM outbox WHERE msg_id = ?", (msg_id,)).fetchone())
    finally:
        conn.close()


def list_rows(root, *, state=None, limit=100):
    """Rows newest first; `state` filters (pending, delivered, gave_up)."""
    if not store_path(root).exists():
        return []
    conn = _db(root)
    try:
        if state:
            rows = conn.execute("SELECT * FROM outbox WHERE state = ? ORDER BY id DESC LIMIT ?",
                                (state, limit))
        else:
            rows = conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _finish(conn, row, state, error, now):
    """Close a pending row; the number of rows it changed (0 when another
    pass closed it first, and then nobody is told twice)."""
    return conn.execute("UPDATE outbox SET state = ?, last_error = ?, finished = ?, next_at = NULL"
                        " WHERE id = ? AND state = ?", (state, error, now, row["id"], PENDING)).rowcount


def _tell_sender(root, row, text):
    """A `system` item to the sending cousin: how its message ended."""
    from cousin_lib import delivery
    home = Path(root) / "cousins" / row["sender"]
    if not (home / "cousin.toml").exists():
        return
    item = delivery.Item(thread_id=delivery.thread_id("system"), source="outbox",
                         sender="framework", body=text)
    delivery.deliver(home, item, wait=False)


def _gave_up_text(row, error):
    minutes = max(1, round(((row.get("finished") or time.time()) - row["created"]) / 60))
    return ("[fw-outbox] Your message to %s (msg_id %s) was NOT delivered: %d attempts"
            " over %d min, last error: %s. It was: %s"
            % (row["dest"], row["msg_id"], row["attempts"], minutes, error,
               _excerpt(row["message"])))


def _excerpt(text, limit=200):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def drain(root, *, send=None, now=None, budget_s=None):
    """One pass over the due rows: send each again, finish it delivered,
    given up (a permanent answer, the deadline passed, the peer gone from
    config/external-peers.toml), or due again later. `send(peer, message,
    msg_id)` is the attempt (chat.post_signed by default). Returns
    {"delivered": [...], "gave_up": [...], "retrying": [...]} of msg_ids.

    Time is read afresh for every row (a slow send must not let the next
    one go out past the deadline on a stale clock); `now` pins it, for a
    test. A pass stops after `budget_s` (PASS_BUDGET_S) and leaves the
    rest due, so a backlog never holds the loops tick; and after one
    transient failure to a peer it sends that peer nothing more this
    pass."""
    from cousin_lib import chat
    clock = (lambda: now) if now is not None else time.time
    budget = PASS_BUDGET_S if budget_s is None else budget_s
    out = {"delivered": [], "gave_up": [], "retrying": []}
    if not store_path(root).exists():
        return out
    send = send or (lambda peer, message, msg_id: chat.post_signed(root, peer, message, msg_id))
    peers = chat.load_external_peers(root)
    conn = _db(root)
    started = clock()
    failing = set()

    def give_up(row, error, at):
        if _finish(conn, row, GAVE_UP, error, at):
            out["gave_up"].append(row["msg_id"])
            _tell_sender(root, dict(row, finished=at), _gave_up_text(row, error))

    try:
        due = [dict(r) for r in conn.execute(
            "SELECT * FROM outbox WHERE state = ? AND next_at <= ? ORDER BY id",
            (PENDING, started))]
        for row in due:
            at = clock()
            if at - started > budget:
                break                                   # the rest stay due for the next tick
            if row["dest"] in failing:
                continue                                # this pass already found it down
            if at - row["created"] > DEADLINE_S:
                give_up(row, row["last_error"] or "the retry window closed", at)
                continue
            peer = peers.get(row["dest"])
            if peer is None or not peer.token_file:
                give_up(row, "peer %s is no longer a signed external peer" % row["dest"], at)
                continue
            attempts = row["attempts"] + 1
            try:
                send(peer, row["message"], row["msg_id"])
                crashpoint("outbox.sent")
                outcome, error = DELIVERED_NOW, None
            except Exception as err:  # noqa: BLE001 - classified, never raised out of a tick
                outcome, error = classify(err), describe(err)
            at = clock()
            conn.execute("UPDATE outbox SET attempts = ?, last_error = ? WHERE id = ?",
                         (attempts, error, row["id"]))
            row = dict(row, attempts=attempts)
            if outcome == DELIVERED_NOW:
                if _finish(conn, row, DELIVERED, error, at):
                    out["delivered"].append(row["msg_id"])
                    how = ("on attempt %d" % attempts if error is None else
                           "earlier: on attempt %d the peer answered it already had this"
                           " message id" % attempts)
                    _tell_sender(root, row, "[fw-outbox] Your message to %s (msg_id %s) was"
                                 " delivered %s. It was: %s"
                                 % (row["dest"], row["msg_id"], how, _excerpt(row["message"])))
            elif outcome == PERMANENT or at + _delay(attempts) - row["created"] > DEADLINE_S:
                give_up(row, error, at)
            else:
                failing.add(row["dest"])
                conn.execute("UPDATE outbox SET next_at = ? WHERE id = ?",
                             (at + _delay(attempts), row["id"]))
                out["retrying"].append(row["msg_id"])
        return out
    finally:
        conn.close()
