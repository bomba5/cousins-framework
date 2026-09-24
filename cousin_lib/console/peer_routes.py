"""The external peers' door (phase 10a, one inbound surface): another
install's cousin sends to one of ours with `POST /peer/send`.

A third authentication world beside the two in console/hive.py:
- /api/* is the operator's (cookie session, network guard);
- /hive/* is the hive nodes' (their bearer token, no guard);
- /peer/* is the external peers': behind the network guard (they sit on
  the ranges config/net-allowlist.json allows), and a signature per
  message (ruling P10a-2): `Authorization: HMAC <sender>:<hex>`, where
  <sender> names an entry of config/external-peers.toml and <hex> is
  chat.peer_signature over (sender, to, sent_at, msg_id, the message's
  sha256) keyed with that entry's `inbound_token_file` (a 0600 file: the
  secret the two installs share). The secret never crosses the wire, so a
  captured request yields no credential; it cannot be altered, and it
  cannot be replayed after the window, nor within it (the dedupe).
No route here reads a session cookie or a bearer token, and no /api/ or
/hive/ route reads a peer signature: a credential is worth nothing outside
its own world.

Until the signature verifies, every refusal is the same `401
unauthorized` (review round 2, N2): an unknown sender, a wrong signature,
a malformed body, an unusable config/external-peers.toml and a secret
file that is unreadable or readable by others look alike, so a caller
cannot tell which peers are configured. An authenticated entry is usable
only when it names a `reach` (ruling P10a-3: the local cousins it may
write to; there is no default-all) and its slug is not a local cousin's
(review I1); an unusable one is answered with a generic 503. Either way
the detail is logged here, and nothing about peers, paths or file modes
goes to the wire (review I4). The message itself goes through peer_inbound.accept (the
window, the dedupe, the rate, the size, a plain name that is never the
operator's or a cousin's, and a 404 for anything outside the reach,
identical to a cousin that does not exist).

    POST /peer/send  {"to", "message", "msg_id", "sent_at"}
    200 {"ok": true, "to", "id"} | 400 | 401 | 403 | 404 | 409 | 413 | 429 | 502 | 503 | 504
"""
import hmac
import json
import re
import sys

from cousin_lib import peer_inbound

MAX_BODY_BYTES = 64 * 1024
UNAVAILABLE = (503, {"error": "external peers unavailable"})
UNAUTHORIZED = (401, {"error": "unauthorized"})
_HEADER = re.compile(r"^HMAC ([A-Za-z0-9][A-Za-z0-9._-]{0,63}):([0-9a-f]{64})$")


def is_peer_path(path):
    return path == "/peer" or path.startswith("/peer/")


def _log(message):
    """The detail of a refusal, for the operator: stderr (the console's
    log), never the response."""
    print("cousin-console /peer/: %s" % message, file=sys.stderr)


def handle(root, method, path, headers, raw_body):
    """One /peer/ request -> (status, payload)."""
    from cousin_lib import chat
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    if (method, path.rstrip("/")) != ("POST", "/peer/send"):
        return 404, {"error": "not found"}
    m = _HEADER.match((headers.get("Authorization") or "").strip() if headers else "")
    if m is None:
        return UNAUTHORIZED
    sender, signature = m.groups()
    try:
        peer = chat.load_external_peers(root).get(sender)
    except MissingConfigError as err:
        _log("config/external-peers.toml is unusable: %s" % err)
        return UNAUTHORIZED
    if peer is None or not peer.inbound_token_file:
        return UNAUTHORIZED
    try:
        secret = chat.read_secret(root, peer.inbound_token_file,
                                  "external peer %s inbound_token_file" % peer.slug)
    except MissingConfigError as err:
        _log(str(err))
        return UNAUTHORIZED
    try:
        body = json.loads(raw_body) if raw_body and raw_body.strip() else {}
    except ValueError:
        return UNAUTHORIZED
    if not isinstance(body, dict):
        return UNAUTHORIZED
    to, message = body.get("to"), body.get("message")
    msg_id, sent_at = body.get("msg_id"), body.get("sent_at")
    try:
        expected = chat.peer_signature(secret, peer.slug, str(to), sent_at, str(msg_id),
                                       message if isinstance(message, str) else "")
    except (TypeError, ValueError, OverflowError):
        return UNAUTHORIZED
    if not hmac.compare_digest(expected, signature):
        return UNAUTHORIZED
    local = {c.slug for c in FrameworkConfig(root).list_cousins()}
    if peer.slug in local:
        _log("external peer %s shares its slug with a local cousin; refused" % peer.slug)
        return UNAVAILABLE
    if not peer.reach:
        _log("external peer %s names no reach; refused (reach is required)" % peer.slug)
        return UNAVAILABLE
    reach = set(peer.reach)
    try:
        out = peer_inbound.accept(
            root, identity="peer:%s" % peer.slug, display=peer.name or peer.slug,
            to=to, message=message, msg_id=msg_id, sent_at=sent_at,
            allowed=lambda slug: slug in reach)
    except peer_inbound.Refused as err:
        return err.status, {"error": err.error}
    return 200, {"ok": True, "to": to, "id": out.get("id")}


def serve(handler, method, path, raw_body):
    status, payload = handle(handler.console.root, method, path, handler.headers, raw_body)
    handler.send_json(status, payload)
