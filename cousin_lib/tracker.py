"""In-flight work tracker: one framework-wide list of what is being
worked on, by whom, in what state.

The store is this module's own SQLite database at <root>/data/tracker.db.
Adding an item works with no service running anywhere; the console
serves the same database through the functions below and is a view of
it, never its owner. Every call opens its own connection and every
mutation runs under BEGIN IMMEDIATE, so cousins and the console can
write at the same time without a "database is locked" escaping. Ids
are AUTOINCREMENT: a deleted id is never handed out again, so a link
to #3 in a note or a chat line cannot come to mean another item.
"""
import argparse
import json
import sqlite3
import time
import sys
from datetime import datetime, timezone

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.trace import traced_cli

STATES = ("open", "active", "blocked", "done", "dropped")
CLOSED = ("done", "dropped")
FIELDS = ("id", "title", "domain", "state", "tags", "owner", "notes",
          "created_at", "updated_at")


class TrackerError(ValueError):
    """A refused call: bad state, blank title, nothing to update."""


class ItemNotFound(TrackerError, KeyError):
    """The id names no item."""

    def __str__(self):
        return "tracker item #%s not found" % (self.args[0],)


def db_path(root=None):
    """<root>/data/tracker.db; root is the flag value, else FRAMEWORK_ROOT."""
    return FrameworkConfig.resolve(root).root / "data" / "tracker.db"


def _db(root):
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # A journal-mode switch needs an exclusive lock and SQLite does not
    # run the busy handler for it: with several processes opening the
    # store at once (the first write of a fresh install, the
    # concurrency test) it raises "database is locked" straight away.
    # WAL is a performance choice, not a correctness one, so retry
    # briefly and carry on in rollback mode if it still refuses.
    for attempt in range(20):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as err:
            if "locked" not in str(err) or attempt == 19:
                break
            time.sleep(0.05 * (attempt + 1))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS items ("
        " id         INTEGER PRIMARY KEY AUTOINCREMENT,"
        " title      TEXT NOT NULL,"
        " domain     TEXT NOT NULL DEFAULT '',"
        " state      TEXT NOT NULL DEFAULT 'open',"
        " tags       TEXT NOT NULL DEFAULT '[]',"
        " owner      TEXT NOT NULL DEFAULT '',"
        " notes      TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_state ON items(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_domain ON items(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_owner ON items(owner)")
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_state(state):
    if state not in STATES:
        raise TrackerError("state must be one of %s, not %r"
                           % ("|".join(STATES), state))
    return state


def _clean_tags(tags):
    out = []
    for tag in tags or ():
        tag = str(tag).strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def _row(row):
    item = dict(row)
    try:
        item["tags"] = json.loads(item.get("tags") or "[]")
    except ValueError:
        item["tags"] = []
    return item


def _default_owner():
    try:
        return CousinConfig.from_env().slug
    except MissingConfigError:
        return ""


def _fetch(conn, item_id):
    row = conn.execute("SELECT * FROM items WHERE id=?",
                       (item_id,)).fetchone()
    if row is None:
        raise ItemNotFound(item_id)
    return _row(row)


def add(title, *, domain="", state="open", tags=(), owner=None, notes="",
        root=None):
    """Insert an item and return it in the full item shape."""
    title = (title or "").strip()
    if not title:
        raise TrackerError("title required")
    _check_state(state)
    owner = _default_owner() if owner is None else str(owner)
    now = _now()
    conn = _db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO items (title, domain, state, tags, owner, notes,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, domain or "", state, json.dumps(_clean_tags(tags)),
             owner, notes or "", now, now),
        )
        item = _fetch(conn, cur.lastrowid)
        conn.execute("COMMIT")
        return item
    finally:
        conn.close()


def update(item_id, *, title=None, domain=None, state=None, tags=None,
           owner=None, notes=None, root=None):
    """Change the named fields only; None means untouched. Raises
    ItemNotFound for a missing id and TrackerError when nothing is
    named or the state is not one of STATES."""
    sets, args = [], []
    if title is not None:
        title = title.strip()
        if not title:
            raise TrackerError("title required")
        sets.append("title=?"); args.append(title)
    if domain is not None:
        sets.append("domain=?"); args.append(domain)
    if state is not None:
        sets.append("state=?"); args.append(_check_state(state))
    if tags is not None:
        sets.append("tags=?"); args.append(json.dumps(_clean_tags(tags)))
    if owner is not None:
        sets.append("owner=?"); args.append(owner)
    if notes is not None:
        sets.append("notes=?"); args.append(notes)
    if not sets:
        raise TrackerError("nothing to update")
    sets.append("updated_at=?"); args.append(_now())
    conn = _db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _fetch(conn, item_id)
        conn.execute("UPDATE items SET %s WHERE id=?" % ", ".join(sets),
                     args + [item_id])
        item = _fetch(conn, item_id)
        conn.execute("COMMIT")
        return item
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def set_state(item_id, state, *, root=None):
    """Move an item to one of STATES and return it."""
    return update(item_id, state=_check_state(state), root=root)


def list_items(domain=None, state=None, tag=None, *, owner=None, root=None):
    """Items matching every given filter, open ones first, most
    recently updated first within each half."""
    where, args = ["1=1"], []
    if domain:
        where.append("domain=?"); args.append(domain)
    if state:
        where.append("state=?"); args.append(_check_state(state))
    if owner:
        where.append("owner=?"); args.append(owner)
    conn = _db(root)
    try:
        rows = conn.execute(
            "SELECT * FROM items WHERE %s"
            " ORDER BY (state IN ('done', 'dropped')) ASC,"
            " updated_at DESC, id DESC" % " AND ".join(where),
            args,
        ).fetchall()
    finally:
        conn.close()
    items = [_row(r) for r in rows]
    if tag:
        items = [i for i in items if tag in i["tags"]]
    return items


def show(item_id, *, root=None):
    """The item, or None."""
    conn = _db(root)
    try:
        return _fetch(conn, item_id)
    except ItemNotFound:
        return None
    finally:
        conn.close()


def delete(item_id, *, root=None):
    """Remove an item; True if a row went, False if none matched."""
    conn = _db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.execute("COMMIT")
        return cur.rowcount > 0
    finally:
        conn.close()


# ---- CLI -------------------------------------------------------------

def _print_item(item, as_json):
    if as_json:
        print(json.dumps({"item": item}, indent=2, sort_keys=True))
    else:
        print("#%d %s [%s]%s%s" % (
            item["id"], item["title"], item["state"],
            " domain=%s" % item["domain"] if item["domain"] else "",
            " owner=%s" % item["owner"] if item["owner"] else ""))


def _cmd_add(args):
    item = add(args.title, domain=args.domain or "", state=args.state,
               tags=args.tag or (), owner=args.owner, notes=args.notes or "",
               root=args.root)
    _print_item(item, args.json)
    return 0


def _cmd_update(args):
    tags = None
    if args.tag:
        tags = list(args.tag)
    if args.add_tag:
        current = show(args.id, root=args.root)
        if current is None:
            raise ItemNotFound(args.id)
        tags = (tags if tags is not None else current["tags"]) + list(args.add_tag)
    item = update(args.id, title=args.title, domain=args.domain,
                  state=args.state, tags=tags, owner=args.owner,
                  notes=args.notes, root=args.root)
    _print_item(item, args.json)
    return 0


def _cmd_state(args):
    item = set_state(args.id, args.state, root=args.root)
    if args.json:
        _print_item(item, True)
    else:
        print("#%d -> %s" % (item["id"], item["state"]))
    return 0


def _cmd_list(args):
    items = list_items(domain=args.domain, state=args.state, tag=args.tag,
                       owner=args.owner, root=args.root)
    if args.json:
        print(json.dumps({"items": items}, indent=2, sort_keys=True))
        return 0
    if not items:
        print("(no tracker items)")
        return 0
    print("%4s  %-8s %-10s %-10s %s"
          % ("ID", "STATE", "OWNER", "DOMAIN", "TITLE"))
    for it in items:
        print("%4d  %-8s %-10s %-10s %s"
              % (it["id"], it["state"], (it["owner"] or "-")[:10],
                 (it["domain"] or "-")[:10], it["title"][:60]))
    return 0


def _cmd_show(args):
    item = show(args.id, root=args.root)
    if item is None:
        raise ItemNotFound(args.id)
    if args.json:
        _print_item(item, True)
    else:
        for key in FIELDS:
            value = item[key]
            if key == "tags":
                value = ", ".join(value) if value else "-"
            print("  %-10s: %s" % (key, value))
    return 0


def _cmd_delete(args):
    if not delete(args.id, root=args.root):
        raise ItemNotFound(args.id)
    if args.json:
        print(json.dumps({"ok": True, "deleted": args.id}))
    else:
        print("deleted #%d" % args.id)
    return 0


@traced_cli("cousin-tracker")
def tracker_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-tracker",
        description="framework-wide in-flight work tracker",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--json", action="store_true")
        p.add_argument("--root", default=None,
                       help="framework root (else FRAMEWORK_ROOT)")

    p = sub.add_parser("add", help="add an item")
    p.add_argument("title")
    p.add_argument("--domain")
    p.add_argument("--state", default="open")
    p.add_argument("--tag", action="append")
    p.add_argument("--owner", help="defaults to your slug from COUSIN_HOME")
    p.add_argument("--notes")
    common(p)
    p = sub.add_parser("update", help="change fields of an item")
    p.add_argument("id", type=int)
    p.add_argument("--title")
    p.add_argument("--domain")
    p.add_argument("--state")
    p.add_argument("--tag", action="append", help="replace the tag list")
    p.add_argument("--add-tag", action="append", help="append a tag")
    p.add_argument("--owner")
    p.add_argument("--notes")
    common(p)
    p = sub.add_parser("state", help="move an item to a state")
    p.add_argument("id", type=int)
    p.add_argument("state", metavar="|".join(STATES))
    common(p)
    p = sub.add_parser("list", help="list items, open first")
    p.add_argument("--domain")
    p.add_argument("--state")
    p.add_argument("--tag")
    p.add_argument("--owner")
    common(p)
    p = sub.add_parser("show", help="one item, every field")
    p.add_argument("id", type=int)
    common(p)
    p = sub.add_parser("delete", help="remove an item (ids never recycle)")
    p.add_argument("id", type=int)
    common(p)
    args = parser.parse_args(argv)
    handlers = {
        "add": _cmd_add, "update": _cmd_update, "state": _cmd_state,
        "list": _cmd_list, "show": _cmd_show, "delete": _cmd_delete,
    }
    try:
        return handlers[args.cmd](args)
    except ItemNotFound as err:
        print("cousin-tracker: %s" % err, file=sys.stderr)
        return 1
    except (TrackerError, MissingConfigError) as err:
        print("cousin-tracker: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(tracker_main())
