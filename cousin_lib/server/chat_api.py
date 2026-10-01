"""The chat API as library calls: one implementation of what
`/api/send`, `/api/<slug>_reply`, `/api/history`, `/api/search`,
`/api/archive` and `/api/reactions` do (docs/reference/chat-api.md),
over a cousin home's `data/chat.db`.

No cousin runs a chat server of its own (2.0.0): the console, the
Telegram bridge, `cousin-chat` and `cousin-reply` call these functions
in-process, over the cousin's own store (spec, "The store is the bus":
the console serves chat as a projection of a store it does not own). The
same API over HTTP is what a hive node's chat server answers, which the
console proxies to.

Each call takes the request's parameters as a plain mapping (query
parameters: the first value of each, as strings) and returns the JSON
body of a 200, or raises BadRequest, whose message is the 400 body's
`error`. The seams a send and a reaction need (delivery, the reaction
notice) are passed in: `make_deliver` and `make_notify` build them.
"""
import json
from pathlib import Path

from cousin_lib import chat_hooks, delivery
from cousin_lib.server.inbound import after_inbound_stored, divert_login_code
from cousin_lib.server.storage import ChatStore, normalize_chat_user, save_data_uri


class BadRequest(ValueError):
    """A client error: its message is the 400 body's `error`."""


def db_path(home):
    return Path(home) / "data" / "chat.db"


def _with_store(home, fn):
    """fn(store) on a connection opened for this call and closed after it."""
    store = ChatStore(db_path(home))
    try:
        return fn(store)
    finally:
        store.close()


def _int(query, name):
    raw = query.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise BadRequest("%s must be an integer" % name)


def history(home, query):
    user = query.get("user")
    if not user:
        raise BadRequest("user is required")
    since, before, limit = _int(query, "since"), _int(query, "before"), _int(query, "limit")
    return _with_store(home, lambda store: store.history(
        user, since=since, before=before, limit=limit or 200,
        archived=query.get("archived") or "0"))


def search(home, query):
    q = query.get("q")
    if not q:
        raise BadRequest("q is required")
    hits = _with_store(home, lambda store: store.search(
        q, user=query.get("user"), archived=query.get("archived") or "0"))
    return {"messages": hits}


def archive(home, body):
    user = body.get("user")
    keep = body.get("keep", 0)
    if not user:
        raise BadRequest("user is required")
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
        raise BadRequest("keep must be a non-negative integer")
    archived = _with_store(home, lambda store: store.archive(user, keep=keep))
    return {"ok": True, "archived": archived}


def react(home, body, *, notify=None):
    """Apply a reaction; an added or bumped one tells the cousin through
    `notify(text)` (the `[fw-reaction]` line)."""
    message_id = body.get("message_id")
    user = body.get("user")
    emoji = body.get("emoji")
    action = body.get("action")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        raise BadRequest("message_id must be an integer")
    if not user or not emoji:
        raise BadRequest("user and emoji are required")
    if action not in ("tap", "remove"):
        raise BadRequest("action must be 'tap' or 'remove'")
    out = _with_store(home, lambda store: store.react(
        message_id, user=user, emoji=emoji, action=action))
    if out["op"] in ("added", "bumped") and notify is not None:
        mine = next(r for r in out["reactions"]
                    if r["user"] == user and r["emoji"] == emoji)
        notify("[fw-reaction] msg-id=%d emoji=%s user=%s tap_count=%d op=%s"
               % (message_id, emoji, user, mine["tap_count"], out["op"]))
    return out


def _attachment(home, message_id, image):
    """The delivered attachment for an inbound data: image, saved as
    <home>/chat/inbound/<id>.<ext>: the file's path (the runner's envelope
    reads it as an image block)."""
    path = save_data_uri(home, image, folder="inbound", name=str(message_id))
    return str(path) if path is not None else "[image attached, decode failed]"


def send(config, body, *, deliver=None):
    """One inbound chat message: divert a login code, store the row,
    deliver it (`deliver(user=, message=, message_id=, attachments=)`,
    fire-and-forget), touch the presence marker and capture a correction,
    then fire the chat hooks. A runner recalls in its own prompt hook, so
    the delivered item carries no recall line."""
    user = body.get("user")
    message = body.get("message")
    if not user or not message:
        raise BadRequest("user and a non-empty message are required")
    home = config.home
    diverted = divert_login_code(config, user, message)
    if diverted is not None:
        row = _with_store(home, lambda store: store.add_message(
            chat_user=normalize_chat_user(user), user=user, message=diverted,
            msg_type="user"))
        return {"ok": True, "id": row["id"], "timestamp": row["timestamp"], "diverted": True}
    reply_to = body.get("reply_to")
    row = _with_store(home, lambda store: store.add_message(
        chat_user=normalize_chat_user(user), user=user, message=message, msg_type="user",
        reply_to=json.dumps(reply_to) if reply_to is not None else None))
    attachments = []
    if body.get("image"):
        attachments.append(_attachment(home, row["id"], body["image"]))
    if deliver is not None:
        deliver(user=user, message=message, message_id=row["id"], attachments=attachments)
    after_inbound_stored(config, user, message)
    chat_hooks.on_message(home, user=user, message=message, message_id=row["id"],
                          slug=config.slug, deliver=deliver)
    return {"ok": True, "id": row["id"], "timestamp": row["timestamp"]}


def reply(config, body):
    """The cousin's own outbound (`/api/<slug>_reply`): one row stored
    under the recipient's thread, never delivered back to the cousin.
    `message` or an attachment (`{"kind", "path"}`, a file already staged
    under the home's chat/ folder), and `reply_to_user`, which has no
    default. Every caller runs in the cousin's own context (cousin-reply,
    the media `chat` commands, the runner's reply tool), so this is an
    in-process write, not a request to a server."""
    message = body.get("message") or ""
    reply_to_user = body.get("reply_to_user")
    attachment = body.get("attachment") or {}
    kind = attachment.get("kind")
    path = attachment.get("path")
    # A caption-less attachment is a valid reply: message OR attachment.
    if not message and not (kind and path):
        raise BadRequest("a reply needs a non-empty message or an attachment")
    if not reply_to_user:
        raise BadRequest("reply_to_user is required: there is no default recipient")
    reply_to = body.get("reply_to")
    row = _with_store(config.home, lambda store: store.add_message(
        chat_user=normalize_chat_user(reply_to_user), user=config.name,
        message=message, msg_type=config.slug,
        reply_to=json.dumps(reply_to) if reply_to is not None else None,
        reply_to_user=reply_to_user, attachment_kind=kind, attachment_path=path))
    return {"ok": True, "id": row["id"], "timestamp": row["timestamp"]}


def make_deliver(config):
    """The `deliver` seam handed to send(): a chat item on the sender's
    thread (a hook's inject: line on `system`), through delivery.deliver
    without waiting."""
    def deliver(*, user, message, message_id, attachments=()):
        source = "hook" if user == chat_hooks.HOOK_SENDER else "chat"
        thread = (delivery.thread_id("system") if source == "hook"
                  else delivery.thread_for_chat(config, user))
        item = delivery.Item(thread_id=thread, source=source, sender=user, body=message,
                             attachments=tuple(attachments), message_id=message_id)
        return delivery.deliver(config.home, item, wait=False)
    return deliver


def make_notify(config):
    """The `notify` seam for a reaction: a `reaction` item on `system`."""
    def notify(text):
        item = delivery.Item(thread_id=delivery.thread_id("system"), source="reaction",
                             body=text)
        return delivery.deliver(config.home, item, wait=False)
    return notify
