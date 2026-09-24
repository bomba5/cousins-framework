"""Telegram bridge: relay a cousin's chat to and from Telegram.

docs/chat.md is the contract. This is the framework's first
send-to-a-third-party feature, so the module refuses to run unless
fully configured: a missing token, a disabled flag, or an empty
operator allowlist is an error naming the cause, never a silent
no-send that looks like a working bot dropping messages.

The bridge is a pure relay: an inbound Telegram message becomes
`POST /api/send` to a tmux cousin's own chat server, or, for a runner
cousin, a stored chat row delivered to its inbox (the steps /api/send
takes, done here); the cousin's replies are relayed back to the
operator's Telegram chat. It never types into a terminal and holds no
chat state of its own; chat.db owns it.
The only thing it keeps is where it is: the Telegram update offset and
one reply cursor per operator thread, in data/telegram-bridge.json. A
cursor moves only past what was delivered, so a failed relay is retried
and a restart resumes instead of re-sending the thread.
The Telegram API and the chat server are injected here so the relay is
testable without a real network.
"""
import base64
import json
import mimetypes
import os
import uuid
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cousin_lib import chat_hooks, delivery
from cousin_lib.config import CousinConfig, FrameworkConfig
from cousin_lib.server.inbound import after_inbound_stored, divert_login_code
from cousin_lib.server.storage import (ChatStore, normalize_chat_user,
                                       save_data_uri)

_API = "https://api.telegram.org/bot%s/%s"
_FILE_API = "https://api.telegram.org/file/bot%s/%s"
# getFile serves up to 20 MB; the chat server keeps a photo inline in
# one JSON body, so the bridge takes less than that.
_MAX_INBOUND_BYTES = 10 * 1024 * 1024
# The Bot API's upload limits: 10 MB for a photo, 50 MB for other files.
_MB = 1024 * 1024
_UPLOAD_LIMITS = {"image": 10 * _MB}
_UPLOAD_LIMIT_DEFAULT = 50 * _MB


class TelegramConfigError(Exception):
    """The bridge cannot run as configured; the message names the fix."""


@dataclass
class BridgeConfig:
    slug: str
    token: str
    operator_ids: set
    operator_name: dict
    port: int
    home: Path

    def threads(self):
        """Operator thread name -> the Telegram ids that own it. Inbound
        lands in the thread named for the sender, so that thread's
        replies go back to those ids only."""
        out = {}
        for user_id in self.operator_ids:
            out.setdefault(_thread_name(self, user_id), set()).add(user_id)
        return out


def _thread_name(cfg, user_id):
    return cfg.operator_name.get(user_id) or "operator"


_CURSOR_FILE = "telegram-bridge.json"


def load_cursors(home):
    """Where the bridge is. A missing or unreadable file is a fresh
    start: offset 0 lets Telegram hand back what it still holds, and a
    thread with no cursor is started at its newest row by the bridge."""
    path = Path(home) / "data" / _CURSOR_FILE
    try:
        data = json.loads(path.read_text())
        state = {"tg_offset": int(data["tg_offset"]),
                 "threads": {str(k): int(v)
                             for k, v in data["threads"].items()}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"tg_offset": 0, "threads": {}}
    if data.get("login_notified_since"):
        # the login notice already sent (relay_login_notice): once per `since`
        state["login_notified_since"] = str(data["login_notified_since"])
    return state


def save_cursors(home, state):
    path = Path(home) / "data" / _CURSOR_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _describe(err):
    """An error for the log. An HTTPError's str() is only the status
    line; the reason is in its body - Telegram's `description`, the chat
    server's `error` - so read that once and keep it on the error."""
    if not isinstance(err, urllib.error.HTTPError):
        return str(err)
    if not hasattr(err, "_bridge_detail"):
        try:
            raw = err.read() or b""
        except Exception:
            raw = b""
        text = raw.decode("utf-8", "replace").strip()
        try:
            data = json.loads(text)
            text = data.get("description") or data.get("error") or text
        except (ValueError, AttributeError):
            pass
        err._bridge_detail = text[:300]
    if err._bridge_detail:
        return "HTTP %d: %s" % (err.code, err._bridge_detail)
    return str(err)


def _permanent(err):
    """A 4xx other than 429 will fail the same way on every retry;
    anything else (network, 5xx, rate limit) may pass next time."""
    return (isinstance(err, urllib.error.HTTPError)
            and 400 <= err.code < 500 and err.code != 429)


def load_bridge_config(home, root=None):
    """Load and validate the per-cousin bridge config, or raise.
    Validation is the perimeter: no partial-config path reaches the
    network. `root` is the framework root (else FRAMEWORK_ROOT, else the
    home's location): the supervisor passes its own, so its check and
    its child agree."""
    home = Path(home)
    try:
        data = tomllib.loads((home / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise TelegramConfigError("cannot read cousin.toml: %s" % err)
    section = data.get("telegram")
    if not section:
        raise TelegramConfigError(
            "no [telegram] section; the bridge is off for this cousin")
    if not section.get("enabled"):
        raise TelegramConfigError("[telegram] enabled is not true")
    root = (root or os.environ.get("FRAMEWORK_ROOT")
            or FrameworkConfig.root_from_home(home))
    if not root:
        raise TelegramConfigError(
            "no framework root: set FRAMEWORK_ROOT, or run the bridge"
            " on a home under <root>/cousins/<slug>")
    root = Path(root)
    token_file = section.get("token_file", "")
    try:
        token = (root / token_file).read_text().strip()
    except OSError:
        raise TelegramConfigError(
            "no bot token at %s; write it before enabling the bridge"
            % (root / token_file))
    operators = section.get("operators") or []
    ids = {op["user_id"] for op in operators if "user_id" in op}
    if not ids:
        raise TelegramConfigError(
            "no operators configured; a bot with no allowlist would"
            " serve anyone who finds its username")
    names = {op["user_id"]: op.get("name", "")
             for op in operators if "user_id" in op}
    return BridgeConfig(
        slug=data["cousin"]["slug"], token=token, operator_ids=ids,
        operator_name=names, port=(data.get("chat") or {}).get("port"), home=home)


def _default_chat_send(cfg, *, user, message, attachment=None):
    """One inbound message into the cousin's chat, by the cousin's lane.
    A runner cousin (cousin.toml `[agent] runner`): the bridge stores the
    row and delivers it itself (_store_and_deliver). A tmux cousin: the
    chat server's own `POST /api/send` (_post_to_chat_server), because
    that is where recall, the tmux socket options and the inject lock
    live; the bridge never types into the terminal itself."""
    if isinstance(delivery.backend_for(cfg.home), delivery.InboxBackend):
        return _store_and_deliver(cfg, user=user, message=message,
                                  attachment=attachment)
    return _post_to_chat_server(cfg, user=user, message=message,
                                attachment=attachment)


def _post_to_chat_server(cfg, *, user, message, attachment=None):
    if not cfg.port:
        raise TelegramConfigError("a tmux cousin's bridge needs its [chat] port")
    body = {"user": user, "message": message}
    if attachment:
        body["image"] = attachment  # a data: URI, decoded by the server
    request = urllib.request.Request(
        "http://127.0.0.1:%d/api/send" % cfg.port,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10)


def _store_and_deliver(cfg, *, user, message, attachment=None):
    """The runner lane: store the row and ride `deliver()`, the steps the
    chat server's /api/send takes, without the chat server. `attachment`
    is a data: URI (as relay_inbound built it); it is decoded to
    <home>/chat/images/ and the row carries its path, the one
    convention the console and the outbound relay both read."""
    config = CousinConfig.load(cfg.home)
    # R18: a login code is stored redacted and delivered to nobody.
    diverted = divert_login_code(config, user, message)
    if diverted is not None:
        store = ChatStore(cfg.home / "data" / "chat.db")
        try:
            store.add_message(chat_user=normalize_chat_user(user), user=user, message=diverted,
                              msg_type="user")
        finally:
            store.close()
        return
    path = save_data_uri(cfg.home, attachment, folder="images") \
        if attachment else None
    store = ChatStore(cfg.home / "data" / "chat.db")
    try:
        row = store.add_message(
            chat_user=normalize_chat_user(user), user=user, message=message,
            msg_type="user",
            attachment_kind="image" if path else None,
            attachment_path=str(path) if path else None,
        )
    finally:
        store.close()

    def deliver(*, user, message, message_id, attachments=()):
        source = "hook" if user == chat_hooks.HOOK_SENDER else "chat"
        thread = (delivery.thread_id("system") if source == "hook"
                 else delivery.thread_for_chat(config, user))
        item = delivery.Item(thread_id=thread, source=source, sender=user,
                             body=message, attachments=tuple(attachments),
                             message_id=message_id)
        return delivery.deliver(cfg.home, item, wait=False)

    # Fire-and-forget by design: the outcome (delivered/queued/failed)
    # is not read here.
    deliver(user=user, message=message, message_id=row["id"],
           attachments=(str(path),) if path else ())
    # After delivery, same order /api/send has: the marker's mtime is
    # the gap baseline for the NEXT message, and the correction capture
    # rides along on the same call.
    after_inbound_stored(config, user, message)
    chat_hooks.on_message(cfg.home, user=user, message=message,
                          message_id=row["id"], slug=cfg.slug,
                          deliver=deliver)


def _default_log(line):
    import sys
    print("cousin-telegram: %s" % line, file=sys.stderr)


def relay_inbound(cfg, *, update, chat_send=None, tg_send=None,
                  tg_fetch=None, log=None):
    """One Telegram update -> the cousin's chat (_default_chat_send
    picks the lane), if the sender is authorized. An unauthorized sender is dropped SILENTLY ON THE
    WIRE - nothing forwarded, nothing sent back, so the bot never
    confirms its existence to a stranger - but LOUDLY IN THE LOG with
    the rejected id, so an operator who typoed their own chat id can
    tell "not on the list" from "bridge down" instead of facing the
    same silence pointed at them that the attacker faces."""
    log = log or _default_log
    message = update.get("message") or {}
    sender = message.get("from") or {}
    if sender.get("id") not in cfg.operator_ids:
        log("rejected message from unauthorized Telegram id %r"
            " (not in operators)" % sender.get("id"))
        try:
            # Offered to the operator for one-click adding: a bot
            # cannot look an id up from a @username.
            from cousin_lib import telegram_admin
            telegram_admin.note_refused(cfg.home, sender)
        except Exception:
            pass
        return  # silent on the wire, logged above
    chat_send = chat_send or (lambda **kw: _default_chat_send(cfg, **kw))
    user = _thread_name(cfg, sender["id"])
    photo = message.get("photo")
    if photo:
        # Telegram lists the sizes smallest first; relay the largest.
        tg_fetch = tg_fetch or (lambda file_id: _tg_fetch(cfg, file_id))
        fetched = tg_fetch(photo[-1]["file_id"])
        if fetched is None:
            log("skipped a photo from %r: over %d bytes"
                % (sender.get("id"), _MAX_INBOUND_BYTES))
            return
        payload, file_path = fetched
        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        chat_send(user=user, message=message.get("caption") or "[photo]",
                  attachment="data:%s;base64,%s"
                  % (mime, base64.b64encode(payload).decode()))
        return
    text = message.get("text") or ""
    if not text:
        log("skipped a message from %r that is neither text nor a photo"
            % sender.get("id"))
        return
    chat_send(user=user, message=text)


def relay_outbound(cfg, *, new_replies, tg_send_text,
                   tg_send_media=None, operator_ids=None, log=None):
    """The cousin's new replies -> the operators' Telegram chats (all of
    them unless `operator_ids` narrows it). Text goes as a message; an
    attachment relays the local file. A permanent rejection for one
    operator is logged and the others still get the reply; a transient
    error propagates, so the caller retries the reply. A retry can
    repeat it to an operator who already had it: at least once, never
    lost."""
    log = log or _default_log
    for reply in new_replies:
        kind = reply.get("attachment_kind")
        path = reply.get("attachment_path")
        if path:
            path = str(cfg.home / path)  # an absolute path stays as is
        if kind and path and tg_send_media is not None:
            unsendable = _unsendable(kind, path)
            if unsendable:
                log("reply %s: %s, skipped" % (reply.get("id"), unsendable))
                continue
        for operator_id in sorted(operator_ids or cfg.operator_ids):
            try:
                if kind and path and tg_send_media is not None:
                    tg_send_media(chat_id=operator_id, kind=kind,
                                  path=path,
                                  caption=reply.get("message") or "")
                elif reply.get("message"):
                    tg_send_text(chat_id=operator_id,
                                 text=reply["message"])
            except Exception as err:
                if not _permanent(err):
                    raise
                log("reply %s to %r rejected, skipped: %s"
                    % (reply.get("id"), operator_id, _describe(err)))


def _unsendable(kind, path):
    """Why the file can never be uploaded, or None. Checked before the
    upload: a missing file would raise an OSError, which reads as
    transient and would hold the cursor on this reply for good."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "attachment %s is missing" % path
    limit = _UPLOAD_LIMITS.get(kind, _UPLOAD_LIMIT_DEFAULT)
    if size > limit:
        return ("attachment %s is %.1f MB, over Telegram's %d MB limit"
                % (path, size / _MB, limit // _MB))
    return None


def pump_inbound(cfg, state, updates, *, relay=None, log=None):
    """Relay Telegram updates in order, moving the offset past each one
    only once it is relayed or permanently rejected. A transient error
    propagates with the offset still on the failed update."""
    log = log or _default_log
    relay = relay or relay_inbound
    for update in updates:
        try:
            relay(cfg, update=update)
        except Exception as err:
            if not _permanent(err):
                raise
            log("update %s rejected by the chat server, skipped: %s"
                % (update.get("update_id"), _describe(err)))
        state["tg_offset"] = update["update_id"] + 1
        save_cursors(cfg.home, state)


def pump_outbound(cfg, state, thread, messages, *, tg_send_text,
                  tg_send_media=None, log=None):
    """Relay one thread's new rows (oldest first) to its operators,
    moving the thread's cursor past each row once it is delivered or is
    not the cousin's. A transient error propagates with the cursor
    before the failed reply."""
    operator_ids = cfg.threads().get(thread) or set()
    for message in messages:
        if message.get("type") == cfg.slug:
            relay_outbound(cfg, new_replies=[message],
                           tg_send_text=tg_send_text,
                           tg_send_media=tg_send_media,
                           operator_ids=operator_ids, log=log)
        state["threads"][thread] = message["id"]
        save_cursors(cfg.home, state)


def _tg_call(cfg, method, params, *, timeout=30):
    data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(_API % (cfg.token, method),
                                     data=data)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read())


def _tg_fetch(cfg, file_id):
    """One Telegram file's bytes and its server path, or None when it is
    over _MAX_INBOUND_BYTES."""
    info = _tg_call(cfg, "getFile", {"file_id": file_id})["result"]
    if info.get("file_size", 0) > _MAX_INBOUND_BYTES:
        return None
    url = _FILE_API % (cfg.token, info["file_path"])
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = resp.read(_MAX_INBOUND_BYTES + 1)
    if len(payload) > _MAX_INBOUND_BYTES:
        return None
    return payload, info["file_path"]


def _upload_spec(kind):
    """An attachment kind -> the Bot API method and its file field. A
    voice reply is an mp3, which Telegram takes as audio."""
    return {"image": ("sendPhoto", "photo"),
            "video": ("sendVideo", "video")}.get(kind,
                                                  ("sendAudio", "audio"))


def _multipart(fields, file_field, path):
    """A multipart/form-data body carrying `fields` and one file, with
    its Content-Type. The Bot API takes an upload no other way."""
    path = Path(path)
    boundary = uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data;"
                    ' name="%s"\r\n\r\n%s\r\n'
                    % (boundary, name, value)).encode())
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\";"
                ' filename="%s"\r\nContent-Type: %s\r\n\r\n'
                % (boundary, file_field, path.name, mime)).encode())
    out.append(path.read_bytes())
    out.append(("\r\n--%s--\r\n" % boundary).encode())
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary


def _tg_upload(cfg, kind, fields, path, *, timeout=120):
    method, file_field = _upload_spec(kind)
    body, ctype = _multipart(fields, file_field, path)
    request = urllib.request.Request(_API % (cfg.token, method), data=body,
                                     headers={"Content-Type": ctype})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read())


def _history(cfg, thread, since=None):
    """One operator thread's rows from the cousin's own chat store, oldest
    first: those after `since`, or with since None the newest row only
    (to start a fresh cursor at the end of the thread). It reads the
    store in this process (chat_api.history, what the chat server's
    /api/history runs), so no chat server has to run (phase 10a). A
    store that cannot be read raises, and the caller retries."""
    from cousin_lib.server import chat_api
    query = {"user": thread}
    query.update({"limit": "1"} if since is None else {"since": str(since)})
    return chat_api.history(cfg.home, query).get("messages", [])


def relay_login_notice(cfg, state, *, tg_send_text):
    """Once per data/login-required.json `since`: the action line to every
    operator, through the bridge's own send path. True when it sent, so
    the caller saves the cursors (`login_notified_since`)."""
    from cousin_lib.runner import auth
    data = auth.read_login_required(cfg.home)
    if not data or state.get("login_notified_since") == data.get("since"):
        return False
    what = "has a billing problem" if data.get("reason") == auth.BILLING else "needs a login"
    text = "%s: account %s %s (on %s). %s." % (
        cfg.slug, data.get("account"), what, data.get("host"), data.get("action"))
    for chat_id in sorted(cfg.operator_ids):
        tg_send_text(chat_id=chat_id, text=text)
    state["login_notified_since"] = data.get("since")
    return True


def run_bridge(home, *, poll_interval=5):
    """The daemon: long-poll Telegram for operator messages, relay the
    cousin's replies back. No inbound port; outbound HTTPS only. A
    transient error logs and backs off; the cursors stay on what was not
    delivered, so the next pass retries it, and they are saved, so a
    restart resumes where the bridge stopped."""
    import sys
    import time

    cfg = load_bridge_config(home)
    state = load_cursors(cfg.home)

    def tg_send_text(**kw):
        _tg_call(cfg, "sendMessage",
                 {"chat_id": kw["chat_id"], "text": kw["text"]})

    def tg_send_media(**kw):
        _tg_upload(cfg, kw["kind"],
                   {"chat_id": kw["chat_id"],
                    "caption": kw.get("caption", "")}, kw["path"])

    while True:
        try:
            updates = _tg_call(cfg, "getUpdates",
                               {"offset": state["tg_offset"], "timeout": 5},
                               timeout=15).get("result", [])
            pump_inbound(cfg, state, updates)
        except Exception as err:
            print("cousin-telegram: inbound error, retrying: %s"
                  % _describe(err),
                  file=sys.stderr)
            time.sleep(3)
        for thread in sorted(cfg.threads()):
            try:
                if thread not in state["threads"]:
                    newest = _history(cfg, thread)
                    state["threads"][thread] = max(
                        [m["id"] for m in newest], default=0)
                    save_cursors(cfg.home, state)
                    continue
                pump_outbound(
                    cfg, state, thread,
                    _history(cfg, thread, state["threads"][thread]),
                    tg_send_text=tg_send_text, tg_send_media=tg_send_media)
            except Exception as err:
                print("cousin-telegram: outbound error, retrying: %s"
                      % _describe(err), file=sys.stderr)
        try:
            if relay_login_notice(cfg, state, tg_send_text=tg_send_text):
                save_cursors(cfg.home, state)
        except Exception as err:
            print("cousin-telegram: login notice error, retrying: %s"
                  % _describe(err), file=sys.stderr)
        time.sleep(poll_interval)


def telegram_main(argv=None):
    """Console entry point: cousin-telegram [--home H]. Refuses to
    start unless fully configured."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(prog="cousin-telegram")
    parser.add_argument("--home", default=os.environ.get("COUSIN_HOME"))
    args = parser.parse_args(argv)
    if not args.home:
        print("cousin-telegram: set COUSIN_HOME or pass --home",
              file=sys.stderr)
        return 2
    try:
        run_bridge(args.home)
    except TelegramConfigError as err:
        print("cousin-telegram: %s" % err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(telegram_main())
