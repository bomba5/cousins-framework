"""Post a reply to this cousin's own chat surface.

The reply path is cousin -> own chat-server; it never reaches another
cousin's surface. Two contract rules: a process without cousin context is
refused rather than guessed, because a reply on the wrong surface is a
disclosure; and the body travels as JSON so newlines survive shell
quoting.
"""
import json
import urllib.request

from cousin_lib.config import MissingConfigError


def send_reply(cfg, body, user=None, reply_to=None):
    body = body.rstrip("\n")
    if not body.strip():
        raise ValueError("empty message body")
    recipient = user or cfg.operator_name
    if not recipient:
        raise MissingConfigError(
            "no --user given and no [operator] name configured; "
            "a reply needs a recipient"
        )
    payload = {"message": body, "reply_to_user": recipient}
    if reply_to is not None:
        payload["reply_to"] = {"id": reply_to}
    url = "http://localhost:%d/api/%s_reply" % (cfg.require_chat_port(), cfg.slug)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read() or b"{}")
