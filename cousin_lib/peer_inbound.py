"""The one gate for a message from outside this install's process tree
(phase 10a, one inbound surface): a hive node's tell-home
(`POST /hive/tell-home`, console/hive) and an external peer's send
(`POST /peer/send`, console/peer_routes). Both routes authenticate a
bearer token first and hand `accept` the identity it resolved to; the
body never names its sender.

What `accept` enforces, for every caller:
- the message carries an id (`msg_id`, 8-128 of [A-Za-z0-9_-]) and a send
  time (`sent_at`, epoch seconds) within WINDOW_S of this host's clock;
- an (identity, msg_id) pair is delivered at most once: the id is kept for
  SEEN_KEEP_S (longer than the window, so a replay is either stale or
  seen); a delivery that fails frees it, so the sender may retry;
- at most RATE_PER_MIN messages per identity in any minute, at most
  MAX_MESSAGE characters each, never empty;
- the destination is one the route allows (`allowed(slug)`: a hive node
  reaches its home cousin only, an external peer its `reach`), checked
  BEFORE the cousin is looked up, and a local, peer-visible cousin (the
  gate `cousin-chat` applies); anything else answers 404, the same as a
  cousin that does not exist, so a sender cannot map the install;
- the display name is the one the operator configured for that sender
  (the route passes it), and it must be a plain name ([A-Za-z0-9 ._-],
  1-64) that is neither the target's operator nor a local cousin's slug or
  name: a sender is never shown, threaded or treated as either (ruling
  P10a-1);
- the message loses its control characters (tab and newline kept): a
  tmux cousin would take them as keystrokes (review I5);
- delivery is chat.deliver_to: in-process for a runner cousin, through
  its chat server for a tmux one, under the display name. A refusal or a
  connection error frees the id (the sender may retry), a timeout keeps
  it (the message may have landed); both answer the sender (502, 504)
  and are logged, never raised into the route.

The seen store is `<root>/data/inbound-seen.db`, mode 0600."""
import math
import os
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path

WINDOW_S = 300.0          # how far a send time may be from this host's clock
SEEN_KEEP_S = 900.0       # how long a delivered id is remembered (> 2 x WINDOW_S)
RATE_PER_MIN = 30         # messages per identity in any 60 s
MAX_MESSAGE = 16000       # characters in one message
_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# a plain name: letters, digits, space, dot, underscore, hyphen; no space at
# either end (a look-alike of the operator's name is still refused below)
_DISPLAY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,62}[A-Za-z0-9._-])?$")


class Refused(Exception):
    """A message the gate turns away: `status` is the HTTP answer."""

    def __init__(self, status, error):
        super().__init__(error)
        self.status, self.error = status, error


def seen_path(root):
    return Path(root) / "data" / "inbound-seen.db"


def _db(root):
    path = seen_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (identity TEXT NOT NULL, msg_id TEXT NOT NULL,"
                 " at REAL NOT NULL, PRIMARY KEY (identity, msg_id))")
    return conn


def _target(root, to, allowed):
    from cousin_lib.config import CousinConfig, MissingConfigError
    import tomllib
    if not isinstance(to, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", to):
        raise Refused(400, "to must name a cousin")
    absent = Refused(404, "no cousin %r here" % to)
    if not allowed(to):
        raise absent                                    # outside the route's reach: absent
    try:
        target = CousinConfig.load(Path(root) / "cousins" / to)
    except (MissingConfigError, OSError, tomllib.TOMLDecodeError):
        raise absent
    if not target.peer_visible:
        raise absent                                    # not peer-visible: absent, as in cousin-chat
    return target


def check_display(root, target, display):
    """Refuse a display name that is not a plain name, that the framework
    writes itself (delivery.FRAMEWORK_SENDERS: "fw-hook" would be threaded
    on `system` as a hook), or that the target would take for its operator
    or a local cousin (ruling P10a-1)."""
    from cousin_lib.config import FrameworkConfig
    from cousin_lib.delivery import FRAMEWORK_SENDERS
    from cousin_lib.server.storage import is_operator, normalize_chat_user
    if not isinstance(display, str) or not _DISPLAY.match(display):
        raise Refused(403, "the sender's configured name is not a plain name")
    if normalize_chat_user(display) in {normalize_chat_user(n) for n in FRAMEWORK_SENDERS}:
        raise Refused(403, "the sender's configured name is reserved by the framework")
    if is_operator(target, display):
        raise Refused(403, "the sender's configured name is the operator's")
    wanted = normalize_chat_user(display)
    for cousin in FrameworkConfig(root).list_cousins():
        if wanted in (normalize_chat_user(cousin.slug), normalize_chat_user(cousin.name)):
            raise Refused(403, "the sender's configured name is a local cousin's")


def accept(root, *, identity, display, to, message, msg_id, sent_at, allowed, now=None):
    """Deliver one authenticated message or raise Refused. Returns the
    delivery's body ({"ok", "id", ...})."""
    from cousin_lib import chat
    now = time.time() if now is None else now
    if not isinstance(msg_id, str) or not _ID.match(msg_id):
        raise Refused(400, "msg_id must be 8-128 letters, digits, '-' or '_'")
    if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)):
        raise Refused(400, "sent_at must be epoch seconds")
    try:
        sent = float(sent_at)
    except OverflowError:
        raise Refused(400, "sent_at must be epoch seconds")
    if not math.isfinite(sent) or abs(now - sent) > WINDOW_S:
        raise Refused(400, "sent_at is more than %ds from this host's clock" % WINDOW_S)
    if not isinstance(message, str):
        raise Refused(400, "message must be non-empty text")
    from cousin_lib.server.inbound import strip_controls
    message = strip_controls(message)
    if not message.strip():
        raise Refused(400, "message must be non-empty text")
    if len(message) > MAX_MESSAGE:
        raise Refused(400, "message is longer than %d characters" % MAX_MESSAGE)
    target = _target(root, to, allowed)
    check_display(root, target, display)
    conn = _db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM seen WHERE at < ?", (now - SEEN_KEEP_S,))
        recent = conn.execute("SELECT COUNT(*) FROM seen WHERE identity = ? AND at >= ?",
                              (identity, now - 60.0)).fetchone()[0]
        if recent >= RATE_PER_MIN:
            conn.execute("COMMIT")
            raise Refused(429, "more than %d messages a minute from %s" % (RATE_PER_MIN, identity))
        try:
            conn.execute("INSERT INTO seen (identity, msg_id, at) VALUES (?, ?, ?)",
                         (identity, msg_id, now))
        except sqlite3.IntegrityError:
            conn.execute("COMMIT")
            raise Refused(409, "message %s from %s was already delivered" % (msg_id, identity))
        conn.execute("COMMIT")
        try:
            return chat.deliver_to(target, {"user": display, "message": message})
        except (TimeoutError, socket.timeout) as err:
            # it may have landed: keep the id, so a retry is not a duplicate
            print("peer_inbound: delivery of %s from %s to %s timed out: %s"
                  % (msg_id, identity, target.slug, err), file=sys.stderr)
            raise Refused(504, "delivery timed out; the message may have landed")
        except Exception as err:  # noqa: BLE001 - answered as a 502, logged here
            conn.execute("DELETE FROM seen WHERE identity = ? AND msg_id = ?", (identity, msg_id))
            print("peer_inbound: delivery of %s from %s to %s failed: %s: %s"
                  % (msg_id, identity, target.slug, type(err).__name__, err), file=sys.stderr)
            raise Refused(502, "delivery failed; the message was not delivered")
    finally:
        conn.close()
