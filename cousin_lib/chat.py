"""Cousin-to-cousin chat: send a message to a peer's chat server.

Targets resolve from the filesystem registry, so peer chat works with no
other service running. The peer-visibility gate is bidirectional: a
cousin marked not peer-visible is absent from peer lists and sees no
peers itself, because isolation that depends on the isolated party not
looking is not isolation. Operator surfaces do not use this gate.

Outbound text passes the per-surface filter before anything touches the
wire; a blocked message is never partially sent.
"""
import json
import urllib.request

from cousin_lib.config import CousinConfig


class NoContextError(Exception):
    """The send cannot be attributed or routed; refuse rather than guess."""


def list_peers(fw, self_slug):
    rows = [c for c in fw.list_cousins() if c.peer_visible]
    me = next((c for c in fw.list_cousins() if c.slug == self_slug), None)
    if me is not None and not me.peer_visible:
        return []
    return rows


def _resolve(fw, dest_slug):
    for c in fw.list_cousins():
        if c.slug == dest_slug:
            return c
    raise NoContextError("no cousin %r in the registry" % dest_slug)


def send_message(fw, sender, dest_slug, text, policy=None, display_name=None):
    if sender is None or not sender.slug:
        raise NoContextError("no sender context; refusing an unattributed send")
    if dest_slug == sender.slug:
        raise ValueError("refusing to send to self (%s)" % dest_slug)
    target = _resolve(fw, dest_slug)
    if policy is not None:
        policy.check(
            text,
            from_slug=sender.slug,
            dest_slug=dest_slug,
            surface="chat",
            context="chat send",
        )
    payload = {"user": display_name or sender.name, "message": text}
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
