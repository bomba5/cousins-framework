"""Unread replies (docs/reference/console-api.md, "Unread"): how many
replies each cousin sent since the console user last had its chat on
screen, the number on the sidebar's badge.

The read mark used to be a time in each browser's local storage, so a
chat read on one device kept its dot on every other one, and a dot could
not say how many. Here it is a message id kept on the server per console
user, in their preferences (routes_prefs, under `chat_read`): one mark
per cousin and thread, which the chat pane moves forward, never back.

Counts cover the cousins whose chat this console answers itself, a
runner cousin on this machine, over its chat.db. A cousin the console
forwards (a hive node, a cousin with a chat host) has none."""
from __future__ import annotations

import sqlite3

from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router, routes_prefs
from cousin_lib.console._common import check_slug
from cousin_lib.console.app import HttpError
from cousin_lib.server import chat_api
from cousin_lib.server.storage import normalize_chat_user

PREF = "chat_read"


def thread_for(config, user):
    """The thread a cousin's badge counts: its operator's, the one the
    fleet row's lastMsgTs reads and the chat pane opens, else the console
    user's own. None when there is neither."""
    name = config.operator_name or user
    return normalize_chat_user(name) if name else None


def _connect(home):
    """A read-only connection to the cousin's chat.db, None when it has
    none yet: a read never creates the store."""
    path = chat_api.db_path(home)
    if not path.exists():
        return None
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1.0)


def count(config, thread, mark):
    """The cousin's replies in the live thread after `mark`. With no mark
    (this user never had the chat on screen since the server kept marks)
    the replies after the thread's last message from a person. 0 for a
    cousin with no chat yet, None when its store cannot be read."""
    try:
        conn = _connect(config.home)
        if conn is None:
            return 0
        try:
            if mark is None:
                mark = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages"
                    " WHERE chat_user=? AND type='user'", (thread,)).fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_user=? AND archived=0"
                " AND type=? AND id>?", (thread, config.slug, mark)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _newest(config, thread):
    """The thread's newest message id, 0 for none, None when the store
    cannot be read."""
    try:
        conn = _connect(config.home)
        if conn is None:
            return 0
        try:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages"
                                " WHERE chat_user=?", (thread,)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _mark(marks, slug, thread):
    per = marks.get(slug)
    value = per.get(thread) if isinstance(per, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _served(server):
    """The cousins whose chat this console answers itself."""
    from cousin_lib.console.proxy import serves_locally
    return [c for c in FrameworkConfig(server.root).list_cousins()
            if serves_locally(c)]


def unread(server, user):
    """{slug: count} for this console user over the cousins the console
    serves, leaving out one with no thread or a store it cannot read."""
    marks = routes_prefs.read(server, user).get(PREF)
    marks = marks if isinstance(marks, dict) else {}
    out = {}
    for config in _served(server):
        thread = thread_for(config, user)
        if thread is None:
            continue
        n = count(config, thread, _mark(marks, config.slug, thread))
        if n is not None:
            out[config.slug] = n
    return out


def move(server, user, config, thread, msg_id):
    """Move this user's mark for the thread forward to msg_id, never back
    and never past the thread's newest message. A mark already past the
    newest one comes from a store since rebuilt (its ids started again),
    so it is replaced. Returns (mark, moved)."""
    def apply(prefs):
        # read under the prefs lock, so two moves never undo each other
        newest = _newest(config, thread)
        want = msg_id if newest is None else min(msg_id, newest)
        marks = prefs.get(PREF)
        if not isinstance(marks, dict):
            marks = prefs[PREF] = {}
        per = marks.get(config.slug)
        if not isinstance(per, dict):
            per = marks[config.slug] = {}
        have = _mark(marks, config.slug, thread)
        stale = have is not None and newest is not None and have > newest
        if have is not None and have >= want and not stale:
            return have, False
        per[thread] = want
        return want, True
    return routes_prefs.update(server, user, apply)


def _served_cousin(server, slug):
    check_slug(slug)
    for config in _served(server):
        if config.slug == slug:
            return config
    raise HttpError(404, "no cousin %s whose chat this console serves" % slug)


def register():
    @router.route("GET", "/api/chat/unread")
    def get_unread(req):
        return 200, {"unread": unread(req.server, req.user)}

    @router.route("POST", "/api/chat/read")
    def post_read(req):
        body = req.json()
        msg_id, chat_user = body.get("id"), body.get("user")
        if not isinstance(msg_id, int) or isinstance(msg_id, bool) or msg_id < 0:
            raise HttpError(400, "id must be a message id, a whole number of at least 0")
        if not isinstance(chat_user, str) or not chat_user.strip():
            raise HttpError(400, "user must name the chat's thread")
        config = _served_cousin(req.server, body.get("cousin"))
        mark, moved = move(req.server, req.user, config,
                           normalize_chat_user(chat_user), msg_id)
        if moved:
            # no user and no cousin in it: every open page asks for its
            # own counts again, so another user's tab learns nothing
            req.server.emit("chat-read", {})
        return 200, {"ok": True, "cousin": config.slug, "mark": mark,
                     "unread": unread(req.server, req.user)}


register()
