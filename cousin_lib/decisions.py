"""Decision register: the questions a cousin asks the operator, kept in
one place until they are answered (docs/decisions.md).

A cousin asks with the `decision` tool (or `cousin-decision ask`): one
question, its options, the option it recommends and why. The operator
answers in the console's Decisions view (or `cousin-decision answer` in
their own terminal). An answer is recorded first, then it reaches the
cousin twice: an operator-level memory in its home, and a turn on its
operator thread. Neither can lose the answer: a memory or a delivery
that failed is left marked undone and `redeliver()` retries it (the
console calls it when the view loads, the CLI has a subcommand).

Decisions that share a group (`grp`) come back as ONE turn, once every
decision of that cousin and group is answered or withdrawn; the memory
is still written per decision when it is answered.

The store is <root>/data/decisions.db, one for the install, opened the
way tracker.py opens its own: WAL with retries, BEGIN IMMEDIATE around
every write, AUTOINCREMENT ids, and a history table that outlives its
rows.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.trace import traced_cli

STATUSES = ("open", "answered", "withdrawn")
SETTLED = ("answered", "withdrawn")
FIELDS = ("id", "cousin", "question", "options", "recommendation", "why", "grp",
          "skill", "stop", "status", "answer", "answer_option", "answered_by",
          "answered_at", "answered_via", "remembered_at", "delivered_at",
          "created_at", "updated_at")
_KEY = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
# "a: text" is keyed only for a short key: "https://x" or "Note: y" is text
_SHORT_KEY = re.compile(r"^[A-Za-z0-9]{1,3}$")
_LETTERS = "abcdefghijklmnopqrstuvwxyz"
TOPIC_CHARS = 60
# A group's answers go back once all of it is settled AND its newest
# question is this old: with one question per call, the operator could
# otherwise settle a group before the cousin asked its next question.
GROUP_GRACE_S = 60
# the console's poller leaves an answer this young to the request that
# recorded it, so the two never race for its first delivery
ANSWER_GRACE_S = 30
# The memory and the turn an answer owes are claimed under BEGIN
# IMMEDIATE with a lease ("pending:<token>:<iso>" in remembered_at or
# delivered_at); only the claimant writes, and a lease this old is taken
# over (its claimant died between the claim and the release).
LEASE_S = 300
PENDING = "pending:"
# An answer that can never reach its cousin (its home is gone, or its
# delivery is refused: a worker, no runner kind) is marked
# "gone:<iso>:<reason>" instead of owed, so nothing retries it on its own;
# a manual retry (redeliver(retry_gone=True)) clears the mark first.
GONE = "gone:"
# `answer` is the operator's act; a cousin asks, it never answers itself.
ANSWER_REFUSAL = ("answering a decision is the operator's act: they answer in the console's"
                  " Decisions view, or with `cousin-decision answer` in their own terminal")


class DecisionError(ValueError):
    """A refused call: blank question, unknown option, a settled decision."""


class DecisionNotFound(DecisionError, KeyError):
    """The id names no decision."""

    def __str__(self):
        return "decision #%s not found" % (self.args[0],)


def db_path(root=None):
    """<root>/data/decisions.db; root is the flag value, else FRAMEWORK_ROOT."""
    return FrameworkConfig.resolve(root).root / "data" / "decisions.db"


def _db(root):
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # As tracker._db: the journal-mode switch takes no busy handler, so
    # it is retried briefly and skipped if it still refuses.
    for attempt in range(20):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as err:
            if "locked" not in str(err) or attempt == 19:
                break
            time.sleep(0.05 * (attempt + 1))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decisions ("
        " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
        " cousin         TEXT NOT NULL,"
        " question       TEXT NOT NULL,"
        " options        TEXT NOT NULL DEFAULT '[]',"
        " recommendation TEXT NOT NULL DEFAULT '',"
        " why            TEXT NOT NULL DEFAULT '',"
        " grp            TEXT NOT NULL DEFAULT '',"
        " skill          TEXT NOT NULL DEFAULT '',"
        " stop           TEXT NOT NULL DEFAULT '',"
        " status         TEXT NOT NULL DEFAULT 'open',"
        " answer         TEXT,"
        " answer_option  TEXT,"
        " answered_by    TEXT,"
        " answered_at    TEXT,"
        " answered_via   TEXT,"
        " remembered_at  TEXT,"
        " delivered_at   TEXT,"
        " created_at     TEXT NOT NULL,"
        " updated_at     TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_status"
                 " ON decisions(status, cousin)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS history ("
        " id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        " decision_id INTEGER NOT NULL,"
        " field       TEXT NOT NULL,"
        " old         TEXT,"
        " new         TEXT,"
        " who         TEXT NOT NULL DEFAULT '',"
        " at          TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_decision"
                 " ON history(decision_id, id)")
    # The store's own id, made once: a delivery key carries it, so a
    # store restored or made anew never reuses a key an inbox still holds.
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if conn.execute("SELECT 1 FROM meta WHERE key='store_id'").fetchone() is None:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('store_id', ?)",
                      (uuid.uuid4().hex,))
    return conn


def _clock():
    return datetime.now(timezone.utc)


def _now():
    return _clock().isoformat(timespec="seconds")


def done(value):
    """A remembered_at or delivered_at that says done (a time), not owed
    (null), not claimed (a pending lease) and not undeliverable (gone)."""
    return bool(value) and not str(value).startswith((PENDING, GONE))


def gone_reason(value):
    """Why a `gone:` mark says the work cannot be done; None for any other."""
    if not str(value or "").startswith(GONE):
        return None
    rest = str(value)[len(GONE):]
    return rest[len(_now()) + 1:] or "undeliverable"


def _gone(reason):
    return "%s%s:%s" % (GONE, _now(), " ".join(str(reason).split())[:200])


def _owed(value, now):
    """Null, or a lease older than LEASE_S: free to claim. A `gone:` mark
    is not: only a manual retry clears it."""
    if not value:
        return True
    if not str(value).startswith(PENDING):
        return False
    _token, _, stamp = str(value)[len(PENDING):].partition(":")
    try:
        since = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return now - since > timedelta(seconds=LEASE_S)


def _record(conn, decision_id, field, old, new, who, at):
    conn.execute(
        "INSERT INTO history (decision_id, field, old, new, who, at)"
        " VALUES (?, ?, ?, ?, ?, ?)", (decision_id, field, old, new, who, at))


def _row(row):
    out = dict(row)
    try:
        out["options"] = json.loads(out.get("options") or "[]")
    except ValueError:
        out["options"] = []
    # what an answer still owes, said plainly for the view
    out["remembered"] = done(out.get("remembered_at"))
    out["delivered"] = done(out.get("delivered_at"))
    reasons = [r for r in (gone_reason(out.get("remembered_at")),
                           gone_reason(out.get("delivered_at"))) if r]
    out["undeliverable"] = "; ".join(dict.fromkeys(reasons))
    return out


def _fetch(conn, decision_id):
    row = conn.execute("SELECT * FROM decisions WHERE id=?",
                       (decision_id,)).fetchone()
    if row is None:
        raise DecisionNotFound(decision_id)
    return _row(row)


def _write(root, work):
    """Run work(conn) under BEGIN IMMEDIATE; commit and return its value."""
    conn = _db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        out = work(conn)
        conn.execute("COMMIT")
        return out
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def env_cousin():
    """The calling cousin's slug from COUSIN_HOME, else ''."""
    try:
        return CousinConfig.from_env().slug
    except MissingConfigError:
        return ""


# ---- options ---------------------------------------------------------

def parse_options(options):
    """[{"key", "text"}] from strings "a: text" or {"key", "text"} objects.
    A string with no short key before a colon (no colon, or a space in
    what comes before it) is all text and gets the next free letter."""
    parsed = []
    for raw in options or ():
        if isinstance(raw, dict):
            key, text = str(raw.get("key") or "").strip(), str(raw.get("text") or "").strip()
        else:
            raw = str(raw)
            head, sep, tail = raw.partition(":")
            if sep and _SHORT_KEY.match(head.strip()):
                key, text = head.strip(), tail.strip()
            else:
                key, text = "", raw.strip()
        if not text:
            raise DecisionError("option %r has no text" % (raw,))
        if key and not _KEY.match(key):
            raise DecisionError("option key %r: 1-16 letters, digits, - or _" % key)
        parsed.append([key, text])
    taken = {k for k, _ in parsed if k}
    letters = iter(_LETTERS)
    for item in parsed:
        if not item[0]:
            item[0] = next((c for c in letters if c not in taken), None)
            if item[0] is None:
                raise DecisionError("too many options without a key (the letters a-z ran"
                                    " out): give them keys, \"k: text\"")
            taken.add(item[0])
    keys = [k for k, _ in parsed]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise DecisionError("option keys must be unique, not %s" % ", ".join(dupes))
    return [{"key": k, "text": t} for k, t in parsed]


def option_text(row, key):
    for opt in row.get("options") or ():
        if opt["key"] == key:
            return opt["text"]
    return None


def answer_text(row):
    """The answer as it is said back: 'a: option text', with a free-text
    note after it when there is one, or the free text alone."""
    key, free = row.get("answer_option"), (row.get("answer") or "").strip()
    if key:
        said = "%s: %s" % (key, option_text(row, key) or "")
        return said if not free or free == option_text(row, key) else "%s (%s)" % (said, free)
    return free


def recommendation_text(row):
    rec = (row.get("recommendation") or "").strip()
    text = option_text(row, rec)
    return "%s: %s" % (rec, text) if text else rec


# ---- the API ---------------------------------------------------------

def ask(cousin, question, *, options=(), recommendation="", why="", group="",
        skill="", stop="", root=None):
    """Open one decision for the operator and return it."""
    cousin = str(cousin or "").strip()
    question = str(question or "").strip()
    if not cousin:
        raise DecisionError("no cousin: ask from a cousin's environment (COUSIN_HOME)")
    if not question:
        raise DecisionError("question required")
    opts = parse_options(options)
    skill, stop = str(skill or "").strip(), str(stop or "").strip()
    group = str(group or "").strip()
    stamp = _clock()
    now = stamp.isoformat(timespec="seconds")

    def work(conn):
        grp = group
        if skill and stop and grp in ("", "%s:%s" % (skill, stop)):
            grp = _stop_run(conn, cousin, skill, stop, stamp)
        cur = conn.execute(
            "INSERT INTO decisions (cousin, question, options, recommendation, why,"
            " grp, skill, stop, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (cousin, question, json.dumps(opts), str(recommendation or "").strip(),
             str(why or "").strip(), grp, skill, stop, now, now))
        _record(conn, cur.lastrowid, "created", None, question, cousin, now)
        return _fetch(conn, cur.lastrowid)
    return _write(root, work)


def _stop_run(conn, cousin, skill, stop, stamp):
    """The group of a skill stop's question: `<skill>:<stop>@<run>`. The
    run is the one this cousin has under way at that stop (a question of
    it still open, or an answer not yet sent), else a new one named for
    the ask time to the minute. The tool context has no id for the asking
    turn on the stdio lane, so both lanes use this one rule; a stop that a
    later run reaches again is a new group, never the old one's."""
    prefix = "%s:%s@" % (skill, stop)
    row = conn.execute(
        "SELECT grp FROM decisions WHERE cousin=? AND substr(grp, 1, ?)=?"
        " AND (status='open' OR (status='answered' AND (delivered_at IS NULL"
        " OR delivered_at LIKE 'pending:%'))) ORDER BY id DESC LIMIT 1",
        (cousin, len(prefix), prefix)).fetchone()
    return row[0] if row else prefix + stamp.strftime("%Y-%m-%dT%H:%M")


def list_decisions(*, cousin=None, status=None, group=None, root=None):
    """Decisions matching every given filter: open ones first, oldest
    first, then the settled ones, newest first."""
    if status and status not in STATUSES:
        raise DecisionError("status must be one of %s, not %r" % ("|".join(STATUSES), status))
    where, args = ["1=1"], []
    for col, value in (("cousin", cousin), ("status", status), ("grp", group)):
        if value:
            where.append("%s=?" % col)
            args.append(value)
    conn = _db(root)
    try:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE %s ORDER BY (status = 'open') DESC,"
            " CASE WHEN status = 'open' THEN id ELSE -id END" % " AND ".join(where),
            args).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def show(decision_id, *, root=None):
    """The decision, or None."""
    conn = _db(root)
    try:
        return _fetch(conn, decision_id)
    except DecisionNotFound:
        return None
    finally:
        conn.close()


def history(decision_id, *, root=None):
    """The decision's changes, oldest first."""
    conn = _db(root)
    try:
        rows = conn.execute("SELECT * FROM history WHERE decision_id=? ORDER BY id",
                            (decision_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def open_summary(root=None):
    """{cousin: {"open": n, "stops": ["skill:stop", ...]}} over the open
    decisions; {} when the install has no store yet (a read never creates
    one)."""
    if not db_path(root).is_file():
        return {}
    out = {}
    for row in list_decisions(status="open", root=root):
        entry = out.setdefault(row["cousin"], {"open": 0, "stops": []})
        entry["open"] += 1
        if row["skill"] and row["stop"]:
            stop = "%s:%s" % (row["skill"], row["stop"])
            if stop not in entry["stops"]:
                entry["stops"].append(stop)
    return out


def signature(root=None):
    """(rows, newest change, open count): what the console's event
    poller compares; None when there is no store yet."""
    if not db_path(root).is_file():
        return None
    conn = _db(root)
    try:
        row = conn.execute("SELECT COUNT(*), MAX(updated_at),"
                           " COALESCE(SUM(status = 'open'), 0) FROM decisions").fetchone()
    finally:
        conn.close()
    return tuple(row)


def answer(decision_id, *, answer=None, option=None, user, via="the CLI", root=None):
    """The operator's answer: an option key, free text, or both. The row
    is answered first and committed; the memory and the turn come after
    and never undo it (a failed one is retried by redeliver). Returns the
    row with `delivery`: "delivered", "waiting" (its group has open
    decisions) or "pending" (it failed and waits for redeliver)."""
    free = str(answer or "").strip()
    key = str(option or "").strip()
    if not free and not key:
        raise DecisionError("an answer needs an option or a text")
    who = str(user or "").strip() or "operator"
    now = _now()

    def work(conn):
        row = _fetch(conn, decision_id)
        if row["status"] != "open":
            raise DecisionError("decision #%d is %s" % (row["id"], row["status"]))
        if key and option_text(row, key) is None:
            known = ", ".join(o["key"] for o in row["options"]) or "none"
            raise DecisionError("decision #%d has no option %r (options: %s)"
                                % (row["id"], key, known))
        text = free or option_text(row, key)
        conn.execute(
            "UPDATE decisions SET status='answered', answer=?, answer_option=?,"
            " answered_by=?, answered_at=?, answered_via=?, updated_at=? WHERE id=?",
            (text, key or None, who, now, via, now, row["id"]))
        _record(conn, row["id"], "answered", None,
                answer_text(dict(row, answer=text, answer_option=key or None)), who, now)
        return _fetch(conn, row["id"])
    row = _write(root, work)
    _remember(root, row)
    _settle_quietly(root, row["cousin"], row["grp"])
    return _with_delivery(root, row["id"])


def withdraw(decision_id, *, cousin, root=None):
    """The asking cousin takes an open question back. Withdrawing the last
    open decision of a group sends the group's answers."""
    cousin = str(cousin or "").strip()
    now = _now()

    def work(conn):
        row = _fetch(conn, decision_id)
        if row["cousin"] != cousin:
            raise DecisionError("decision #%d was asked by another cousin: only the cousin"
                                " that asked withdraws it" % row["id"])
        if row["status"] != "open":
            raise DecisionError("decision #%d is %s" % (row["id"], row["status"]))
        conn.execute("UPDATE decisions SET status='withdrawn', updated_at=? WHERE id=?",
                     (now, row["id"]))
        _record(conn, row["id"], "withdrawn", "open", "withdrawn", cousin, now)
        return _fetch(conn, row["id"])
    row = _write(root, work)
    _settle_quietly(root, row["cousin"], row["grp"])
    return row


def owed(root=None):
    """Whether any answer still owes a memory or a turn; False when there
    is no store (a read never creates one)."""
    if not db_path(root).is_file():
        return False
    conn = _db(root)
    try:
        return conn.execute(
            "SELECT 1 FROM decisions WHERE status='answered' AND (remembered_at IS NULL"
            " OR remembered_at LIKE 'pending:%' OR delivered_at IS NULL"
            " OR delivered_at LIKE 'pending:%') LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def _young(row, now, min_age):
    """Answered less than min_age seconds ago."""
    if not min_age or not row.get("answered_at"):
        return False
    try:
        at = datetime.fromisoformat(row["answered_at"])
    except ValueError:
        return False
    return (now - at).total_seconds() < min_age


def redeliver(*, retry_gone=False, min_age=0, root=None):
    """Retry every memory and turn an answer still owes (the console's
    event poller calls it while anything is owed). The manual retry (the
    CLI's, the console's button) passes retry_gone, which first clears the
    `gone:` marks of what could not be delivered. Returns [{"ids": [...],
    "ok": bool}] for the turns tried."""
    if retry_gone:
        def clear(conn):
            for column in ("remembered_at", "delivered_at"):
                conn.execute("UPDATE decisions SET %s=NULL WHERE %s LIKE 'gone:%%'"
                             % (column, column))
        _write(root, clear)
    now = _clock()
    for row in list_decisions(status="answered", root=root):
        if not done(row["remembered_at"]) and not _young(row, now, min_age):
            _remember(root, row)
    return _settle(root, None, None, min_age=min_age)


# ---- after an answer ---------------------------------------------------

def _home(root, cousin):
    """The cousin's home, or None when it has no cousin.toml: nothing is
    written into a directory that is not a home."""
    home = FrameworkConfig.resolve(root).root / "cousins" / cousin
    return home if (home / "cousin.toml").is_file() else None


def cite(row):
    return "decision register #%d, answered by %s at %s in %s" % (
        row["id"], row["answered_by"], row["answered_at"], row["answered_via"] or "the CLI")


def memory_fact(row):
    fact = "Asked: %s Answer: %s." % (row["question"], answer_text(row))
    rec = recommendation_text(row)
    if rec:
        fact += " Asked with the recommendation: %s." % rec
    return fact


def _claim(root, column, ids):
    """Claim `column` (remembered_at or delivered_at) of these rows under
    BEGIN IMMEDIATE: the lease when every one of them is owed (_owed),
    else None (done, or another process holds it)."""
    now = _clock()
    lease = "%s%s:%s" % (PENDING, uuid.uuid4().hex[:12], now.isoformat(timespec="seconds"))
    marks = ",".join("?" * len(ids))

    def work(conn):
        values = [r[0] for r in conn.execute(
            "SELECT %s FROM decisions WHERE id IN (%s)" % (column, marks), ids)]
        if len(values) != len(ids) or not all(_owed(v, now) for v in values):
            return None
        conn.execute("UPDATE decisions SET %s=? WHERE id IN (%s)" % (column, marks),
                     [lease] + list(ids))
        return lease
    return _write(root, work)


def _release(root, column, ids, lease, ok):
    """End a claim: the time when the work was done (ok True), null to
    owe it again (False), or a `gone:` mark (ok a reason string). Only rows
    still under this lease change."""
    value = _now() if ok is True else (_gone(ok) if ok else None)
    _write(root, lambda conn: conn.execute(
        "UPDATE decisions SET %s=? WHERE %s=? AND id IN (%s)"
        % (column, column, ",".join("?" * len(ids))),
        [value, lease] + list(ids)))


def memory_topic(row):
    """`decision #<id>: <the question's first 60 characters>`, on one line."""
    return "decision #%d: %s" % (row["id"], " ".join(row["question"].split())[:TOPIC_CHARS])


def _remember(root, row):
    """The operator-level memory of one answer, written by the one process
    that claims it. Never raises: a failure is released as owed, for
    redeliver."""
    try:
        home = _home(root, row["cousin"])
        lease = _claim(root, "remembered_at", [row["id"]])
    except Exception:  # noqa: BLE001 - the answer is kept; redeliver retries
        return False
    if lease is None:
        return False
    if home is None:
        try:
            _release(root, "remembered_at", [row["id"]], lease, NO_HOME)
        except Exception:  # noqa: BLE001 - the lease runs out and the work is retried
            pass
        return False
    ok = False
    try:
        from cousin_lib import memory
        memory.remember(home, memory_topic(row), memory_fact(row), level="operator",
                        cite=cite(row))
        ok = True
    except Exception:  # noqa: BLE001 - released as owed below
        pass
    try:
        _release(root, "remembered_at", [row["id"]], lease, ok)
    except Exception:  # noqa: BLE001 - the lease runs out and the work is retried
        pass
    return ok


def turn_body(rows):
    """The turn for one answered decision, or for a whole group."""
    lines = ["Decision #%d answered: %s -> %s" % (r["id"], r["question"], answer_text(r))
             for r in rows]
    if len(rows) == 1 and not rows[0]["grp"]:
        return lines[0]
    first = rows[0]
    if first["skill"] and first["stop"]:
        head = "Decisions answered at skill %s, stop %s (group %s):" % (
            first["skill"], first["stop"], first["grp"])
    else:
        head = "Decisions answered (group %s):" % first["grp"]
    return "\n".join([head] + lines)


NO_HOME = "the cousin's home is gone"


def _deliver(root, rows):
    """One turn for these answered rows on the cousin's operator thread
    (system when it has none). True when the cousin took it, False to try
    again, or why it never can (no home; its delivery is refused: a worker,
    no runner kind). Never raises."""
    try:
        from cousin_lib import delivery
        home = _home(root, rows[0]["cousin"])
        if home is None:
            return NO_HOME
        backend = delivery.backend_for(home)
        if isinstance(backend, delivery.RefusedBackend):
            return "its delivery is refused: %s" % backend.reason(home)
        operator = CousinConfig.load(home).operator_name
        thread = (delivery.thread_id("operator", operator) if operator
                  else delivery.thread_id("system"))
        item = delivery.Item(thread_id=thread, source="decision", body=turn_body(rows),
                             sender=rows[-1]["answered_by"] or "operator")
        wait = not isinstance(backend, delivery.InboxBackend)
        # keyed: a retry after a put whose outcome was never recorded is
        # the first row, not a second turn
        with delivery.keyed("decision:%s:%s" % (store_id(root),
                                                ",".join(str(r["id"]) for r in rows))):
            return bool(delivery.accepted(delivery.deliver(home, item, wait=wait), home))
    except Exception:  # noqa: BLE001 - the answer is kept; redeliver retries
        return False


def store_id(root=None):
    """The store's own id (the meta table's, made once)."""
    conn = _db(root)
    try:
        return conn.execute("SELECT value FROM meta WHERE key='store_id'").fetchone()[0]
    finally:
        conn.close()


def _units(root, cousin, grp, min_age=0):
    """The turns owed: answered rows whose turn is not done, one unit per
    ungrouped row and one per settled group: no decision of it open, and
    its newest question older than GROUP_GRACE_S."""
    where, args = ["status='answered'",
                   "(delivered_at IS NULL OR delivered_at LIKE 'pending:%')"], []
    if cousin is not None:
        where.append("cousin=?"); args.append(cousin)
    if grp is not None:
        where.append("grp=?"); args.append(grp)
    conn = _db(root)
    try:
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM decisions WHERE %s ORDER BY id" % " AND ".join(where), args)]
        units, groups = [], {}
        for row in rows:
            if not row["grp"]:
                units.append([row])
            else:
                groups.setdefault((row["cousin"], row["grp"]), []).append(row)
        for (who, name), members in groups.items():
            if _group_due(conn, who, name):
                units.append(members)
    finally:
        conn.close()
    now = _clock()
    units = [u for u in units if not any(_young(r, now, min_age) for r in u)]
    return sorted(units, key=lambda u: u[0]["id"])


def _group_due(conn, cousin, grp):
    """No decision of the group open, and its newest question asked more
    than GROUP_GRACE_S ago."""
    still_open, newest = conn.execute(
        "SELECT COALESCE(SUM(status='open'), 0), MAX(created_at) FROM decisions"
        " WHERE cousin=? AND grp=?", (cousin, grp)).fetchone()
    if still_open or not newest:
        return False
    return _clock() - datetime.fromisoformat(newest) >= timedelta(seconds=GROUP_GRACE_S)


def _settle(root, cousin, grp, min_age=0):
    """Deliver every unit owed (for one cousin and group, or all of them),
    each by the one process that claims its rows."""
    out = []
    for unit in _units(root, cousin, grp, min_age):
        ids = [r["id"] for r in unit]
        lease = _claim(root, "delivered_at", ids)
        if lease is None:
            continue                  # done meanwhile, or another process has it
        outcome = _deliver(root, unit)
        try:
            _release(root, "delivered_at", ids, lease, outcome)
        except Exception:  # noqa: BLE001 - the lease runs out; the keyed put is a no-op
            pass
        out.append({"ids": ids, "ok": outcome is True})
    return out


def _settle_quietly(root, cousin, grp):
    """_settle after an answer or a withdrawal that is already committed:
    a busy store must not turn it into an error; the retry catches up."""
    try:
        _settle(root, cousin, grp)
    except Exception:  # noqa: BLE001 - the console's poller and redeliver retry
        pass


def _with_delivery(root, decision_id):
    row = show(decision_id, root=root)
    if row["delivered"]:
        row["delivery"] = "delivered"
    elif gone_reason(row["delivered_at"]):
        row["delivery"] = "undeliverable"
    elif row["grp"]:
        conn = _db(root)
        try:
            due = _group_due(conn, row["cousin"], row["grp"])
        finally:
            conn.close()
        row["delivery"] = "pending" if due else "waiting"
    else:
        row["delivery"] = "pending"
    return row


# ---- text, shared by the CLI and the in-process tool --------------------

def row_line(row):
    """'#3 [open] question (group g)': one decision on one line."""
    extra = []
    if row["grp"]:
        extra.append("group %s" % row["grp"])
    if row["skill"]:
        extra.append("skill %s, stop %s" % (row["skill"], row["stop"] or "-"))
    line = "#%d [%s] %s: %s" % (row["id"], row["status"], row["cousin"], row["question"])
    if extra:
        line += " (%s)" % "; ".join(extra)
    if row["status"] == "answered":
        line += " -> %s" % answer_text(row)
    return line


def asked_text(row):
    return ("asked decision #%d: %s\nEnd your turn now: the answer comes back as a turn,"
            " and is written to your memory." % (row["id"], row["question"]))


def list_text(rows):
    return "\n".join(row_line(r) for r in rows) if rows else "(no decisions)"


def show_text(row):
    lines = [row_line(row)]
    for opt in row["options"]:
        mark = " (recommended)" if opt["key"] == row["recommendation"] else ""
        lines.append("  %s: %s%s" % (opt["key"], opt["text"], mark))
    if row["recommendation"] and not option_text(row, row["recommendation"]):
        lines.append("  recommended: %s" % row["recommendation"])
    if row["why"]:
        lines.append("  why: %s" % row["why"])
    if row["status"] == "answered":
        lines.append("  answered by %s at %s in %s; memory: %s; turn: %s"
                     % (row["answered_by"], row["answered_at"], row["answered_via"],
                        row["remembered_at"] if row["remembered"] else
                        ("undeliverable: %s" % gone_reason(row["remembered_at"])
                         if gone_reason(row["remembered_at"]) else "not written"),
                        row["delivered_at"] if row["delivered"] else
                        ("undeliverable: %s" % gone_reason(row["delivered_at"])
                         if gone_reason(row["delivered_at"]) else "not delivered")))
    return "\n".join(lines)


def answered_text(row):
    said = {"delivered": "delivered to %s" % row["cousin"],
            "waiting": "the group's turn waits until all of it is answered and its newest"
                       " question is %d s old" % GROUP_GRACE_S,
            "pending": "delivery failed and is kept: `cousin-decision redeliver` retries it",
            "undeliverable": "undeliverable (%s): `cousin-decision redeliver` tries again"
                             % row.get("undeliverable")}
    return "answered #%d -> %s; %s" % (row["id"], answer_text(row),
                                       said.get(row.get("delivery"), ""))


def withdrawn_text(row):
    return "withdrew decision #%d" % row["id"]


# ---- CLI -------------------------------------------------------------

def _print(args, payload, text):
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else text)


def _cmd_ask(args):
    cousin = env_cousin()
    row = ask(cousin, args.question, options=args.option or (),
              recommendation=args.recommendation or "", why=args.why or "",
              group=args.group or "", skill=args.skill or "", stop=args.stop or "",
              root=args.root)
    _print(args, {"decision": row}, asked_text(row))
    return 0


def _cmd_list(args):
    status = None if args.status == "all" else (args.status or "open")
    rows = list_decisions(cousin=args.cousin or env_cousin() or None, status=status,
                          group=args.group, root=args.root)
    _print(args, {"decisions": rows}, list_text(rows))
    return 0


def _cmd_show(args):
    row = show(args.id, root=args.root)
    if row is None:
        raise DecisionNotFound(args.id)
    _print(args, {"decision": row, "history": history(args.id, root=args.root)},
           show_text(row))
    return 0


def _cmd_withdraw(args):
    cousin = env_cousin()
    if not cousin:
        raise DecisionError("withdraw is the asking cousin's: run it in its environment")
    row = withdraw(args.id, cousin=cousin, root=args.root)
    _print(args, {"decision": row}, withdrawn_text(row))
    return 0


def _cmd_answer(args):
    if os.environ.get("COUSIN_HOME"):
        raise DecisionError(ANSWER_REFUSAL)
    row = answer(args.id, answer=args.answer, option=args.option, user=args.user,
                 via="the CLI", root=args.root)
    _print(args, {"decision": row}, answered_text(row))
    return 0


def _cmd_redeliver(args):
    tried = redeliver(retry_gone=True, root=args.root)
    lines = ["%s #%s" % ("delivered" if t["ok"] else "still pending",
                         ", #".join(str(i) for i in t["ids"])) for t in tried]
    _print(args, {"tried": tried}, "\n".join(lines) or "nothing to deliver")
    return 0


@traced_cli("cousin-decision")
def decision_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-decision",
        description="the decision register: a cousin asks, the operator answers")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--json", action="store_true")
        p.add_argument("--root", default=None, help="framework root (else FRAMEWORK_ROOT)")

    p = sub.add_parser("ask", help="ask the operator one question (a cousin's act)")
    p.add_argument("question")
    p.add_argument("--option", action="append",
                   help='one option, "key: text" (repeat for each; a bare text gets a letter)')
    p.add_argument("--recommendation", help="the option key (or text) you recommend")
    p.add_argument("--why", help="why it is asked, and why the recommendation")
    p.add_argument("--group", help="questions that belong together come back as one turn")
    p.add_argument("--skill", help="the skill whose stop point asks it")
    p.add_argument("--stop", help="the stop point's name")
    common(p)
    p = sub.add_parser("list", help="decisions; in a cousin's environment its own")
    p.add_argument("--status", choices=STATUSES + ("all",), help="default open")
    p.add_argument("--group")
    p.add_argument("--cousin", help="one cousin's (default: COUSIN_HOME's, else all)")
    common(p)
    p = sub.add_parser("show", help="one decision, every field")
    p.add_argument("id", type=int)
    common(p)
    p = sub.add_parser("withdraw", help="take an open question back (the asking cousin's)")
    p.add_argument("id", type=int)
    common(p)
    p = sub.add_parser("answer", help="answer a decision: the operator's act, refused inside"
                                      " a cousin's environment (COUSIN_HOME)")
    p.add_argument("id", type=int)
    p.add_argument("--option", help="the option key chosen")
    p.add_argument("--answer", help="free text (with or without an option)")
    p.add_argument("--user", default="operator", help="who answers (default operator)")
    common(p)
    p = sub.add_parser("redeliver", help="retry the memories and turns answers still owe")
    common(p)
    args = parser.parse_args(argv)
    handlers = {"ask": _cmd_ask, "list": _cmd_list, "show": _cmd_show,
                "withdraw": _cmd_withdraw, "answer": _cmd_answer, "redeliver": _cmd_redeliver}
    try:
        args.root = str(FrameworkConfig.for_command(args.root).root)
        return handlers[args.cmd](args)
    except DecisionNotFound as err:
        print("cousin-decision: %s" % err, file=sys.stderr)
        return 1
    except (DecisionError, MissingConfigError) as err:
        print("cousin-decision: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(decision_main())
