"""Console authentication: PBKDF2-HMAC-SHA256 users in
config/console-users.json, sessions behind an HttpOnly cookie
(persisted as token hashes in data/console-sessions.json), and the four account routes (docs/reference/console-api.md, "The auth
model").

First run: with no users file the console is open to everyone the
network guard admits and `GET /api/auth/me` says `configured: false`.
`cousin-console adduser <name>` (password from a prompt, never argv)
creates the file; from then on every `/api/*` route but login and
`me` needs a session, with no address-based bypass.

Fail closed: a users file that is PRESENT but cannot be read as a
non-empty user map (unreadable, not JSON, not an object, no users, an
entry that is not an object) is never read as "no users". Every
`/api/*` route but `me` answers 503 naming the file and the fix, until
the operator repairs or removes it. Only an ABSENT file is first run.
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


class UsersFileError(Exception):
    """The users file is present but unusable; the console stays
    closed until it is fixed or removed."""


class Users:
    """The users file. Shape: {"<user>": {"salt": hex, "hash": hex,
    "iterations": N}}; written atomically with mode 0600.

    Three states (`state()`): "missing" (no file: first run, open),
    "ok" (a non-empty map of user objects: enforced), "broken"
    (present but unusable: closed). load() and everything built on it
    raise UsersFileError in the broken state rather than return {}."""

    def __init__(self, path):
        self.path = Path(path)

    def _broken(self, why):
        return UsersFileError(
            "%s is present but unusable (%s); the console refuses every"
            " authenticated route until it is fixed. Restore it from a"
            " backup, or remove it and run `cousin-console adduser"
            " <name>` to recreate it" % (self.path, why))

    def _read(self):
        """The user map, None when the file is absent, or raise."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as err:
            raise self._broken("unreadable: %s" % err)
        try:
            data = json.loads(raw)
        except ValueError as err:
            raise self._broken("not valid JSON: %s" % err)
        if not isinstance(data, dict):
            raise self._broken("not a JSON object")
        if not data:
            raise self._broken("holds no users")
        bad = sorted(str(k) for k, v in data.items()
                     if not isinstance(v, dict))
        if bad:
            raise self._broken("entry for %s is not an object"
                               % ", ".join(bad))
        return data

    def state(self):
        """("missing", None) | ("ok", None) | ("broken", message)."""
        try:
            data = self._read()
        except UsersFileError as err:
            return "broken", str(err)
        return ("missing" if data is None else "ok"), None

    def load(self):
        return self._read() or {}

    def configured(self):
        return self._read() is not None

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
    """Session tokens, kept in memory and, given a path, on disk so a
    console restart does not log everyone out. The file holds only a
    SHA-256 of each token (a read of it opens no session), mode 0600.

    A session carries a stamp of the user's password entry (its salt)
    when the caller gives one: a lookup whose current stamp differs -
    password reset, user removed - drops the session."""

    # A lookup moves `last`; writing that on every request would churn
    # the disk, so a moved `last` is persisted at most this often.
    TOUCH_WRITE_SECONDS = 3600

    def __init__(self, idle_seconds=SESSION_IDLE_SECONDS, path=None):
        self.idle_seconds = idle_seconds
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._rows = {}
        self._written = 0
        self._load()

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _load(self):
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return  # absent or unreadable: start empty, never fail
        if not isinstance(data, dict):
            return
        now = time.time()
        for key, row in data.items():
            if isinstance(row, dict) and isinstance(row.get("user"), str) \
                    and isinstance(row.get("last"), (int, float)) \
                    and now - row["last"] <= self.idle_seconds:
                self._rows[key] = {"user": row["user"], "last": row["last"],
                                   "stamp": row.get("stamp")}

    def _save(self, now):
        """Caller holds the lock. A failed write keeps the sessions in
        memory; the next change retries."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(self._rows, fh, sort_keys=True)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._written = now
        except OSError:
            pass

    def create(self, user, *, stamp=None, now=None):
        now = now or time.time()
        token = secrets.token_hex(32)
        with self._lock:
            self._rows[self._key(token)] = {"user": user, "last": now,
                                            "stamp": stamp}
            self._save(now)
        return token

    def lookup(self, token, *, now=None, stamp_of=None):
        now = now or time.time()
        key = self._key(token)
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                return None
            stale = now - row["last"] > self.idle_seconds
            if not stale and stamp_of is not None:
                try:
                    stale = stamp_of(row["user"]) != row.get("stamp")
                except UsersFileError:
                    pass  # a broken users file is refused upstream
                          # anyway; it must not log everyone out
            if stale:
                del self._rows[key]
                self._save(now)
                return None
            row["last"] = now
            if now - self._written > self.TOUCH_WRITE_SECONDS:
                self._save(now)
            return row["user"]

    def restamp(self, token, stamp):
        """Keep the session that changed the password valid; every
        other session of that user drops at its next lookup."""
        with self._lock:
            row = self._rows.get(self._key(token))
            if row is not None:
                row["stamp"] = stamp
                self._save(time.time())

    def drop(self, token):
        with self._lock:
            if self._rows.pop(self._key(token), None) is not None:
                self._save(time.time())


def user_stamp(users, name):
    """The stamp a session carries: the salt of the user's password
    entry, which set_password always changes; None for no such user."""
    entry = users.load().get(name)
    return entry.get("salt") if isinstance(entry, dict) else None


def cookie_header(token, *, secure=False):
    # A persistent cookie, not a session one: an iOS home-screen web
    # app drops session cookies whenever the system closes the app.
    parts = ["%s=%s" % (COOKIE, token), "Max-Age=%d" % SESSION_IDLE_SECONDS,
             "HttpOnly", "SameSite=Strict", "Path=/"]
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
        token = server.sessions.create(
            user, stamp=user_stamp(server.users, user))
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
        state, error = server.users.state()
        if state == "broken":
            # Answered, not refused, so the page can say why it is
            # closed; it names nobody and grants nothing.
            return 200, {"user": None, "configured": True, "users": [],
                         "error": error}
        return 200, {"user": req.user, "configured": state == "ok",
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
        req.server.sessions.restamp(
            req.session_token, user_stamp(req.server.users, req.user))
        return 200, {"ok": True, "user": req.user}


register()
