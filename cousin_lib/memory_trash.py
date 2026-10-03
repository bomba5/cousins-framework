"""Reversible removal of memories: a per-cousin trash.

Removing a memory never destroys it. A batch is one operator action
(a raw entry; a decision plus its raw mirror; one memory, note or
legacy file) and lives in <home>/memory/.trash/<id>/:

- manifest.json: {"id", "deleted_at", "by", "items": [...]}; a line
  item keeps the home-relative path, its 1-based line number and the
  line itself; a file item keeps the home-relative path, and the bytes
  sit under files/<path> in the batch directory;
- every trash and restore appends one line to
  <home>/memory/.trash/audit.jsonl.

What may be removed is narrow on purpose:

- lines of memory/raw/<name>.jsonl (daily files and monthly digests;
  the gzip archive is the forensic tier and stays whole) and of
  data/decisions.jsonl;
- regular files under memory/ and notes/, except the generated layer
  (memory/distilled/, which the distiller rewrites from raw: remove
  the raw entries instead), the raw files themselves (remove entries),
  the index and recall artifacts, and the trash;
- a file under legacy/ only when the caller says so explicitly.

`.secrets/`, links and anything outside the home are refused by the
same resolver the console's file reader uses.

A line is addressed by its number and the short hash of its text: a
file that shifted since the explorer read it is searched for the hash,
and a hash that is gone is a 409, never a guess. Rewrites are atomic
(temp file + rename) and retried when an appender grew the file while
the rewrite was being prepared.

Consumers of what changed: after_change() reruns the distiller when a
raw line moved; the search index notices a removed or restored file by
itself (its staleness check compares file counts and mtimes) and
memory_search skips the trash directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import memory_lock, perimeter
from cousin_lib.home_files import PathRefused, resolve_in

TRASH_NAME = ".trash"
_RAW_LINE_RE = re.compile(r"^memory/raw/[^/]+\.jsonl$")
_LINE_FILES = ("data/decisions.jsonl",)
_NOT_REMOVABLE_FILES = {"fts_index.db", "vectors.db", "embeddings.json",
                        ".recall-log.jsonl", ".recall-counts.json",
                        ".recall-log-archive.jsonl", ".reindexed"}
_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9]{6}(-[0-9]+)?$")
_REWRITE_TRIES = 5


def trash_dir(home):
    return Path(home) / "memory" / TRASH_NAME


def _audit(home, record):
    path = trash_dir(home) / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def line_sha(text):
    """The short hash that addresses one JSONL line (no newline)."""
    return hashlib.sha1(text.rstrip("\r\n").encode()).hexdigest()[:12]


def _now():
    return datetime.now(timezone.utc)


def _new_batch_dir(home):
    base = trash_dir(home)
    base.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%dT%H%M%S-%f")
    candidate, n = stamp, 1
    while True:
        try:
            (base / candidate).mkdir()
            return candidate, base / candidate
        except FileExistsError:
            n += 1
            candidate = "%s-%d" % (stamp, n)


def _rel_parts(rel):
    return [p for p in str(rel).split("/") if p not in ("", ".")]


def _no_links(home, rel, path):
    """The resolved path must be the literal one: a link anywhere on
    the way could make an allowed name reach a refused place."""
    if path != Path(home).resolve().joinpath(*_rel_parts(rel)):
        raise PathRefused("links are not followed here")


def _check_line_file(home, rel):
    rel = "/".join(_rel_parts(rel))
    if not (_RAW_LINE_RE.match(rel) or rel in _LINE_FILES):
        raise PathRefused("only memory/raw/*.jsonl and"
                          " data/decisions.jsonl lines can be removed")
    path = resolve_in(home, rel)
    _no_links(home, rel, path)
    if not path.is_file():
        raise PathRefused("not found", status=404)
    return rel, path


def _check_file(home, rel, allow_legacy):
    parts = _rel_parts(rel)
    rel = "/".join(parts)
    if not parts or parts[0] not in ("memory", "notes", "legacy"):
        raise PathRefused("only files under memory/, notes/ (or legacy/"
                          " when selected) can be removed")
    if parts[0] == "legacy" and not allow_legacy:
        raise PathRefused("legacy/ is the pre-migration archive: select"
                          " it explicitly to remove a file there")
    if parts[0] == "memory":
        if len(parts) > 1 and parts[1] in ("distilled", "raw", TRASH_NAME):
            raise PathRefused(
                "memory/%s is not removable as a file (distilled is"
                " regenerated from raw; raw is removed entry by entry;"
                " the trash restores)" % parts[1])
        if parts[-1] in _NOT_REMOVABLE_FILES:
            raise PathRefused("index and recall artifacts are not"
                              " memories")
    path = resolve_in(home, rel)
    if Path(home, rel).is_symlink():
        raise PathRefused("links are not removed")
    _no_links(home, rel, path)
    if not path.is_file():
        raise PathRefused("not found", status=404)
    return rel, path


def can_trash_line(rel):
    """True when a line at `rel` may be moved into a trash batch: a
    memory/raw/<name>.jsonl daily file or monthly digest. The gzip
    archive is the forensic tier and is never rewritten. A caller that
    would rather ask than catch PathRefused (dream_memory.undo) asks
    first, so one untrashable line cannot fail a whole batch."""
    return bool(_RAW_LINE_RE.match(str(rel or "")))


def _rewrite(home, path, mutate):
    """Apply mutate(lines) -> lines to a file atomically. Retried when
    the file changed while the new content was prepared (an appender
    wrote a line); the check and the replace hold the home's memory write
    lock, so no append lands between them and none is lost."""
    # The one chokepoint for trashing and restoring lines and files: the
    # caller's `rel` is validated by _check_file/_check_line_file, and
    # this is what holds the result to the perimeter as well.
    perimeter.assert_writable(path, writer="memory_trash._rewrite")
    for _ in range(_REWRITE_TRIES):
        before = path.stat()
        raw = path.read_bytes().decode("utf-8", "replace")
        lines = raw.splitlines()
        new_lines = mutate(lines)
        body = "".join(l + "\n" for l in new_lines)
        tmp = path.with_name(".%s.trash-tmp" % path.name)
        tmp.write_text(body)
        os.chmod(tmp, before.st_mode & 0o7777)
        with memory_lock.write_lock(home):
            now = path.stat()
            if (now.st_size, now.st_mtime_ns) == (before.st_size,
                                                  before.st_mtime_ns):
                os.replace(tmp, path)
                return
        tmp.unlink()
    raise PathRefused("the file kept changing; try again", status=409)


def _locate(lines, line_no, sha):
    """0-based index of the addressed line: the stated number when its
    hash matches (or no hash was given), else the one line carrying the
    hash."""
    idx = line_no - 1
    if 0 <= idx < len(lines) and (sha is None or line_sha(lines[idx]) == sha):
        return idx
    if sha is None:
        raise PathRefused("line %d does not exist" % line_no, status=409)
    hits = [i for i, l in enumerate(lines) if line_sha(l) == sha]
    if len(hits) == 1:
        return hits[0]
    raise PathRefused("the entry changed or is gone since it was read;"
                      " reload", status=409)


def trash_lines(home, refs, *, by=None):
    """Move JSONL lines into one trash batch. refs: [(rel, line_no,
    sha)], sha from line_sha() of the line as read (None skips the
    check). All refs are validated before anything moves."""
    home = Path(home)
    checked = []
    for rel, line_no, sha in refs:
        rel, path = _check_line_file(home, rel)
        checked.append((rel, path, int(line_no), sha))
    # Resolve every index against the current content first, so a bad
    # ref fails the batch before any file is rewritten.
    plan = {}
    for rel, path, line_no, sha in checked:
        lines = path.read_bytes().decode("utf-8", "replace").splitlines()
        idx = _locate(lines, line_no, sha)
        plan.setdefault(rel, (path, []))[1].append(
            (idx, line_sha(lines[idx]), lines[idx]))
    batch_id, batch_dir = _new_batch_dir(home)
    items = []
    for rel, (path, targets) in plan.items():
        wanted = {sha for _i, sha, _l in targets}
        removed = []

        def mutate(lines, targets=targets, wanted=wanted, removed=removed):
            del removed[:]
            keep = []
            drop = set()
            for idx, sha, _text in targets:
                if idx < len(lines) and line_sha(lines[idx]) == sha:
                    drop.add(idx)
                else:
                    drop.add(_locate(lines, idx + 1, sha))
            for i, text in enumerate(lines):
                if i in drop:
                    removed.append((i + 1, text))
                else:
                    keep.append(text)
            return keep

        _rewrite(home, path, mutate)
        for line_no, text in removed:
            items.append({"kind": "line", "path": rel, "line_no": line_no,
                          "sha": line_sha(text), "line": text})
    manifest = {"id": batch_id, "deleted_at": _now().isoformat(),
                "by": by, "items": items}
    (batch_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    for item in items:
        _audit(home, {"ts": manifest["deleted_at"], "action": "trash",
                      "trash_id": batch_id, "kind": "line",
                      "path": item["path"], "line_no": item["line_no"],
                      "sha": item["sha"], "by": by})
    return manifest


def trash_file(home, rel, *, by=None, allow_legacy=False):
    """Move one memory, note (or, when allowed, legacy) file into a new
    trash batch, keeping its home-relative path."""
    home = Path(home)
    rel, path = _check_file(home, rel, allow_legacy)
    batch_id, batch_dir = _new_batch_dir(home)
    dest = batch_dir / "files" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(path, dest)
    except OSError:
        shutil.move(str(path), str(dest))
    manifest = {"id": batch_id, "deleted_at": _now().isoformat(), "by": by,
                "items": [{"kind": "file", "path": rel,
                           "size": dest.stat().st_size}]}
    (batch_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    _audit(home, {"ts": manifest["deleted_at"], "action": "trash",
                  "trash_id": batch_id, "kind": "file", "path": rel,
                  "by": by})
    return manifest


def _batch(home, trash_id):
    if not isinstance(trash_id, str) or not _ID_RE.match(trash_id):
        raise PathRefused("unknown trash id", status=404)
    batch_dir = trash_dir(home) / trash_id
    try:
        manifest = json.loads((batch_dir / "manifest.json").read_text())
    except (OSError, ValueError):
        raise PathRefused("unknown trash id", status=404)
    return batch_dir, manifest


def list_trash(home):
    """Every batch, newest first, as its manifest."""
    base = trash_dir(home)
    out = []
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir() or not _ID_RE.match(child.name):
            continue
        try:
            out.append(json.loads((child / "manifest.json").read_text()))
        except (OSError, ValueError):
            continue
    return out


def restore(home, trash_id, *, by=None):
    """Put a batch back: files to their path (never over an existing
    file), lines into their file at their old position. All targets are
    checked before anything moves; the batch directory goes on
    success."""
    home = Path(home)
    batch_dir, manifest = _batch(home, trash_id)
    items = manifest.get("items") or []
    for item in items:
        target = resolve_in(home, item["path"])
        if item["kind"] == "file":
            if target.exists() or Path(home, item["path"]).is_symlink():
                raise PathRefused("%s exists again; move it away first"
                                  % item["path"], status=409)
            if not (batch_dir / "files" / item["path"]).is_file():
                raise PathRefused("trash copy of %s is missing"
                                  % item["path"], status=409)
        elif target.is_file():
            present = target.read_bytes().decode("utf-8", "replace") \
                .splitlines()
            if item["line"] in present:
                raise PathRefused("that entry is back in %s already"
                                  % item["path"], status=409)
    lines_by_path = {}
    for item in items:
        if item["kind"] == "file":
            target = resolve_in(home, item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(batch_dir / "files" / item["path"], target)
        else:
            lines_by_path.setdefault(item["path"], []).append(item)
    for rel, line_items in lines_by_path.items():
        target = resolve_in(home, rel)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")
        ordered = sorted(line_items, key=lambda it: it["line_no"])

        def mutate(lines, ordered=ordered):
            lines = list(lines)
            for it in ordered:
                at = min(max(0, it["line_no"] - 1), len(lines))
                lines.insert(at, it["line"])
            return lines

        _rewrite(home, target, mutate)
    shutil.rmtree(batch_dir)
    stamp = _now().isoformat()
    for item in items:
        _audit(home, {"ts": stamp, "action": "restore", "trash_id": trash_id,
                      "kind": item["kind"], "path": item["path"],
                      "line_no": item.get("line_no"), "by": by})
    return manifest


def after_change(home, manifest):
    """Bring what depends on the memory up to date after a trash or a
    restore: the distilled views are regenerated when a raw line moved
    (the distiller is deterministic and cheap, and boot runs it anyway).
    The search index needs nothing: its staleness check sees the file
    count or mtimes change and it rebuilds on the next search; raw
    lines are not indexed at all. Never raises: the move already
    happened."""
    report = {"distilled": False, "index": "rebuilds on next search"}
    touched_raw = any(it.get("kind") == "line"
                      and str(it.get("path", "")).startswith("memory/raw/")
                      for it in manifest.get("items") or [])
    if touched_raw:
        try:
            from cousin_lib import distill
            distill.distill(home)
            report["distilled"] = True
        except Exception as err:  # noqa: BLE001 - reported, not raised
            report["distill_error"] = "%s: %s" % (type(err).__name__, err)
    return report


# -------------------------------------------------------------- CLI

def _describe(manifest):
    parts = []
    for item in manifest.get("items") or []:
        if item["kind"] == "file":
            parts.append(item["path"])
        else:
            parts.append("%s:%d" % (item["path"], item["line_no"]))
    return ", ".join(parts)


def cli(argv, home):
    """`cousin-memory trash [list]` and `cousin-memory trash restore
    <id>`, wired from cousin_lib.memory."""
    parser = argparse.ArgumentParser(prog="cousin-memory trash")
    parser.add_argument("action", nargs="?", default="list",
                        choices=("list", "restore"))
    parser.add_argument("trash_id", nargs="?")
    args = parser.parse_args(argv)
    if args.action == "list":
        batches = list_trash(home)
        if not batches:
            print("trash is empty")
        for manifest in batches:
            print("%s  %s  %s" % (manifest["id"],
                                  (manifest.get("by") or "-"),
                                  _describe(manifest)))
        return 0
    if not args.trash_id:
        print("error: restore needs a trash id (cousin-memory trash list)",
              file=sys.stderr)
        return 2
    try:
        manifest = restore(home, args.trash_id,
                           by=os.environ.get("USER") or None)
    except PathRefused as err:
        print("error: %s" % err, file=sys.stderr)
        return 1
    report = after_change(home, manifest)
    print("restored %s%s" % (_describe(manifest),
                             " (distilled views regenerated)"
                             if report["distilled"] else ""))
    return 0
