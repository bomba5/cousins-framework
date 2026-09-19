"""Telegram bridge: relay a cousin's chat to and from Telegram.

docs/chat.md is the contract. This is the framework's first
send-to-a-third-party feature, so the module refuses to run unless
fully configured: a missing token, a disabled flag, or an empty
operator allowlist is an error naming the cause, never a silent
no-send that looks like a working bot dropping messages.

The bridge is a pure relay: inbound Telegram messages become
`POST /api/send` to the cousin's own chat server, and the cousin's
replies are relayed back to the operator's Telegram chat. It touches
no terminal and holds no chat state; the chat server owns all of it.
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

from cousin_lib.config import FrameworkConfig

_API = "https://api.telegram.org/bot%s/%s"
_FILE_API = "https://api.telegram.org/file/bot%s/%s"
# getFile serves up to 20 MB; the chat server keeps a photo inline in
# one JSON body, so the bridge takes less than that.
_MAX_INBOUND_BYTES = 10 * 1024 * 1024


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
        return {"tg_offset": int(data["tg_offset"]),
                "threads": {str(k): int(v)
                            for k, v in data["threads"].items()}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"tg_offset": 0, "threads": {}}


def save_cursors(home, state):
    path = Path(home) / "data" / _CURSOR_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _permanent(err):
    """A 4xx other than 429 will fail the same way on every retry;
    anything else (network, 5xx, rate limit) may pass next time."""
    return (isinstance(err, urllib.error.HTTPError)
            and 400 <= err.code < 500 and err.code != 429)


def load_bridge_config(home):
    """Load and validate the per-cousin bridge config, or raise.
    Validation is the perimeter: no partial-config path reaches the
    network."""
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
    root = (os.environ.get("FRAMEWORK_ROOT")
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
        operator_name=names, port=data["chat"]["port"], home=home)


def _default_chat_send(cfg, *, user, message, attachment=None):
    body = {"user": user, "message": message}
    if attachment:
        body["image"] = attachment  # a data: URI, decoded by the server
    request = urllib.request.Request(
        "http://127.0.0.1:%d/api/send" % cfg.port,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10)


def _default_log(line):
    import sys
    print("cousin-telegram: %s" % line, file=sys.stderr)


def relay_inbound(cfg, *, update, chat_send=None, tg_send=None,
                  tg_fetch=None, log=None):
    """One Telegram update -> the cousin's chat server, if the sender
    is authorized. An unauthorized sender is dropped SILENTLY ON THE
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
                    % (reply.get("id"), operator_id, err))


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
                % (update.get("update_id"), err))
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
    """One operator thread's rows from the cousin's chat server, oldest
    first: those after `since`, or with since None the newest row only
    (to start a fresh cursor at the end of the thread). An unreachable
    server raises, and the caller retries."""
    url = ("http://127.0.0.1:%d/api/history?user=%s"
           % (cfg.port, urllib.parse.quote(thread)))
    url += "&limit=1" if since is None else "&since=%d" % since
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read()).get("messages", [])


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
            print("cousin-telegram: inbound error, retrying: %s" % err,
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
                      % err, file=sys.stderr)
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
