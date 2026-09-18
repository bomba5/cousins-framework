"""Cousin-to-cousin chat: send a message to a peer's chat server.

Targets resolve from the filesystem registry, so peer chat works with no
other service running. The peer-visibility gate is bidirectional: a
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


class NoContextError(Exception):
    """The send cannot be attributed or routed; refuse rather than guess."""


class PeerAddressRefused(ValueError):
    """An external peer's address is outside the network guard."""


@dataclass(frozen=True)
class ExternalPeer:
    slug: str
    url: str
    send_path: str = DEFAULT_SEND_PATH

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
        send_path = entry.get("send_path", DEFAULT_SEND_PATH)
        if not isinstance(send_path, str) or not send_path.startswith("/"):
            raise MissingConfigError(
                "%s send_path must start with '/', got %r"
                % (where, send_path))
        out[slug] = ExternalPeer(slug=slug, url=url, send_path=send_path)
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


def _post_external(peer, payload, guard):
    check_peer_address(peer, guard)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(
        peer.send_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req, timeout=5) as r:
        body = r.read()
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {}


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
    if policy is not None:
        policy.check(
            text,
            from_slug=sender.slug,
            dest_slug=dest_slug,
            surface="chat",
            context="chat send",
        )
    payload = {"user": display_name or sender.name, "message": text}
    if isinstance(target, ExternalPeer):
        if guard is None:
            from cousin_lib.server.netguard import NetGuard
            guard = NetGuard.from_config(fw.root)
        return _post_external(target, payload, guard)
    url = "http://%s:%d/api/send" % (
        target.chat_host or "localhost",
        target.require_chat_port(),
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


@traced_cli("cousin-chat")
def chat_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-chat")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="post a message to another cousin")
    s.add_argument("slug")
    s.add_argument("text")
    s.add_argument("--from", dest="display_name", help="sender display name")
    sub.add_parser("list", help="list addressable cousins")
    args = parser.parse_args(argv)

    try:
        fw = FrameworkConfig.from_env()
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
        for c in list_peers(fw, sender.slug):
            marker = " (self)" if c.slug == sender.slug else ""
            print("%-12s port=%-6s%s" % (c.slug, c.chat_port or "?", marker))
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
    except urllib.error.URLError as e:
        print("cousin-chat: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "to": args.slug, "id": result.get("id")}, sort_keys=True))
    return 0
