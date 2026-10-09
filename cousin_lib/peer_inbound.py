"""The one gate for a message from outside this install's process tree
(one inbound surface): a hive node's tell-home
(`POST /hive/tell-home`, console/hive) and an external peer's send
(`POST /peer/send`, console/peer_routes). Both routes authenticate a
bearer token first and hand `accept` the identity it resolved to; the
body never names its sender.

What `accept` enforces, for every caller:
- the message carries an id (`msg_id`, 8-128 of [A-Za-z0-9_-]) and a send
  time (`sent_at`, epoch seconds) within WINDOW_S of this host's clock;
- an (identity, msg_id) pair is delivered at most once: the id is kept for
  SEEN_KEEP_S (longer than the window, so a replay is either stale or
  seen); a delivery that fails frees it, so the sender may retry; an id
  whose delivery never returned (the gate was killed in between) is
  delivered by the retry, under the key `peer:<identity>:<msg_id>`, unless
  the cousin's inbox already holds that key (then it answers 409);
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
  name: a sender is never shown, threaded or treated as either;
- the message loses its control characters (tab and newline kept): a
  tmux cousin would take them as keystrokes;
- delivery is chat.deliver_to: in-process for a runner cousin, refused
  by name for a cousin with no runner kind, under the display name. A
  refusal or a
  connection error frees the id (the sender may retry), and so does a
  message the cousin's inbox did not take (NotDelivered: nothing was
  kept); a timeout keeps it (the message may have landed). Each answers the
  sender (502, 504) and is logged, never raised into the route.

The seen store is `<root>/data/inbound-seen.db`, mode 0600."""
import math
import os
import re
import socket
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

from cousin_lib.crashpoint import crashpoint
from cousin_lib.sqlite_util import add_column

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
    # 0 from the id's record until its delivery returned (or timed out): a
    # gate killed in between leaves 0, and the sender's retry delivers it
    # (under its key) instead of hearing "already delivered" for a message
    # that never landed
    add_column(conn, "seen", "settled", "INTEGER NOT NULL DEFAULT 1")
    return conn


def _key(identity, msg_id):
    return "peer:%s:%s" % (identity, msg_id)


# keys this process is delivering now: an unsettled id in here is in
# flight (a concurrent replay answers 409), one not in here was left by a
# gate that died
_IN_FLIGHT = set()


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


# The Latin letters that are real letters of a language and do not
# decompose to an ASCII base, with what a reader takes each for. Any other
# non-ASCII letter is refused: small capitals, IPA forms, other scripts and
# fullwidth forms all pass for an ASCII letter.
_LATIN_EXTRA = {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
                "ß": "ss", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "þ": "th",
                "Þ": "TH", "ð": "d", "Ð": "D"}


def _landed(target, key):
    """True when the target's inbox holds the row put under `key`: a first
    delivery that landed before its gate died. Unreadable reads as landed:
    a 409 the sender counts delivered beats a second copy."""
    from cousin_lib.runner.inbox import Inbox
    try:
        return Inbox(target.home).keyed_id(key) is not None
    except (OSError, sqlite3.Error):
        return True


def _settle(conn, identity, msg_id):
    conn.execute("UPDATE seen SET settled = 1 WHERE identity = ? AND msg_id = ?",
                 (identity, msg_id))


def _free(conn, identity, msg_id):
    conn.execute("DELETE FROM seen WHERE identity = ? AND msg_id = ?", (identity, msg_id))


def _ascii_base(char):
    """The ASCII letters a letter is, by its canonical decomposition (ò is
    o and a grave) or _LATIN_EXTRA; None for anything else. Canonical
    only: a fullwidth "Ａ" decomposes to "A" only by compatibility, and is
    not one."""
    if char.isascii():
        return char
    if char in _LATIN_EXTRA:
        return _LATIN_EXTRA[char]
    base = "".join(c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c))
    return base if base and base.isascii() and base.isalpha() else None


def _plain(display):
    """A plain name: _DISPLAY's shape, where a letter may also be an ASCII
    letter with diacritics (Totò, Nicolò) or one of _LATIN_EXTRA (Søren,
    Łukasz), compared composed (NFC). Nothing else: a look-alike from
    another script, a small capital or an IPA letter would pass for an
    ASCII one."""
    if not isinstance(display, str):
        return False
    shape = []
    for char in unicodedata.normalize("NFC", display):
        base = _ascii_base(char)
        if base is None:
            return False
        shape.append(base[0] if not char.isascii() else char)
    return bool(_DISPLAY.match("".join(shape)))


def skeleton(name):
    """The name a reader would take it for: _LATIN_EXTRA spelled out, the
    accents dropped, case-folded, spaces as underscores. "Àna" and "Ana"
    are one skeleton, so are "Søren" and "Soren"."""
    spelled = "".join(_LATIN_EXTRA.get(c, c) for c in unicodedata.normalize("NFC", str(name or "")))
    folded = "".join(c for c in unicodedata.normalize("NFKD", spelled) if not unicodedata.combining(c))
    return folded.casefold().replace(" ", "_")


def check_display(root, target, display):
    """Refuse a display name that is not a plain name, that the framework
    writes itself (delivery.FRAMEWORK_SENDERS: "fw-hook" would be threaded
    on `system` as a hook), or that the target would take for its operator
    or a local cousin."""
    from cousin_lib.config import FrameworkConfig
    from cousin_lib.delivery import FRAMEWORK_SENDERS
    from cousin_lib.server.storage import is_operator, normalize_chat_user
    if not _plain(display):
        raise Refused(403, "the sender's configured name is not a plain name")
    # compared by skeleton: an accent is not enough to pass for another name
    wanted = skeleton(display)
    if wanted in {skeleton(n) for n in FRAMEWORK_SENDERS}:
        raise Refused(403, "the sender's configured name is reserved by the framework")
    operator = getattr(target, "operator_name", None)
    if is_operator(target, display) or (operator and wanted == skeleton(operator)):
        raise Refused(403, "the sender's configured name is the operator's")
    for cousin in FrameworkConfig(root).list_cousins():
        if wanted in (skeleton(cousin.slug), skeleton(cousin.name)):
            raise Refused(403, "the sender's configured name is a local cousin's")


def accept(root, *, identity, display, to, message, msg_id, sent_at, allowed, now=None):
    """Deliver one authenticated message or raise Refused. Returns the
    delivery's body ({"ok", "id", ...})."""
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
    key = _key(identity, msg_id)
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
            conn.execute("INSERT INTO seen (identity, msg_id, at, settled) VALUES (?, ?, ?, 0)",
                         (identity, msg_id, now))
        except sqlite3.IntegrityError:
            settled = conn.execute("SELECT settled FROM seen WHERE identity = ? AND msg_id = ?",
                                   (identity, msg_id)).fetchone()[0]
            # unsettled and in no delivery of this process: the gate that
            # recorded it died before its delivery returned. Delivered now,
            # unless the cousin's inbox holds the key (it landed first).
            orphan = not settled and key not in _IN_FLIGHT and not _landed(target, key)
            if not orphan:
                if not settled and key not in _IN_FLIGHT:
                    _settle(conn, identity, msg_id)
                conn.execute("COMMIT")
                raise Refused(409, "message %s from %s was already delivered" % (msg_id, identity))
        _IN_FLIGHT.add(key)         # under the write lock: a replay now reads it in flight
        conn.execute("COMMIT")
        try:
            crashpoint("peer.seen")
            return _deliver(conn, target, identity, display, to, message, msg_id, key)
        finally:
            _IN_FLIGHT.discard(key)
    finally:
        conn.close()


def _deliver(conn, target, identity, display, to, message, msg_id, key):
    """The delivery of one recorded id, under its key; the id is settled
    (kept) when it landed or may have, freed when nothing was kept."""
    from cousin_lib import chat, delivery
    from cousin_lib.server import chat_api
    try:
        with delivery.keyed(key):
            out = chat.deliver_to(target, {"user": display, "message": message})
        _settle(conn, identity, msg_id)
        return out
    except (TimeoutError, socket.timeout) as err:
        # it may have landed: keep the id, so a retry is not a duplicate
        _settle(conn, identity, msg_id)
        print("peer_inbound: delivery of %s from %s to %s timed out: %s"
              % (msg_id, identity, target.slug, err), file=sys.stderr)
        raise Refused(504, "delivery timed out; the message may have landed")
    except chat.DeliveryRefused as err:
        # the cousin has no runner kind: absent, as to anyone outside,
        # and final (a 5xx would have the sender retry for nothing)
        _free(conn, identity, msg_id)
        print("peer_inbound: delivery of %s from %s to %s refused: %s"
              % (msg_id, identity, target.slug, err), file=sys.stderr)
        raise Refused(404, "no cousin %r here" % to)
    except chat_api.NotDelivered as err:
        # nothing was kept: free the id so the sender's retry delivers it
        _free(conn, identity, msg_id)
        print("peer_inbound: delivery of %s from %s to %s: %s"
              % (msg_id, identity, target.slug, err), file=sys.stderr)
        raise Refused(502, "not delivered: the cousin's inbox did not take it; retry")
    except Exception as err:  # noqa: BLE001 - answered as a 502, logged here
        _free(conn, identity, msg_id)
        print("peer_inbound: delivery of %s from %s to %s failed: %s: %s"
              % (msg_id, identity, target.slug, type(err).__name__, err), file=sys.stderr)
        raise Refused(502, "delivery failed; the message was not delivered")
