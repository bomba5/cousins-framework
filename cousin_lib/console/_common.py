"""Helpers every route module shares: slug and name validation, cousin
lookup against the filesystem registry, the chat-server client for
proxied reads and writes, and the tmux runner on the console's binary
and socket."""
from __future__ import annotations

import json
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from cousin_lib.config import CousinConfig, MissingConfigError
from cousin_lib.console.app import HttpError

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def check_slug(slug):
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise HttpError(400, "bad slug")
    return slug


def check_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise HttpError(400, "bad loop name")
    return name


def cousin_home(server, slug):
    """The home of a registered cousin, else 404. The slug is checked
    before any lookup."""
    check_slug(slug)
    home = server.root / "cousins" / slug
    if not (home / "cousin.toml").is_file():
        raise HttpError(404, "unknown cousin %s" % slug)
    return home


def load_cousin(server, slug):
    home = cousin_home(server, slug)
    try:
        return CousinConfig.load(home)
    except (MissingConfigError, tomllib.TOMLDecodeError) as err:
        raise HttpError(500, "cousin.toml unusable for %s: %s" % (slug, err))


def read_toml(home):
    try:
        return tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def chat_base(config):
    if not config.chat_port:
        return None
    return "http://%s:%d" % (config.chat_host or "127.0.0.1",
                             config.chat_port)


def chat_call(config, path, *, method="GET", payload=None, timeout=5.0):
    """Proxy one call to the cousin's chat server. (status, body) for a
    JSON answer of any status; HttpError 502 when the server is
    unreachable or answers non-JSON; 404 when there is no port."""
    base = chat_base(config)
    if base is None:
        raise HttpError(404, "cousin %s has no chat port" % config.slug)
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise HttpError(502, "chat server for %s unreachable: %s"
                        % (config.slug, err))
    try:
        body = json.loads(raw)
    except ValueError:
        raise HttpError(502, "chat server for %s answered non-JSON"
                        % config.slug)
    return status, body


def chat_health(config, timeout=0.75):
    """'ok', 'down' or 'none' - the fleet row's chat column."""
    if not config.chat_port:
        return "none"
    try:
        status, body = chat_call(config, "/health", timeout=timeout)
    except HttpError:
        return "down"
    return "ok" if status == 200 else "down"


def tmux(server, args, *, timeout=5):
    cmd = [server.tmux_bin]
    if server.tmux_socket:
        cmd += ["-S", server.tmux_socket]
    return subprocess.run(cmd + list(args), capture_output=True, text=True,
                          timeout=timeout, check=False)


def session_alive(server, config):
    try:
        return tmux(server, ["has-session", "-t",
                             config.tmux_session]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
