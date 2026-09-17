"""Console authentication: PBKDF2-HMAC-SHA256 users in
config/console-users.json, in-memory sessions behind an HttpOnly
cookie, and the four account routes (docs/console-spec.md, "The auth
model").

First run: with no users file the console is open to everyone the
network guard admits and `GET /api/auth/me` says `configured: false`.
`cousin-console adduser <name>` (password from a prompt, never argv)
creates the file; from then on every `/api/*` route but login and
`me` needs a session, with no address-based bypass.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from cousin_lib.console import router
from cousin_lib.console.app import HttpError

ITERATIONS = 200000
SALT_BYTES = 16
MIN_PASSWORD_CHARS = 8
SESSION_IDLE_SECONDS = 30 * 24 * 3600
COOKIE = "console_session"
_DUMMY_SALT = bytes(SALT_BYTES)


def hash_password(password, salt, iterations=ITERATIONS):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                               iterations).hex()


class Users:
    """The users file. Shape: {"<user>": {"salt": hex, "hash": hex,
    "iterations": N}}; written atomically with mode 0600."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def configured(self):
        return bool(self.load())

    def names(self):
        return sorted(self.load())

    def set_password(self, name, password):
        data = self.load()
        salt = secrets.token_bytes(SALT_BYTES)
        data[name] = {"salt": salt.hex(),
                      "hash": hash_password(password, salt, ITERATIONS),
                      "iterations": ITERATIONS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def verify(self, name, password):
        """Constant-time compare; an unknown user still costs one PBKDF2
        so the response time does not name who exists."""
        entry = self.load().get(name) if isinstance(name, str) else None
        if not isinstance(entry, dict):
            hash_password(password, _DUMMY_SALT, ITERATIONS)
            return False
        try:
            salt = bytes.fromhex(entry["salt"])
            iterations = int(entry["iterations"])
            expected = entry["hash"]
        except (KeyError, ValueError, TypeError):
            return False
        return hmac.compare_digest(hash_password(password, salt, iterations),
                                   expected)


class Sessions:
    """Tokens in this process only: a restart logs everyone out."""

    def __init__(self, idle_seconds=SESSION_IDLE_SECONDS):
        self.idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._rows = {}

    def create(self, user, *, now=None):
        token = secrets.token_hex(32)
        with self._lock:
            self._rows[token] = {"user": user, "last": now or time.time()}
        return token

    def lookup(self, token, *, now=None):
        now = now or time.time()
        with self._lock:
            row = self._rows.get(token)
            if row is None:
                return None
            if now - row["last"] > self.idle_seconds:
                del self._rows[token]
                return None
            row["last"] = now
            return row["user"]

    def drop(self, token):
        with self._lock:
            self._rows.pop(token, None)


def cookie_header(token, *, secure=False):
    parts = ["%s=%s" % (COOKIE, token), "HttpOnly", "SameSite=Strict",
             "Path=/"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header():
    return "%s=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/" % COOKIE


def register():
    @router.route("POST", "/api/auth/login")
    def login(req):
        server = req.server
        if not server.users.configured():
            raise HttpError(409, "auth not configured")
        body = req.json()
        user, password = body.get("user"), body.get("password")
        if not isinstance(user, str) or not isinstance(password, str) \
                or not user or not password:
            raise HttpError(400, "user and password are required")
        if not server.users.verify(user, password):
            raise HttpError(401, "bad credentials")
        token = server.sessions.create(user)
        return 200, {"ok": True, "user": user}, {
            "Set-Cookie": cookie_header(token, secure=server.secure_cookie)}


    @router.route("POST", "/api/auth/logout")
    def logout(req):
        if req.session_token:
            req.server.sessions.drop(req.session_token)
        return 200, {"ok": True}, {"Set-Cookie": clear_cookie_header()}


    @router.route("GET", "/api/auth/me")
    def me(req):
        server = req.server
        return 200, {"user": req.user, "configured": server.users.configured(),
                     "users": server.users.names() if req.user else []}


    @router.route("POST", "/api/auth/change-password")
    def change_password(req):
        if req.user is None:
            raise HttpError(401, "login required")
        body = req.json()
        old, new = body.get("old_password"), body.get("new_password")
        if not isinstance(old, str) or not isinstance(new, str):
            raise HttpError(400, "old_password and new_password are required")
        if len(new) < MIN_PASSWORD_CHARS:
            raise HttpError(400, "new password must be at least %d characters"
                            % MIN_PASSWORD_CHARS)
        if not req.server.users.verify(req.user, old):
            raise HttpError(403, "current password incorrect")
        req.server.users.set_password(req.user, new)
        return 200, {"ok": True, "user": req.user}


register()
