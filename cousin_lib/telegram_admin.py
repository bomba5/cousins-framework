"""Provisioning and lifecycle of a cousin's Telegram bridge
(docs/chat.md, "Telegram"): the operator side of cousin_lib.telegram.

- The token is write-only. It lives at <root>/config/telegram/<slug>.token,
  mode 0600, and nothing here ever returns it: status says whether one
  is set and, after a check, the bot's @name.
- Operators are the Telegram numeric ids the bot answers. A bot cannot
  look an id up from a @username, so the bridge remembers the last
  senders it refused (data/telegram-pending.json) and the console
  offers them for one-click adding: the person presses Start, the
  operator adds them.
- The bridge process belongs to its cousin: a cousin-supervisor child
  (supervisor.telegram_spec) started with the runner when [telegram]
  enabled is true, stopped with it, its pid in data/telegram.pid. With
  no supervisor to ask, the console stops a bridge left running outside
  one (stop_bridge); nothing here starts one.
"""
import json
import os
import re
import signal
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
PENDING_KEEP = 5
_MARKER = "cousin_lib.telegram"


class TelegramAdminError(ValueError):
    """A refused provisioning call; the message says what to fix."""


def _toml(home):
    try:
        return tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _section(home):
    return _toml(home).get("telegram") or {}


def token_path(root, slug):
    return Path(root) / "config" / "telegram" / ("%s.token" % slug)


def _token_file(home, root):
    section = _section(home)
    rel = section.get("token_file")
    return Path(root) / rel if rel else None


def _token(home, root):
    path = _token_file(home, root)
    try:
        return path.read_text().strip() if path else ""
    except OSError:
        return ""


# -- pending senders ---------------------------------------------------

def _pending_path(home):
    return Path(home) / "data" / "telegram-pending.json"


def note_refused(home, sender):
    """Remember a refused sender (the newest PENDING_KEEP), so the
    operator can add them without hunting for a numeric id."""
    try:
        user_id = int(sender.get("id"))
    except (TypeError, ValueError):
        return
    path = _pending_path(home)
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        rows = []
    rows = [r for r in rows if r.get("user_id") != user_id]
    rows.append({"user_id": user_id,
                 "username": sender.get("username") or "",
                 "first_name": sender.get("first_name") or "",
                 "at": datetime.now(timezone.utc)
                 .isoformat(timespec="seconds")})
    rows = rows[-PENDING_KEEP:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows))
    os.replace(tmp, path)


def pending(home):
    try:
        rows = json.loads(_pending_path(home).read_text())
    except (OSError, ValueError):
        return []
    ops = {o["user_id"] for o in operators(home)}
    return [r for r in rows if r.get("user_id") not in ops]


# -- config writes -----------------------------------------------------

def operators(home):
    out = []
    for op in _section(home).get("operators") or []:
        if isinstance(op, dict) and isinstance(op.get("user_id"), int):
            out.append({"user_id": op["user_id"],
                        "name": str(op.get("name") or "")})
    return out


def _write_section(home, section):
    """Rewrite the [telegram] table of cousin.toml from `section`,
    keeping every other line; parsed back before it is persisted."""
    path = Path(home) / "cousin.toml"
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    start = end = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[telegram\]\s*(#.*)?$", line):
            start, end = i, len(lines)
            for j in range(i + 1, len(lines)):
                if re.match(r"^\s*\[", lines[j]):
                    end = j
                    break
            break
    body = ["[telegram]\n",
            "enabled = %s\n" % ("true" if section.get("enabled") else "false")]
    if section.get("token_file"):
        body.append("token_file = %s\n" % json.dumps(section["token_file"]))
    ops = ", ".join("{ user_id = %d, name = %s }"
                    % (o["user_id"], json.dumps(o.get("name") or ""))
                    for o in section.get("operators") or [])
    body.append("operators = [%s]\n" % ops)
    if start is None:
        new = text.rstrip("\n") + "\n\n" + "".join(body)
    else:
        tail = lines[end:]
        new = "".join(lines[:start] + body
                      + (["\n"] if tail else []) + tail)
    parsed = tomllib.loads(new)
    if parsed.get("telegram", {}).get("operators", []) != [
            {"user_id": o["user_id"], "name": o.get("name") or ""}
            for o in section.get("operators") or []]:
        raise TelegramAdminError("operators did not round-trip")
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(new)
    os.replace(tmp, path)


def _current(home):
    section = dict(_section(home))
    section["operators"] = operators(home)
    return section


def set_operators(home, ops):
    clean, seen = [], set()
    for op in ops or []:
        try:
            user_id = int(op.get("user_id"))
        except (TypeError, ValueError, AttributeError):
            raise TelegramAdminError("each operator needs a numeric user_id")
        if user_id <= 0 or user_id in seen:
            raise TelegramAdminError("user_id %r is invalid or repeated"
                                     % op.get("user_id"))
        seen.add(user_id)
        name = str(op.get("name") or "").strip()
        if len(name) > 64 or any(ord(c) < 32 for c in name):
            raise TelegramAdminError("operator name must be at most 64"
                                     " printable characters")
        clean.append({"user_id": user_id, "name": name})
    section = _current(home)
    section["operators"] = clean
    _write_section(home, section)
    return clean


def set_token(home, root, slug, token):
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        raise TelegramAdminError("that does not look like a bot token"
                                 " (<digits>:<35 or so characters>)")
    path = token_path(root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    section = _current(home)
    section["token_file"] = str(path.relative_to(Path(root)))
    _write_section(home, section)


def set_enabled(home, enabled):
    section = _current(home)
    section["enabled"] = bool(enabled)
    _write_section(home, section)


def check_token(home, root, *, call=None):
    """getMe with the stored token: the bot's @name, or the error. The
    token itself is never part of the answer."""
    token = _token(home, root)
    if not token:
        return {"ok": False, "error": "no token set"}
    call = call or _get_me
    try:
        me = call(token)
    except Exception as err:  # noqa: BLE001 - the reason is the answer
        return {"ok": False, "error": str(err).replace(token, "<token>")}
    return {"ok": True, "bot": me.get("username") or ""}


def _get_me(token):
    url = "https://api.telegram.org/bot%s/getMe" % token
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise TelegramAdminError(data.get("description") or "getMe failed")
    return data["result"]


def discover(home, root, *, call=None):
    """Offer the people who wrote to the bot while no bridge was polling
    it: the first-time case, where there is no operator yet, so the
    bridge cannot run and nothing records the Start press. getUpdates
    without an offset confirms nothing, so the bridge still gets every
    update when it starts; and it never runs beside a live bridge, which
    Telegram would answer with 409. Serves nobody. Returns how many
    senders were offered, or None when it did not look."""
    if bridge_pid(home) is not None:
        return None
    token = _token(home, root)
    if not token:
        return None
    call = call or _get_updates
    try:
        updates = call(token)
    except Exception:
        return None
    ops = {o["user_id"] for o in operators(home)}
    seen = 0
    for update in updates:
        sender = (update.get("message") or {}).get("from") or {}
        if sender.get("id") and sender["id"] not in ops:
            note_refused(home, sender)
            seen += 1
    return seen


def _get_updates(token):
    url = ("https://api.telegram.org/bot%s/getUpdates?timeout=0"
           "&allowed_updates=%%5B%%22message%%22%%5D" % token)
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    return data.get("result", []) if data.get("ok") else []


# -- lifecycle ---------------------------------------------------------

def _pid_path(home):
    return Path(home) / "data" / "telegram.pid"


def _pid_is_bridge(pid):
    try:
        cmd = Path("/proc/%d/cmdline" % pid).read_bytes()
    except OSError:
        return False
    return _MARKER.encode() in cmd or b"cousin-telegram" in cmd


def bridge_pid(home):
    try:
        pid = int(_pid_path(home).read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return pid if _pid_is_bridge(pid) else None


def ready(home, root):
    """Why the bridge cannot run, or None when it can."""
    section = _section(home)
    if not section.get("enabled"):
        return "disabled"
    if not _token(home, root):
        return "no token set"
    if not operators(home):
        return ("no operators yet: press Start on the bot in Telegram,"
                " then add yourself from 'waiting to be added'")
    return None


def stop_bridge(home, *, wait=5.0):
    pid = bridge_pid(home)
    if not pid:
        _pid_path(home).unlink(missing_ok=True)
        return "not running"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    _pid_path(home).unlink(missing_ok=True)
    return "stopped"


def status(home, root):
    return {
        "enabled": bool(_section(home).get("enabled")),
        "token_set": bool(_token(home, root)),
        "operators": operators(home),
        "pending": pending(home),
        "running": bridge_pid(home) is not None,
        "ready": ready(home, root),
    }
