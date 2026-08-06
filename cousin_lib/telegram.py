"""Telegram bridge: relay a cousin's chat to and from Telegram.

docs/telegram-spec.md is the contract. This is the framework's first
send-to-a-third-party feature, so the module refuses to run unless
fully configured: a missing token, a disabled flag, or an empty
operator allowlist is an error naming the cause, never a silent
no-send that looks like a working bot dropping messages.

The bridge is a pure relay: inbound Telegram messages become
`POST /api/send` to the cousin's own chat server, and the cousin's
replies are relayed back to the operator's Telegram chat. It touches
no terminal and holds no chat state; the chat server owns all of it.
The Telegram API and the chat server are injected here so the relay is
testable without a real network.
"""
import json
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cousin_lib.config import FrameworkConfig

_API = "https://api.telegram.org/bot%s/%s"


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
    root = FrameworkConfig.from_env().root
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
                  log=None):
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
    name = cfg.operator_name.get(sender["id"]) \
        or sender.get("first_name") or "operator"
    text = message.get("text") or ""
    chat_send(user=name, message=text)


def relay_outbound(cfg, *, new_replies, tg_send_text,
                   tg_send_media=None):
    """The cousin's new replies -> the operator's Telegram chat. Text
    goes as a message; an attachment relays the local file. Every
    operator gets the cousin's reply (v1: one operator chat per id)."""
    for reply in new_replies:
        kind = reply.get("attachment_kind")
        path = reply.get("attachment_path")
        for operator_id in cfg.operator_ids:
            if kind and path and tg_send_media is not None:
                tg_send_media(chat_id=operator_id, kind=kind, path=path,
                              caption=reply.get("message") or "")
            elif reply.get("message"):
                tg_send_text(chat_id=operator_id,
                             text=reply["message"])


def _tg_call(cfg, method, params, *, timeout=30):
    data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(_API % (cfg.token, method),
                                     data=data)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read())


def _poll_history_since(cfg, since_id):
    """The cousin's own replies since a cursor - rows whose type is the
    cousin's slug, from its chat server."""
    url = ("http://127.0.0.1:%d/api/history?user=%s&since=%d"
           % (cfg.port, urllib.parse.quote(
               next(iter(cfg.operator_name.values()), "operator")),
              since_id))
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except OSError:
        return since_id, []
    replies = [m for m in data.get("messages", [])
               if m.get("type") == cfg.slug]
    new_since = max([m["id"] for m in data.get("messages", [])],
                    default=since_id)
    return new_since, replies


def run_bridge(home, *, poll_interval=5):
    """The daemon: long-poll Telegram for operator messages, relay the
    cousin's replies back. No inbound port; outbound HTTPS only. A
    transient error logs and backs off - it never crashes or drops the
    cursor."""
    import sys
    import time

    cfg = load_bridge_config(home)
    tg_offset = 0
    reply_cursor = 0
    while True:
        try:
            updates = _tg_call(cfg, "getUpdates",
                               {"offset": tg_offset, "timeout": 5},
                               timeout=15).get("result", [])
            for update in updates:
                tg_offset = update["update_id"] + 1
                relay_inbound(cfg, update=update)
        except Exception as err:
            print("cousin-telegram: inbound error, retrying: %s" % err,
                  file=sys.stderr)
            time.sleep(3)
        try:
            reply_cursor, replies = _poll_history_since(
                cfg, reply_cursor)
            relay_outbound(
                cfg, new_replies=replies,
                tg_send_text=lambda **kw: _tg_call(
                    cfg, "sendMessage",
                    {"chat_id": kw["chat_id"], "text": kw["text"]}),
                tg_send_media=lambda **kw: _tg_call(
                    cfg, "sendPhoto" if kw["kind"] == "image"
                    else "sendVideo" if kw["kind"] == "video"
                    else "sendAudio",
                    {"chat_id": kw["chat_id"],
                     "caption": kw.get("caption", "")}))
        except Exception as err:
            print("cousin-telegram: outbound error, retrying: %s" % err,
                  file=sys.stderr)
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
