"""Confined, read-only access to the files under one base directory
(a cousin home, or the harness auto-memory directory of one).

The boundary every caller inherits:

- a path is a relative POSIX string; an absolute path, a `..` segment,
  an empty segment past the first or a NUL is refused before any
  filesystem call;
- the candidate is resolved (links followed) and must stay inside the
  resolved base: a link that leaves the base is listed as such and
  never followed;
- `.secrets` is invisible at any depth, including through a link whose
  target lands in it: never listed, never read, never downloaded;
- a file whose first bytes are not text is never handed back as text.

PathRefused carries the HTTP status a route answers with.
"""
from __future__ import annotations

import mimetypes
import os
import stat
from pathlib import Path

SECRET_NAMES = frozenset({".secrets"})
IMAGE_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".webp": "image/webp",
                  ".bmp": "image/bmp", ".ico": "image/x-icon"}
MARKDOWN_SUFFIXES = {".md", ".markdown"}
SNIFF_BYTES = 8192
LIST_CAP = 2000
DEFAULT_PAGE_LINES = 1000
MAX_PAGE_LINES = 5000
# A Markdown file up to this size comes back whole, to render; past it
# the reader pages it as text like any other large file.
MARKDOWN_WHOLE_BYTES = 2 * 1024 * 1024


class PathRefused(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _segments(rel):
    if not isinstance(rel, str):
        raise PathRefused("path must be a string")
    if "\x00" in rel:
        raise PathRefused("bad path")
    if rel.startswith("/") or rel.startswith("~"):
        raise PathRefused("path must be relative")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise PathRefused("path may not contain ..")
        if part in SECRET_NAMES:
            raise PathRefused("not found", status=404)
    return parts


def resolve_in(base, rel):
    """The resolved absolute path of `rel` under `base`, or PathRefused.
    Existence is not checked; containment and the secrets rule are."""
    root = Path(base).resolve()
    parts = _segments(rel)
    candidate = root.joinpath(*parts).resolve() if parts else root
    try:
        inner = candidate.relative_to(root)
    except ValueError:
        raise PathRefused("path leaves the home", status=403)
    if any(p in SECRET_NAMES for p in inner.parts):
        raise PathRefused("not found", status=404)
    return candidate


def relpath(base, path):
    """`path` relative to the resolved base, as a POSIX string."""
    return Path(path).relative_to(Path(base).resolve()).as_posix()


def _is_hidden(name):
    return name.startswith(".")


def list_dir(base, rel, *, show_hidden=False):
    """One directory level: {"path", "entries": [...], "truncated"}.
    Entries sort directories first, then by name; each carries name,
    path (relative to base), type (dir, file, link, other), size, mtime,
    and for a link `outside` (its target leaves the base or does not
    exist) and `target_type`."""
    directory = resolve_in(base, rel)
    if not directory.is_dir():
        raise PathRefused("not a directory", status=404)
    root = Path(base).resolve()
    rows = []
    try:
        names = os.listdir(directory)
    except OSError as err:
        raise PathRefused("unreadable: %s" % err.strerror, status=403)
    for name in names:
        if name in SECRET_NAMES:
            continue
        if _is_hidden(name) and not show_hidden:
            continue
        full = directory / name
        try:
            lst = full.lstat()
        except OSError:
            continue
        row = {"name": name, "path": relpath(root, full),
               "size": lst.st_size, "mtime": lst.st_mtime}
        if stat.S_ISLNK(lst.st_mode):
            row["type"] = "link"
            try:
                target = resolve_in(root, row["path"])
                tst = target.stat()
                row["outside"] = False
                row["target_type"] = "dir" if stat.S_ISDIR(tst.st_mode) \
                    else "file"
                row["size"] = tst.st_size
            except (PathRefused, OSError):
                row["outside"] = True
                row["target_type"] = None
        elif stat.S_ISDIR(lst.st_mode):
            row["type"] = "dir"
        elif stat.S_ISREG(lst.st_mode):
            row["type"] = "file"
        else:
            row["type"] = "other"
        rows.append(row)

    def order(row):
        is_dir = row["type"] == "dir" or (row["type"] == "link"
                                          and row.get("target_type") == "dir")
        return (0 if is_dir else 1, _is_hidden(row["name"]),
                row["name"].lower())

    rows.sort(key=order)
    truncated = len(rows) > LIST_CAP
    return {"path": relpath(root, directory) if directory != root else "",
            "entries": rows[:LIST_CAP], "truncated": truncated,
            "total": len(rows)}


def classify(path):
    """'image', 'markdown', 'text' or 'binary' for a regular file."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    try:
        with open(path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return "binary"
    if b"\x00" in head:
        return "binary"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as err:
        # A multi-byte character cut by the sniff window is still text.
        if err.start < len(head) - 4:
            return "binary"
    return "markdown" if suffix in MARKDOWN_SUFFIXES else "text"


def open_file(base, rel):
    """(resolved path, stat) of a regular file under base."""
    path = resolve_in(base, rel)
    try:
        st = path.stat()
    except OSError:
        raise PathRefused("not found", status=404)
    if not stat.S_ISREG(st.st_mode):
        raise PathRefused("not a file", status=404)
    return path, st


def read_text_page(base, rel, *, start=1, count=DEFAULT_PAGE_LINES):
    """The reader's view of one file. Always: kind, path, size, mtime.
    markdown (up to MARKDOWN_WHOLE_BYTES): `text`, the whole file.
    text (and larger markdown): `lines` from 1-based `start`, at most
    `count` of them, `total_lines`, `more`. image and binary: nothing
    else; the download route serves the bytes."""
    path, st = open_file(base, rel)
    kind = classify(path)
    out = {"kind": kind, "path": relpath(base, path), "size": st.st_size,
           "mtime": st.st_mtime, "mime": mimetypes.guess_type(path.name)[0]}
    if kind in ("image", "binary"):
        return out
    if kind == "markdown" and st.st_size <= MARKDOWN_WHOLE_BYTES:
        out["text"] = path.read_bytes().decode("utf-8", "replace")
        return out
    if kind == "markdown":
        out["kind"] = "text"
    start = max(1, int(start or 1))
    count = max(1, min(int(count or DEFAULT_PAGE_LINES), MAX_PAGE_LINES))
    lines = []
    total = 0
    with open(path, "rb") as fh:
        for raw in fh:
            total += 1
            if start <= total < start + count:
                lines.append(raw.rstrip(b"\r\n").decode("utf-8", "replace"))
    out.update({"start": start, "lines": lines, "total_lines": total,
                "more": start + len(lines) - 1 < total})
    return out


def download_type(path):
    """(content type, inline?) for the download route: raster images
    inline; everything else, SVG included (it can carry script, so it
    is read as text and never rendered), an attachment."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return IMAGE_SUFFIXES[suffix], True
    return "application/octet-stream", False
