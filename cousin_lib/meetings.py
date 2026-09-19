"""Meetings: a chat container shared by the user and several running
cousins (docs/meetings.md).

The store is this module's SQLite database at <root>/data/meetings.db,
opened per call like the tracker's, and the orchestration lives here
too: the console and the CLI are views and entry points, never owners.
A meeting runs in rounds. The user posts; each participant speaks once,
in the stored order, and is woken only on its own turn with everything
said since its last one; then the floor is the user's again. `@slug`
at the start of a post asks one participant directly. A speaker that
does not answer in time, or whose session is gone, is skipped by
`tick`, which cousin-loops runs every tick.

Delivery is injected into the cousin's terminal. A failed injection
(the pane sat at a menu) leaves the turn undelivered and the next tick
tries again, so a turn is never recorded as sent when it was not.
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.trace import traced_cli

STATES = ("open", "closing", "closed")
MODES = ("floor", "round", "direct", "minutes")
DEFAULT_TIMEOUT_S = 600
MAX_TEXT = 20000
_DIRECT = re.compile(r"^@([A-Za-z0-9_-]+)\s+(.*)$", re.DOTALL)

HOWTO = ('Answer with: cousin-meeting say {id} "<text>" (or: cousin-meeting '
         'pass {id}). One turn; stay on topic; be brief.')
MINUTES_HOWTO = ("Write the minutes: decisions, open questions, actions with "
                 "their owner. Add each action to the tracker (cousin-tracker "
                 "add). Post them with: cousin-meeting minutes {id} \"<text>\" "
                 "(or --stdin).")


OPEN_NOTICE = ('(Meeting {id} "{topic}" opened by {user}): you are a '
               'participant, with {others}. Speaking order: {order}; you '
               'speak {place} in every round. Nothing to do now: wait for '
               'your turn, a line starting (Meeting {id} ...). Do not '
               'answer this one.')
CLOSE_NOTICE = ('(Meeting {id} "{topic}" closed): the meeting is over; '
                'nothing to answer.')


def _order(participants, current=None):
    """The speaking order with each turn numbered: "1 wren > 2 toki";
    the current speaker marked."""
    return " > ".join("%d %s%s" % (i + 1, s, " (now)" if s == current else "")
                      for i, s in enumerate(participants))


def _place(participants, slug):
    n = participants.index(slug) + 1
    return "%d of %d" % (n, len(participants))


def _notify(slugs, text_for, deliver):
    """Best-effort one-liners outside any turn (open, close): a failed
    injection is not retried, the turn line carries everything anyway."""
    for slug in slugs:
        try:
            deliver(slug, text_for(slug))
        except Exception:
            pass


class MeetingError(ValueError):
    """A refused call: bad participants, not your turn, closed meeting."""


class MeetingNotFound(MeetingError, KeyError):
    def __str__(self):
        return "meeting #%s not found" % (self.args[0],)


def db_path(root=None):
    return FrameworkConfig.resolve(root).root / "data" / "meetings.db"


def _db(root):
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for attempt in range(20):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as err:
            if "locked" not in str(err) or attempt == 19:
                break
            time.sleep(0.05 * (attempt + 1))
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS meetings ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " topic TEXT NOT NULL,"
        " participants TEXT NOT NULL,"
        " facilitator TEXT NOT NULL DEFAULT '',"
        " state TEXT NOT NULL DEFAULT 'open',"
        " mode TEXT NOT NULL DEFAULT 'floor',"
        " turn_slug TEXT NOT NULL DEFAULT '',"
        " turn_index INTEGER NOT NULL DEFAULT -1,"
        " round INTEGER NOT NULL DEFAULT 0,"
        " turn_started REAL NOT NULL DEFAULT 0,"
        " turn_delivered INTEGER NOT NULL DEFAULT 0,"
        " turn_timeout_s INTEGER NOT NULL DEFAULT 600,"
        " created_by TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL,"
        " closed_at TEXT NOT NULL DEFAULT '',"
        " updated_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS entries ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " meeting_id INTEGER NOT NULL,"
        " speaker TEXT NOT NULL,"
        " kind TEXT NOT NULL,"
        " text TEXT NOT NULL,"
        " round INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL);"
        "CREATE INDEX IF NOT EXISTS idx_entries_meeting"
        " ON entries(meeting_id, id);"
        "CREATE TABLE IF NOT EXISTS seen ("
        " meeting_id INTEGER NOT NULL,"
        " slug TEXT NOT NULL,"
        " last_entry_id INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (meeting_id, slug));")
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _meeting(row):
    m = dict(row)
    m["participants"] = json.loads(m["participants"] or "[]")
    m["turn_delivered"] = bool(m["turn_delivered"])
    return m


def _fetch(conn, meeting_id):
    row = conn.execute("SELECT * FROM meetings WHERE id=?",
                       (meeting_id,)).fetchone()
    if row is None:
        raise MeetingNotFound(meeting_id)
    return _meeting(row)


def _entries(conn, meeting_id, after=0):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM entries WHERE meeting_id=? AND id>? ORDER BY id",
        (meeting_id, after))]


def _add_entry(conn, m, speaker, kind, text):
    cur = conn.execute(
        "INSERT INTO entries (meeting_id, speaker, kind, text, round,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (m["id"], speaker, kind, text, m["round"], _now()))
    return cur.lastrowid


def _set(conn, meeting_id, **fields):
    fields["updated_at"] = _now()
    cols = ", ".join("%s=?" % k for k in fields)
    conn.execute("UPDATE meetings SET %s WHERE id=?" % cols,
                 list(fields.values()) + [meeting_id])


def _text(text):
    text = (text or "").strip()
    if not text:
        raise MeetingError("text required")
    if len(text) > MAX_TEXT:
        raise MeetingError("text over %d characters" % MAX_TEXT)
    return text


def _tx(root, fn):
    """Run fn(conn) under BEGIN IMMEDIATE; commit on success."""
    conn = _db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            out = fn(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return out
    finally:
        conn.close()


def _known_slugs(root):
    return {c.slug: c for c in FrameworkConfig.resolve(root).list_cousins()}


# -- reading ----------------------------------------------------------

def list_meetings(*, state=None, root=None):
    """Newest first; open and closing before closed."""
    conn = _db(root)
    try:
        rows = [_meeting(r) for r in conn.execute(
            "SELECT * FROM meetings ORDER BY id DESC")]
        counts = dict(conn.execute(
            "SELECT meeting_id, COUNT(*) FROM entries GROUP BY meeting_id"))
    finally:
        conn.close()
    if state:
        rows = [m for m in rows if m["state"] == state]
    for m in rows:
        m["entries"] = counts.get(m["id"], 0)
    rows.sort(key=lambda m: (m["state"] == "closed", -m["id"]))
    return rows


def show(meeting_id, *, root=None):
    conn = _db(root)
    try:
        m = _fetch(conn, meeting_id)
        m["transcript"] = _entries(conn, meeting_id)
    finally:
        conn.close()
    return m


# -- turns ------------------------------------------------------------

def _start_turn(conn, m, slug, mode, index):
    _set(conn, m["id"], turn_slug=slug, turn_index=index, mode=mode,
         turn_started=time.time(), turn_delivered=0)
    m.update(turn_slug=slug, turn_index=index, mode=mode,
             turn_delivered=False)


def _floor(conn, m):
    _set(conn, m["id"], turn_slug="", turn_index=-1, mode="floor",
         turn_delivered=0)
    m.update(turn_slug="", turn_index=-1, mode="floor")


def _advance(conn, m):
    """After a speaker is done: the next participant of a round, or the
    floor back to the user."""
    if m["state"] == "closing":
        _finish(conn, m)
        return
    if m["mode"] == "round":
        nxt = m["turn_index"] + 1
        if nxt < len(m["participants"]):
            _start_turn(conn, m, m["participants"][nxt], "round", nxt)
            return
    _floor(conn, m)


def _finish(conn, m):
    _set(conn, m["id"], state="closed", closed_at=_now(), turn_slug="",
         turn_index=-1, mode="floor", turn_delivered=0)
    m.update(state="closed", turn_slug="")


def turn_text(conn, m):
    """The message a speaker is woken with: everything said since the
    last thing it was sent, then how to answer."""
    slug = m["turn_slug"]
    row = conn.execute(
        "SELECT last_entry_id FROM seen WHERE meeting_id=? AND slug=?",
        (m["id"], slug)).fetchone()
    after = row[0] if row else 0
    entries = _entries(conn, m["id"], after)
    if m["mode"] == "minutes":
        entries = _entries(conn, m["id"])
        head = '(Meeting %d "%s" closing, you facilitate)' % (
            m["id"], m["topic"])
        howto = MINUTES_HOWTO.format(id=m["id"])
    else:
        what = ("a direct question to you" if m["mode"] == "direct"
                else "round %d, your turn: %s; order %s" % (
                    m["round"], _place(m["participants"], slug),
                    _order(m["participants"], slug)))
        head = '(Meeting %d "%s" %s)' % (m["id"], m["topic"], what)
        howto = HOWTO.format(id=m["id"])
    said = " | ".join("%s: %s" % (e["speaker"], " ".join(e["text"].split()))
                      for e in entries) or "(nothing new)"
    last = entries[-1]["id"] if entries else after
    return "%s: %s || %s" % (head, said, howto), last


def _try_deliver(conn, m, deliver):
    if not m["turn_slug"] or m["turn_delivered"]:
        return False
    text, last = turn_text(conn, m)
    if not deliver(m["turn_slug"], text):
        return False
    conn.execute(
        "INSERT INTO seen (meeting_id, slug, last_entry_id) VALUES (?, ?, ?)"
        " ON CONFLICT(meeting_id, slug) DO UPDATE SET last_entry_id="
        "excluded.last_entry_id", (m["id"], m["turn_slug"], last))
    _set(conn, m["id"], turn_delivered=1)
    m["turn_delivered"] = True
    return True


# -- mutations --------------------------------------------------------

def open_meeting(topic, participants, *, created_by="", facilitator="",
                 timeout_s=DEFAULT_TIMEOUT_S, is_alive=None, deliver=None,
                 root=None):
    topic = _text(topic)
    slugs = [str(s).strip() for s in participants or () if str(s).strip()]
    if not slugs:
        raise MeetingError("at least one participant")
    if len(set(slugs)) != len(slugs):
        raise MeetingError("a participant is listed twice")
    known = _known_slugs(root)
    for slug in slugs + ([facilitator] if facilitator else []):
        if slug not in known:
            raise MeetingError("no cousin %r" % slug)
        if known[slug].chat_host:
            raise MeetingError("%s is a remote cousin; remote cousins cannot"
                               " join a meeting yet" % slug)
    alive = is_alive or default_is_alive
    stopped = [s for s in slugs if not alive(s)]
    if stopped:
        raise MeetingError("not running: %s (start them first)"
                           % ", ".join(stopped))
    try:
        timeout_s = int(timeout_s)
    except (TypeError, ValueError):
        raise MeetingError("timeout must be whole seconds")
    if timeout_s < 60:
        raise MeetingError("timeout must be at least 60 seconds")

    def run(conn):
        now = _now()
        cur = conn.execute(
            "INSERT INTO meetings (topic, participants, facilitator,"
            " turn_timeout_s, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (topic, json.dumps(slugs), facilitator or "", timeout_s,
             created_by or "", now, now))
        m = _fetch(conn, cur.lastrowid)
        _add_entry(conn, m, "system", "system",
                   "meeting opened by %s: %s; participants %s"
                   % (created_by or "the user", topic, ", ".join(slugs)))
        return m
    m = _tx(root, run)
    # Every participant learns it is in the meeting now: otherwise a
    # cousin not called in the first round has no way to know.
    _notify(slugs, lambda s: OPEN_NOTICE.format(
        id=m["id"], topic=topic, user=created_by or "the user",
        others=", ".join(x for x in slugs if x != s) or "only you",
        order=_order(slugs), place=_place(slugs, s)),
        deliver or default_deliver)
    return m


def post(meeting_id, user, text, *, deliver=None, root=None):
    """The user speaks. Starts a round, or a direct turn for `@slug`."""
    text = _text(text)
    deliver = deliver or default_deliver

    def run(conn):
        m = _fetch(conn, meeting_id)
        if m["state"] != "open":
            raise MeetingError("meeting #%d is %s" % (m["id"], m["state"]))
        if m["mode"] != "floor":
            raise MeetingError("not your floor: %s is speaking"
                               % m["turn_slug"])
        direct = _DIRECT.match(text)
        target = direct.group(1) if direct else None
        if target and target not in m["participants"]:
            raise MeetingError("%s is not in meeting #%d"
                               % (target, m["id"]))
        m["round"] += 1
        _set(conn, m["id"], round=m["round"])
        _add_entry(conn, m, user or "user", "user", text)
        if target:
            _start_turn(conn, m, target, "direct",
                        m["participants"].index(target))
        else:
            _start_turn(conn, m, m["participants"][0], "round", 0)
        _try_deliver(conn, m, deliver)
        return m
    return _tx(root, run)


def _speak(meeting_id, slug, text, kind, *, deliver, root):
    deliver = deliver or default_deliver

    def run(conn):
        m = _fetch(conn, meeting_id)
        if m["state"] == "closed":
            raise MeetingError("meeting #%d is closed" % m["id"])
        if slug != m["turn_slug"]:
            whose = m["turn_slug"] or "the user's"
            raise MeetingError("not your turn in meeting #%d: it is %s"
                               % (m["id"], whose if m["turn_slug"]
                                  else "the user's floor"))
        if kind == "minutes" and m["mode"] != "minutes":
            raise MeetingError("minutes are asked for at closing")
        if kind != "minutes" and m["mode"] == "minutes":
            raise MeetingError("meeting #%d is closing: post the minutes"
                               " with cousin-meeting minutes %d"
                               % (m["id"], m["id"]))
        _add_entry(conn, m, slug, kind, text)
        _advance(conn, m)
        _try_deliver(conn, m, deliver)
        return m
    return _tx(root, run)


def say(meeting_id, slug, text, *, deliver=None, root=None):
    return _speak(meeting_id, slug, _text(text), "cousin",
                  deliver=deliver, root=root)


def pass_turn(meeting_id, slug, *, deliver=None, root=None):
    return _speak(meeting_id, slug, "(pass)", "pass",
                  deliver=deliver, root=root)


def minutes(meeting_id, slug, text, *, deliver=None, root=None):
    m = _speak(meeting_id, slug, _text(text), "minutes",
               deliver=deliver, root=root)
    _notify_closed(m, deliver)
    return m


def _notify_closed(m, deliver):
    if m["state"] == "closed":
        _notify(m["participants"], lambda s: CLOSE_NOTICE.format(
            id=m["id"], topic=m["topic"]), deliver or default_deliver)


def skip(meeting_id, user, *, reason="skipped by the user", deliver=None,
         root=None):
    deliver = deliver or default_deliver

    def run(conn):
        m = _fetch(conn, meeting_id)
        if not m["turn_slug"]:
            raise MeetingError("nobody is speaking in meeting #%d" % m["id"])
        _add_entry(conn, m, "system", "system",
                   "%s skipped (%s)" % (m["turn_slug"], reason))
        _advance(conn, m)
        _try_deliver(conn, m, deliver)
        return m
    return _tx(root, run)


def close(meeting_id, user, *, deliver=None, root=None):
    """With a facilitator the meeting goes to closing and the minutes
    are asked for; without one it closes now. Any turn in progress
    ends."""
    deliver = deliver or default_deliver

    def run(conn):
        m = _fetch(conn, meeting_id)
        if m["state"] != "open":
            raise MeetingError("meeting #%d is already %s"
                               % (m["id"], m["state"]))
        _add_entry(conn, m, "system", "system",
                   "closed by %s" % (user or "the user"))
        if m["facilitator"]:
            _set(conn, m["id"], state="closing")
            m["state"] = "closing"
            _start_turn(conn, m, m["facilitator"], "minutes", -1)
            _try_deliver(conn, m, deliver)
        else:
            _finish(conn, m)
        return m
    m = _tx(root, run)
    _notify_closed(m, deliver)
    return m


def delete(meeting_id, user, *, deliver=None, root=None):
    """Remove a meeting and its transcript. Participants of a meeting
    that was still running are told it is over."""
    def run(conn):
        m = _fetch(conn, meeting_id)
        for table, col in (("entries", "meeting_id"), ("seen", "meeting_id"),
                           ("meetings", "id")):
            conn.execute("DELETE FROM %s WHERE %s=?" % (table, col),
                         (meeting_id,))
        return m
    m = _tx(root, run)
    if m["state"] != "closed":
        _notify(m["participants"], lambda s: CLOSE_NOTICE.format(
            id=m["id"], topic=m["topic"]), deliver or default_deliver)
    return m


def tick(*, deliver=None, is_alive=None, now=None, root=None):
    """Retry undelivered turns; skip a speaker whose session is gone or
    who ran out of time. Returns a list of report lines."""
    deliver = deliver or default_deliver
    alive = is_alive or default_is_alive
    now = now or time.time()
    report = []
    for m in list_meetings(root=root):
        if m["state"] == "closed" or not m["turn_slug"]:
            continue

        def run(conn, mid=m["id"]):
            cur = _fetch(conn, mid)
            slug = cur["turn_slug"]
            if not slug or cur["state"] == "closed":
                return
            reason = None
            if not alive(slug):
                reason = "not running"
            elif now - cur["turn_started"] > cur["turn_timeout_s"]:
                reason = "no answer in %d s" % cur["turn_timeout_s"]
            if reason:
                _add_entry(conn, cur, "system", "system",
                           "%s skipped (%s)" % (slug, reason))
                report.append("meeting #%d: %s skipped (%s)"
                              % (mid, slug, reason))
                _advance(conn, cur)
            if _try_deliver(conn, cur, deliver):
                report.append("meeting #%d: turn delivered to %s"
                              % (mid, cur["turn_slug"]))
        try:
            _tx(root, run)
        except Exception as err:  # noqa: BLE001 - one meeting never costs the rest
            report.append("meeting #%d: tick failed: %s" % (m["id"], err))
    return report


# -- delivery and liveness --------------------------------------------

def default_is_alive(slug):
    """A meeting speaker is alive when its tmux session exists: the
    turn is typed into that session."""
    import subprocess

    try:
        config = CousinConfig.load(
            FrameworkConfig.from_env().root / "cousins" / slug)
        return subprocess.run(
            ["tmux", "has-session", "-t", "=" + config.tmux_session],
            capture_output=True, timeout=5, check=False).returncode == 0
    except Exception:
        return False


def default_deliver(slug, text):
    from cousin_lib.server.injection import TmuxInjector

    config = CousinConfig.load(
        FrameworkConfig.from_env().root / "cousins" / slug)
    return bool(TmuxInjector(config.tmux_session).inject(text))


# -- CLI --------------------------------------------------------------

def _me():
    try:
        return CousinConfig.from_env().slug
    except MissingConfigError:
        raise MeetingError("COUSIN_HOME is not set: say, pass and minutes"
                           " speak as the cousin whose home that is")


def _body(args):
    if getattr(args, "stdin", False):
        return sys.stdin.read()
    if args.text is None:
        raise MeetingError("text required (or --stdin)")
    return args.text


def _print_meeting(m, as_json):
    if as_json:
        print(json.dumps(m, indent=2))
        return
    turn = m["turn_slug"] or "the user's floor"
    print("#%d [%s] %s - round %d, %s; participants %s"
          % (m["id"], m["state"], m["topic"], m["round"], turn,
             ", ".join(m["participants"])))
    for e in m.get("transcript", []):
        print("  %s (r%d) %s: %s" % (e["created_at"][11:16], e["round"],
                                     e["speaker"], e["text"]))


@traced_cli("cousin-meeting")
def meeting_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-meeting",
        description="meetings: a chat shared by the user and several"
                    " running cousins, in rounds")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--state", choices=STATES)
    p = sub.add_parser("show")
    p.add_argument("id", type=int)
    p = sub.add_parser("open")
    p.add_argument("topic")
    p.add_argument("participants", nargs="+")
    p.add_argument("--facilitator", default="")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--user", default="user")
    p = sub.add_parser("post")
    p.add_argument("id", type=int)
    p.add_argument("text", nargs="?")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--user", default="user")
    for name in ("say", "minutes"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        p.add_argument("text", nargs="?")
        p.add_argument("--stdin", action="store_true")
    p = sub.add_parser("pass")
    p.add_argument("id", type=int)
    for name in ("skip", "close", "delete"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        p.add_argument("--user", default="user")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            rows = list_meetings(state=args.state)
            if args.json:
                print(json.dumps(rows, indent=2))
            elif not rows:
                print("(no meetings)")
            for m in [] if args.json else rows:
                print("#%d [%s] %s - %s" % (
                    m["id"], m["state"], m["topic"],
                    m["turn_slug"] or "the user's floor"))
            return 0
        if args.cmd == "show":
            _print_meeting(show(args.id), args.json)
            return 0
        if args.cmd == "open":
            m = open_meeting(args.topic, args.participants,
                             created_by=args.user,
                             facilitator=args.facilitator,
                             timeout_s=args.timeout)
        elif args.cmd == "post":
            m = post(args.id, args.user, _body(args))
        elif args.cmd == "say":
            m = say(args.id, _me(), _body(args))
        elif args.cmd == "pass":
            m = pass_turn(args.id, _me())
        elif args.cmd == "minutes":
            m = minutes(args.id, _me(), _body(args))
        elif args.cmd == "delete":
            m = delete(args.id, args.user)
            print("deleted meeting #%d" % m["id"])
            return 0
        elif args.cmd == "skip":
            m = skip(args.id, args.user)
        else:
            m = close(args.id, args.user)
        _print_meeting(m, args.json)
        return 0
    except MeetingError as err:
        print("cousin-meeting: %s" % err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(meeting_main())
