"""Import a cousin's chat history from the previous framework.

`cousin-chat-import <slug> --old-home <dir>` reads <old-home>/data/chat.db
(the previous framework's shape: original_id plus image/audio/video
columns) into this cousin's store (<home>/data/chat.db, attachment_kind
and attachment_path), with the cousin stopped (no runner holding its
lock).

- Every old row keeps its id, so reply quotes between old messages still
  point at the right one.
- Rows the new store already holds move to ids after the highest old id;
  their reply quotes, reactions and inbound pictures move with them.
- An old picture (a `/api/chat/<dir>/<name>` link into the old home, a
  `data:` image, or an absolute path) is copied to
  <home>/chat/inbound/<id>.<ext>, the name the console shows a message's
  picture by. A picture whose file is gone is counted, the message kept.
- Audio and video links are kept as text on the message (this framework
  has no media path); they are counted in the report.
- The previous store is kept as data/chat.db.pre-import-<stamp>, and a
  marker (data/.chat-imported.json) refuses a second import.
"""
import argparse
import base64
import datetime
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MARKER = ".chat-imported.json"
_DATA_URI = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.*)$", re.S)
_API_LINK = re.compile(r"^/api/chat/([A-Za-z0-9_-]+)/([^/]+)$")
_INBOUND_NAME = re.compile(r"^(\d+)(\.[A-Za-z0-9]+)$")


class ImportRefused(Exception):
    """The import would lose or duplicate history; nothing was written."""


def _resolve_image(value, old_home):
    """(bytes, ext) for an old image column value, or None when the
    picture cannot be found."""
    if not value:
        return None
    m = _DATA_URI.match(value)
    if m:
        ext = "." + m.group(1).lower().replace("jpeg", "jpg")
        if ext not in IMAGE_EXTS:
            return None
        try:
            return base64.b64decode(m.group(2)), ext
        except (ValueError, TypeError):
            return None
    m = _API_LINK.match(value)
    if m:
        # The previous server served /api/chat/image/<name> from
        # chat/images/: try the plural folder, then the literal one.
        base = pathlib.Path(old_home) / "chat"
        path = base / (m.group(1) + "s") / m.group(2)
        if not path.is_file():
            path = base / m.group(1) / m.group(2)
    elif value.startswith("/"):
        path = pathlib.Path(value)
    else:
        return None
    ext = path.suffix.lower()
    if ext not in IMAGE_EXTS or not path.is_file():
        return None
    return path.read_bytes(), ext


def _remap_reply(reply_to, mapping):
    """A reply quote whose id points at a renumbered row, rewritten."""
    if not reply_to or not mapping:
        return reply_to
    try:
        data = json.loads(reply_to)
    except (ValueError, TypeError):
        return reply_to
    if isinstance(data, dict) and data.get("id") in mapping:
        data["id"] = mapping[data["id"]]
        return json.dumps(data)
    return reply_to


def import_history(old_home, new_home, *, force=False):
    old_home, new_home = pathlib.Path(old_home), pathlib.Path(new_home)
    old_db = old_home / "data" / "chat.db"
    new_db = new_home / "data" / "chat.db"
    marker = new_home / "data" / MARKER
    if not old_db.is_file():
        raise ImportRefused("no old chat store at %s" % old_db)
    if not new_db.is_file():
        # A chat server that never stored a row has not created its
        # store yet; create it with the server's own schema.
        from cousin_lib.server.storage import ChatStore
        new_db.parent.mkdir(parents=True, exist_ok=True)
        ChatStore(new_db)
    if marker.exists() and not force:
        raise ImportRefused("history already imported (%s)" % marker)

    old = sqlite3.connect("file:%s?mode=ro" % old_db, uri=True)
    old.row_factory = sqlite3.Row
    old_cols = {r[1] for r in old.execute("PRAGMA table_info(messages)")}
    old_rows = [dict(r) for r in old.execute("SELECT * FROM messages ORDER BY id")]
    has_rx = old.execute("SELECT 1 FROM sqlite_master WHERE name='reactions'").fetchone()
    old_rx = [dict(r) for r in old.execute("SELECT * FROM reactions")] if has_rx else []
    old.close()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = new_db.with_name("chat.db.pre-import-%s" % stamp)
    shutil.copy2(new_db, backup)

    new = sqlite3.connect(new_db)
    new.row_factory = sqlite3.Row
    cur_rows = [dict(r) for r in new.execute("SELECT * FROM messages ORDER BY id")]
    cur_rx = [dict(r) for r in new.execute("SELECT * FROM reactions")]

    top = max([r["id"] for r in old_rows] or [0])
    mapping = {r["id"]: top + i for i, r in enumerate(cur_rows, 1)}
    inbound = new_home / "chat" / "inbound"
    inbound.mkdir(parents=True, exist_ok=True)

    # Move the new rows' pictures out of the way first (two phases, so a
    # renumbered file never lands on a name another one still holds).
    moves = []
    for entry in list(inbound.iterdir()):
        m = _INBOUND_NAME.match(entry.name)
        if m and int(m.group(1)) in mapping:
            parked = entry.with_name(".import-%s" % entry.name)
            os.replace(entry, parked)
            moves.append((parked, inbound / ("%d%s" % (mapping[int(m.group(1))], m.group(2)))))

    report = {"imported": len(old_rows), "renumbered": len(cur_rows),
              "images": 0, "images_missing": 0, "media_links_kept": 0,
              "old_home": str(old_home), "backup": str(backup)}
    out = []
    for r in old_rows:
        kind = path = None
        pic = _resolve_image(r.get("image"), old_home)
        if pic is not None:
            data, ext = pic
            target = inbound / ("%d%s" % (r["id"], ext))
            target.write_bytes(data)
            kind, path = "image", str(target)
            report["images"] += 1
        elif r.get("image"):
            report["images_missing"] += 1
        message = r["message"]
        for col in ("audio", "video"):
            if col in old_cols and r.get(col):
                message += "\n[%s: %s]" % (col, r[col])
                report["media_links_kept"] += 1
        out.append((r["id"], r["chat_user"], r["user"], message, r["timestamp"],
                    r["type"], r.get("archived") or 0, r.get("reply_to"),
                    r.get("reply_to_user"), kind, path))
    for r in cur_rows:
        nid = mapping[r["id"]]
        path = r.get("attachment_path")
        if path:
            m = _INBOUND_NAME.match(pathlib.Path(path).name)
            if m and int(m.group(1)) == r["id"]:
                path = str(inbound / ("%d%s" % (nid, m.group(2))))
        out.append((nid, r["chat_user"], r["user"], r["message"], r["timestamp"],
                    r["type"], r.get("archived") or 0,
                    _remap_reply(r.get("reply_to"), mapping),
                    r.get("reply_to_user"), r.get("attachment_kind"), path))

    rx = [(x["message_id"], x["user"], x["emoji"], x.get("tap_count") or 1,
           x["created"]) for x in old_rx]
    rx += [(mapping.get(x["message_id"], x["message_id"]), x["user"], x["emoji"],
            x.get("tap_count") or 1, x["created"]) for x in cur_rx]

    try:
        with new:
            new.execute("DELETE FROM reactions")
            new.execute("DELETE FROM messages")
            new.executemany(
                "INSERT INTO messages (id, chat_user, user, message, timestamp,"
                " type, archived, reply_to, reply_to_user, attachment_kind,"
                " attachment_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
            new.executemany(
                "INSERT OR IGNORE INTO reactions (message_id, user, emoji,"
                " tap_count, created) VALUES (?,?,?,?,?)", rx)
            high = max([row[0] for row in out] or [0])
            new.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            new.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('messages', ?)",
                        (high,))
    except sqlite3.Error:
        for parked, final in moves:
            os.replace(parked, parked.with_name(parked.name[len(".import-"):]))
        raise
    finally:
        new.close()
    for parked, final in moves:
        os.replace(parked, final)
    report["imported_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    marker.write_text(json.dumps(report, indent=2) + "\n")
    from cousin_lib import memory
    memory.record_event(
        new_home, "L1_FRAMEWORK", "framework:chat-import",
        "chat history imported from %s: %d messages (%d existing"
        " renumbered, %d pictures, %d missing)" % (
            old_home, report["imported"], report["renumbered"],
            report["images"], report["images_missing"]), "framework")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-chat-import",
        description="import a cousin's chat history from the previous framework")
    parser.add_argument("slug")
    parser.add_argument("--old-home", required=True,
                        help="the cousin's previous home (holds data/chat.db"
                             " and chat/); an unpacked legacy archive works")
    parser.add_argument("--root", help="framework root (else FRAMEWORK_ROOT)")
    parser.add_argument("--force", action="store_true",
                        help="import even if a marker says it was done")
    args = parser.parse_args(argv)
    from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
    try:
        root = FrameworkConfig.resolve(args.root).root
        home = pathlib.Path(root) / "cousins" / args.slug
        cfg = CousinConfig.load(home)
    except MissingConfigError as err:
        print("cousin-chat-import: %s" % err, file=sys.stderr)
        return 2
    from cousin_lib.runner.main import is_running
    if is_running(cfg.home):
        print("cousin-chat-import: %s's runner is running; stop it first"
              " (the import rewrites its store)" % args.slug, file=sys.stderr)
        return 2
    try:
        report = import_history(args.old_home, cfg.home, force=args.force)
    except ImportRefused as err:
        print("cousin-chat-import: %s" % err, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
