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
import json
import os
import tomllib
import urllib.error
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
    text = message.get("text") or ""
    if not text:
        log("skipped a non-text message from %r (only text is relayed)"
            % sender.get("id"))
        return
    chat_send = chat_send or (lambda **kw: _default_chat_send(cfg, **kw))
    chat_send(user=_thread_name(cfg, sender["id"]), message=text)


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
        _tg_call(cfg, "sendPhoto" if kw["kind"] == "image"
                 else "sendVideo" if kw["kind"] == "video"
                 else "sendAudio",
                 {"chat_id": kw["chat_id"],
                  "caption": kw.get("caption", "")})

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
