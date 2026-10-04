"""Cousin-to-cousin chat: send a message to a peer.

Targets resolve from the filesystem registry, so peer chat works with no
other service running. A local peer on a runner kind (`[agent] runner`)
is written directly, in this process: its chat store and its inbox,
through server/chat_api.send (no cousin runs a chat server of its own).
A local cousin with no runner kind is refused by name
(DeliveryRefused, delivery.lane_refusal): nothing is sent or stored.
The peer-visibility gate is bidirectional: a
cousin marked not peer-visible is absent from peer lists and sees no
peers itself, because isolation that depends on the isolated party not
looking is not isolation. Operator surfaces do not use this gate.

Outbound text passes the per-surface filter before anything touches the
wire; a blocked message is never partially sent.

External peers: a cousin that runs on another framework instance (not
under <root>/cousins/) is reachable when config/external-peers.toml
names its chat server's base URL and send route. Its address must pass
the install's network guard (loopback and the private ranges, extended
by config/net-allowlist.json), the request goes direct (no proxy, no
redirect), and a slug in neither place is still an error.
"""
import argparse
import json
import socket
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.outbound_filter import FilterBlocked, OutboundPolicy
from cousin_lib.trace import traced_cli

EXTERNAL_PEERS_RELPATH = ("config", "external-peers.toml")
DEFAULT_SEND_PATH = "/api/send"
PEER_SEND_PATH = "/peer/send"


def peer_signature(secret, sender, to, sent_at, msg_id, message):
    """HMAC-SHA256, hex, of one /peer/send message with the secret the two
    installs share: over the sender's name, the target,
    the send time to the millisecond, the id and the message's sha256, so
    a captured request can be neither altered nor sent from anyone else,
    and the secret itself never crosses the wire."""
    import hashlib
    import hmac
    canonical = "\n".join([sender, to, "%.3f" % float(sent_at), msg_id,
                            hashlib.sha256(message.encode("utf-8")).hexdigest()])
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"),
                    hashlib.sha256).hexdigest()


class NoContextError(Exception):
    """The send cannot be attributed or routed; refuse rather than guess."""


class PeerAddressRefused(ValueError):
    """An external peer's address is outside the network guard."""


class SenderRefused(ValueError):
    """The name a send would be shown under is not the sender's own."""


def check_sender_name(sender, target, display_name, *, external=False):
    """The name a peer message is shown under: the sending cousin's own
    `name` unless `--from` gives another spelling of the same cousin (its
    slug or its name, case- and space-insensitive). To an external peer
    (`external`) `--from` may still be a free-form display name ("Wren of
    testbed"): the receiving install runs peer_inbound.check_display on
    it, and nothing here acts on it. Anything else is
    refused, the same names peer_inbound.check_display keeps from an
    outside sender: a sender is never shown, threaded or treated as the
    target's operator, another cousin or the framework (an operator's
    name would reach the operator-only paths, such as correction capture
    and the login-code divert). Returns the name to show."""
    from cousin_lib.delivery import FRAMEWORK_SENDERS
    from cousin_lib.peer_inbound import _DISPLAY
    from cousin_lib.server.storage import is_operator, normalize_chat_user
    shown = display_name or sender.name or sender.slug
    if display_name:
        own = {normalize_chat_user(sender.slug),
               normalize_chat_user(sender.name or sender.slug)}
        if not isinstance(display_name, str) \
                or not _DISPLAY.match(display_name) \
                or (not external and normalize_chat_user(display_name) not in own):
            raise SenderRefused(
                "--from %r refused: a cousin sends under its own name or"
                " slug only (%s)" % (display_name, sender.slug))
    if normalize_chat_user(shown) in {normalize_chat_user(n)
                                      for n in FRAMEWORK_SENDERS}:
        raise SenderRefused(
            "sender name %r refused: it is reserved by the framework" % shown)
    if is_operator(target, shown):
        raise SenderRefused(
            "sender name %r refused: it is the target's operator" % shown)
    return shown


class DeliveryRefused(Exception):
    """A local cousin with no runner kind: 2.0.0 has no transport to it.
    The message is delivery.lane_refusal's line."""


@dataclass(frozen=True)
class ExternalPeer:
    """One entry of config/external-peers.toml. Outbound: `url` +
    `send_path`, and, for a peer whose console takes POST /peer/send,
    `token_file` (the secret that peer's install shares with this one) and
    `sender` (the name that peer knows this install by): each message is
    signed with the secret, which never travels. Inbound:
    `inbound_token_file`, the secret shared with that peer,
    which our POST /peer/send checks its signatures with; `name`, how its
    messages are shown here; `reach`, the local cousins it may write to,
    required (no reach, no entry)."""
    slug: str
    url: str
    send_path: str = DEFAULT_SEND_PATH
    token_file: str = ""
    inbound_token_file: str = ""
    name: str = ""
    sender: str = ""
    reach: tuple = ()

    @property
    def send_url(self):
        return self.url.rstrip("/") + self.send_path


def load_external_peers(root):
    """config/external-peers.toml as {slug: ExternalPeer}. Absent: {}.
    Unparsable, or a peer without a usable http(s) url or with a
    send_path that is not an absolute path: loud (MissingConfigError),
    because the file promised a delivery route."""
    path = FrameworkConfig(root).root.joinpath(*EXTERNAL_PEERS_RELPATH)
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise MissingConfigError("%s is unusable: %s" % (path, err))
    peers = data.get("peers", {})
    if not isinstance(peers, dict):
        raise MissingConfigError("%s: [peers] must be a table" % path)
    out = {}
    for slug, entry in peers.items():
        where = "%s [peers.%s]" % (path, slug)
        if not isinstance(entry, dict):
            raise MissingConfigError("%s must be a table" % where)
        url = entry.get("url")
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme not in ("http", "https")
                or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise MissingConfigError(
                "%s url must be an http(s) base URL such as"
                " http://127.0.0.1:8085, got %r" % (where, url))
        # a peer we sign for takes /peer/send, not the legacy chat route
        send_path = entry.get("send_path", PEER_SEND_PATH if entry.get("token_file")
                              else DEFAULT_SEND_PATH)
        if not isinstance(send_path, str) or not send_path.startswith("/"):
            raise MissingConfigError(
                "%s send_path must start with '/', got %r"
                % (where, send_path))
        extra = {}
        for key in ("token_file", "inbound_token_file", "name", "sender"):
            value = entry.get(key, "")
            if not isinstance(value, str):
                raise MissingConfigError("%s %s must be a string" % (where, key))
            extra[key] = value
        reach = entry.get("reach", [])
        if not isinstance(reach, list) or not all(isinstance(r, str) for r in reach):
            raise MissingConfigError("%s reach must be a list of slugs" % where)
        out[slug] = ExternalPeer(slug=slug, url=url, send_path=send_path,
                                 reach=tuple(reach), **extra)
    return out


def _external_peers(fw):
    """External peers minus any slug the local registry already has: a
    local cousin always wins, and the shadowed entry is reported."""
    peers = load_external_peers(fw.root)
    local = {c.slug for c in fw.list_cousins()}
    for slug in sorted(set(peers) & local):
        print("cousin-chat: config/external-peers.toml names %r, which is"
              " a local cousin; using the local one" % slug,
              file=sys.stderr)
        del peers[slug]
    return peers


def check_peer_address(peer, guard):
    """Every address the peer's host resolves to must pass the guard;
    one outside it refuses the send (a name that resolves both inside
    and outside is not trusted)."""
    parsed = urllib.parse.urlsplit(peer.url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port,
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise PeerAddressRefused(
            "external peer %r: cannot resolve %s: %s"
            % (peer.slug, parsed.hostname, err))
    addresses = sorted({info[4][0] for info in infos})
    refused = [a for a in addresses if not guard(a)]
    if not addresses or refused:
        raise PeerAddressRefused(
            "external peer %r: %s resolves to %s, outside the network"
            " guard (loopback and private ranges; config/net-allowlist.json"
            " adds more)" % (peer.slug, parsed.hostname,
                            ", ".join(refused or addresses) or "nothing"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect is an error: it would leave the guard


def read_secret(root, rel, what):
    """A secret file under the install: a single line, owned by us and
    readable by nobody else (mode 0600 or tighter), or MissingConfigError."""
    path = FrameworkConfig(root).root / rel
    try:
        st = path.stat()
        if st.st_mode & 0o077:
            raise MissingConfigError("%s %s is readable by group or others: chmod 600 it"
                                     % (what, path))
        value = path.read_text().strip()
    except OSError as err:
        raise MissingConfigError("%s %s is unreadable: %s" % (what, path, err))
    if not value:
        raise MissingConfigError("%s %s is empty" % (what, path))
    return value


def _post_external(peer, payload, guard, *, root=None):
    """One message to an external peer. With a `token_file` (a peer whose
    console takes POST /peer/send) the message is signed with it
    (`Authorization: HMAC <sender>:<hex>`, peer_signature; the secret never
    travels) and the body carries what that route needs: `to`, a `msg_id`
    and a `sent_at` (its replay window). Without one, the legacy body,
    {user, message}, to the peer's chat server."""
    import time
    import uuid
    check_peer_address(peer, guard)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect())
    headers = {"Content-Type": "application/json"}
    if peer.token_file:
        if not peer.sender:
            raise MissingConfigError("external peer %s: token_file needs sender, the name that"
                                     " peer knows this install by" % peer.slug)
        secret = read_secret(root, peer.token_file, "external peer %s token_file" % peer.slug)
        payload = {"to": peer.slug, "message": payload["message"], "msg_id": uuid.uuid4().hex,
                   "sent_at": round(time.time(), 3)}
        headers["Authorization"] = "HMAC %s:%s" % (peer.sender, peer_signature(
            secret, peer.sender, peer.slug, payload["sent_at"], payload["msg_id"],
            payload["message"]))
    req = urllib.request.Request(
        peer.send_url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with opener.open(req, timeout=5) as r:
        body = r.read()
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {}


def login_marker(home):
    """" LOGIN REQUIRED (account <name> on <host>)" or " BILLING (...)"
    while the cousin's runner waits on data/login-required.json, else ""."""
    from cousin_lib.runner import auth
    data = auth.read_login_required(home)
    if not data:
        return ""
    label = "BILLING" if data.get("reason") == auth.BILLING else "LOGIN REQUIRED"
    return " %s (account %s on %s)" % (label, data.get("account"), data.get("host"))


def list_peers(fw, self_slug):
    rows = [c for c in fw.list_cousins() if c.peer_visible]
    me = next((c for c in fw.list_cousins() if c.slug == self_slug), None)
    if me is not None and not me.peer_visible:
        return []
    return rows


def list_external_peers(fw, self_slug):
    """External peers, under the same bidirectional gate: a cousin that
    is not peer-visible sees none."""
    me = next((c for c in fw.list_cousins() if c.slug == self_slug), None)
    if me is not None and not me.peer_visible:
        return []
    return [p for _slug, p in sorted(_external_peers(fw).items())]


def _resolve(fw, dest_slug):
    for c in fw.list_cousins():
        if c.slug == dest_slug:
            return c
    raise NoContextError("no cousin %r in the registry" % dest_slug)


def deliver_local(target, payload):
    """One message to a local runner-lane cousin, in this process:
    chat_api.send with the inbox delivery. `payload` is /api/send's body
    ({user, message}). Returns its body."""
    from cousin_lib.server import chat_api
    return chat_api.send(target, payload, deliver=chat_api.make_deliver(target))


def is_local_runner(target):
    """True for a local cousin on the runner lane (delivery reads the same
    `[agent] runner`): reached in-process, never over HTTP."""
    from cousin_lib import delivery
    return isinstance(delivery.backend_for(target.home), delivery.InboxBackend)


def send_message(fw, sender, dest_slug, text, policy=None, display_name=None,
                 guard=None):
    if sender is None or not sender.slug:
        raise NoContextError("no sender context; refusing an unattributed send")
    if dest_slug == sender.slug:
        raise ValueError("refusing to send to self (%s)" % dest_slug)
    try:
        target = _resolve(fw, dest_slug)
    except NoContextError:
        external = _external_peers(fw).get(dest_slug)
        if external is None:
            raise NoContextError(
                "no cousin %r in the registry or in"
                " config/external-peers.toml" % dest_slug)
        target = external
    shown = check_sender_name(sender, target, display_name,
                              external=isinstance(target, ExternalPeer))
    if policy is not None:
        policy.check(
            text,
            from_slug=sender.slug,
            dest_slug=dest_slug,
            surface="chat",
            context="chat send",
        )
    payload = {"user": shown, "message": text}
    if isinstance(target, ExternalPeer):
        if guard is None:
            from cousin_lib.server.netguard import NetGuard
            guard = NetGuard.from_config(fw.root)
        return _post_external(target, payload, guard, root=fw.root)
    return deliver_to(target, payload)


def deliver_to(target, payload):
    """One /api/send body to a local cousin, in-process (deliver_local).
    A cousin with no runner kind is DeliveryRefused with
    delivery.lane_refusal's line: nothing is opened or stored."""
    if is_local_runner(target):
        return deliver_local(target, payload)
    from cousin_lib import delivery
    raise DeliveryRefused(delivery.lane_refusal(target.home))


@traced_cli("cousin-chat")
def chat_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-chat")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="post a message to another cousin")
    s.add_argument("slug")
    s.add_argument("text")
    s.add_argument("--from", dest="display_name",
                   help="sender display name: this cousin's own name or"
                        " slug only")
    sub.add_parser("list", help="list addressable cousins")
    args = parser.parse_args(argv)

    try:
        fw = FrameworkConfig.for_command()
        sender = CousinConfig.from_env()
    except MissingConfigError as e:
        print("cousin-chat: %s" % e, file=sys.stderr)
        return 2

    if args.cmd == "list":
        try:
            external = list_external_peers(fw, sender.slug)
        except MissingConfigError as e:
            print("cousin-chat: %s" % e, file=sys.stderr)
            return 2
        from cousin_lib import delivery
        for c in list_peers(fw, sender.slug):
            marker = " (self)" if c.slug == sender.slug else ""
            kind = delivery._runner_kind(c.home) or (
                "worker" if c.type == "worker" else "none")
            print("%-12s kind=%-8s%s%s" % (c.slug, kind, marker,
                                           login_marker(c.home)))
        for p in external:
            print("%-12s url=%s (external)" % (p.slug, p.url))
        return 0

    try:
        result = send_message(
            fw,
            sender,
            args.slug,
            args.text,
            policy=OutboundPolicy.load(fw.root),
            display_name=args.display_name,
        )
    except FilterBlocked as e:
        print("cousin-chat: %s" % e, file=sys.stderr)
        return 3
    except (MissingConfigError, NoContextError, ValueError) as e:
        print("cousin-chat: %s" % e, file=sys.stderr)
        return 2
    except (DeliveryRefused, urllib.error.URLError) as e:
        print("cousin-chat: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "to": args.slug, "id": result.get("id")}, sort_keys=True))
    return 0
