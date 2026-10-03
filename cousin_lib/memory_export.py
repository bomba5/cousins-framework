"""Move a cousin's memory between homes, byte for byte.

`cousin-memory export --out PATH` writes one tar.gz of the home's memory
state and `cousin-memory import PATH` brings it into another home.

The rule everything here descends from: an entry's id (memory.entry_id)
is a sha1 over its own stored timestamp, topic and content, and obsolete
marks, `derived_from` hops and dreaming journals name entries by that id.
The monthly fold keeps ids because it copies day files into the archives
byte-identical. A mover that parsed and re-serialized a line, or stamped
it again, would change its id and orphan every mark and derivation that
names it. So nothing here parses a line to move it: files travel as their
bytes, archives as their compressed bytes, and a merge compares and
appends lines as bytes.

What moves (MANIFEST.json lists it, each file with its size and sha256):

- memory/raw/*.jsonl, the day files and the monthly digests ("lines");
- memory/raw/archive/*.jsonl.gz, the folded months ("archive"), never
  recompressed;
- memory/*.md, the knowledge files; memory/distilled/, memory/imported/
  and memory/.trash/ (removed memories, which the decisions backfill
  reads so a removal is not undone); memory/.dream-ledger.json;
- data/dreams/ (the dreaming passes and their journals), data/decisions.jsonl
  and its rotated archives, data/template-sync.json.

What does not (listed in the manifest as excluded, with the reason): the
search indexes, the recall log and counts, the distiller's stamp, the
backfill's mark and lock files. They are rebuilt or re-earned by the home
that receives the memory.

Import checks every file against the manifest before it writes a byte,
and refuses a home that already has raw memory unless merging. A merge
appends the lines a home does not hold yet, byte for byte, to the file
they came from; an archive of the same name with other bytes is refused,
never merged; a whole file that differs is left as the home has it.
Afterwards the distiller runs, as after a trash restore; the search index
notices the new files on the next search.
"""
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import memory, memory_lock, perimeter
from cousin_lib.home_files import PathRefused, resolve_in

FORMAT = 1
BUNDLE_KIND = "cousin-memory-export"
MANIFEST_NAME = "MANIFEST.json"

# Kinds: how a file moves and merges.
LINES, ARCHIVE, FILE = "lines", "archive", "file"

# The subtrees that move whole (every file under them, kind by suffix).
_TREES = ("memory/distilled/", "memory/imported/", "memory/.trash/", "data/dreams/")
_SINGLE = {"memory/.dream-ledger.json": FILE, "data/template-sync.json": FILE,
           "data/decisions.jsonl": LINES}
_RAW_DAY = re.compile(r"^memory/raw/[^/]+\.jsonl$")
_RAW_ARCHIVE = re.compile(r"^memory/raw/archive/[^/]+\.jsonl\.gz$")
_KNOWLEDGE = re.compile(r"^memory/[^/]+\.md$")
_DECISIONS_ARCHIVE = re.compile(r"^data/decisions-archive-[^/]+\.jsonl$")

# Why a file under memory/ (or a known one under data/) stays behind.
_SEARCH = "the keyword search index: rebuilt from the files on the next search"
_VECTORS = "the semantic index: re-embedded from the files"
_RECALL = "this home's recall weighting: re-earned by the searches the new home makes"
_EXCLUDED = {
    "memory/fts_index.db": _SEARCH, "memory/fts_index.db-wal": _SEARCH,
    "memory/fts_index.db-shm": _SEARCH, "memory/.fts.db": _SEARCH,
    "memory/vectors.db": _VECTORS, "memory/embeddings.json": _VECTORS,
    "memory/.embeddings.lock": _VECTORS,
    "memory/.reindexed": "a mark of the last reindex: the new home reindexes itself",
    "memory/.recall-log.jsonl": _RECALL, "memory/.recall-log-archive.jsonl": _RECALL,
    "memory/.recall-counts.json": _RECALL,
    "memory/.last-distill": "the distiller's stamp: the import runs the distiller",
    "data/.decisions-backfilled": "the decisions backfill's mark: the new home runs its"
                                  " own backfill, idempotent against the raw that moved",
    "data/.decisions-backfilled.lock": "a lock file",
    "data/.memory-write.lock": "a lock file",
}
_OTHER = "not a file the framework reads as memory"


class BundleError(Exception):
    """The bundle is not one this importer can trust: exit 2."""


class ImportRefused(Exception):
    """The home cannot take the bundle as asked: exit 2."""


def _safe_rel(rel):
    """`rel` as a clean POSIX path relative to a home, or None."""
    rel = str(rel or "")
    if not rel or rel.startswith("/") or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return rel


def kind_of(rel):
    """How a home-relative path moves (LINES, ARCHIVE, FILE), or None
    when it is not part of the memory this module moves."""
    rel = _safe_rel(rel)
    if rel is None or rel.endswith(".tmp") or rel.endswith(".lock"):
        return None
    if rel in _SINGLE:
        return _SINGLE[rel]
    if _RAW_DAY.match(rel) or _DECISIONS_ARCHIVE.match(rel):
        return LINES
    if _RAW_ARCHIVE.match(rel):
        return ARCHIVE
    if _KNOWLEDGE.match(rel):
        return FILE
    if any(rel.startswith(tree) for tree in _TREES):
        return LINES if rel.endswith(".jsonl") else FILE
    return None


def _excluded_why(rel):
    if rel in _EXCLUDED:
        return _EXCLUDED[rel]
    if rel.endswith(".tmp"):
        return "a writer's temporary file"
    if rel.endswith(".lock"):
        return "a lock file"
    return _OTHER


def _walk(base, top):
    """Every path under `top` (a directory under `base`), files and
    links, never following a link into another tree; sorted."""
    out = []
    if not top.is_dir() or top.is_symlink():
        return out
    for dirpath, dirnames, filenames in os.walk(top):
        dirpath = Path(dirpath)
        for name in list(dirnames):
            if (dirpath / name).is_symlink():
                out.append(dirpath / name)
                dirnames.remove(name)
        out.extend(dirpath / name for name in filenames)
    return sorted(out)


def collect(home):
    """(files, excluded) for an export of `home`: files as [(rel, kind)],
    excluded as [{"path", "why"}], both sorted by path. Everything under
    memory/ is one or the other; under data/ only the memory this module
    moves and the known marks are named."""
    home = Path(home)
    files, excluded = [], []
    candidates = _walk(home, home / "memory") + _walk(home, home / "data" / "dreams")
    candidates += [home / rel for rel in ("data/decisions.jsonl", "data/template-sync.json",
                                          "data/.decisions-backfilled")]
    candidates += sorted((home / "data").glob("decisions-archive-*.jsonl"))
    seen = set()
    for path in candidates:
        rel = path.relative_to(home).as_posix()
        if rel in seen or not (path.is_symlink() or path.exists()):
            continue
        seen.add(rel)
        if path.is_symlink():
            excluded.append({"path": rel, "why": "a link, not a file of this home"})
            continue
        kind = kind_of(rel)
        if kind is None or not path.is_file():
            excluded.append({"path": rel, "why": _excluded_why(rel)})
        else:
            files.append((rel, kind))
    return sorted(files), sorted(excluded, key=lambda e: e["path"])


def _source_slug(home):
    from cousin_lib.config import CousinConfig, MissingConfigError
    try:
        return CousinConfig.load(home).slug
    except (MissingConfigError, OSError, ValueError):
        return Path(home).name


def _framework_version():
    try:
        import cousin_lib
        return str(cousin_lib.__version__)
    except Exception:  # noqa: BLE001 - a bundle without a version is still a bundle
        return "unknown"


def export(home, out):
    """Write the bundle of `home`'s memory to `out` (a new tar.gz) and
    return its manifest. The files are read under the home's memory write
    lock, so a fold or a dreaming pass cannot move a day file mid-read;
    each is stored as the bytes it holds. Refuses an `out` that exists."""
    home, out = Path(home).resolve(), Path(out)
    if out.exists():
        raise FileExistsError("%s exists (an export never overwrites)" % out)
    with memory_lock.write_lock(home):
        files, excluded = collect(home)
        blobs = {rel: (home / rel).read_bytes() for rel, _k in files}
    manifest = {
        "format": FORMAT, "kind": BUNDLE_KIND,
        "framework_version": _framework_version(),
        "source": _source_slug(home),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [{"path": rel, "kind": kind, "size": len(blobs[rel]),
                   "sha256": hashlib.sha256(blobs[rel]).hexdigest()}
                  for rel, kind in files],
        "excluded": excluded,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(".%s.tmp" % out.name)
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            def add(name, data):
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), now, 0o644
                tar.addfile(info, io.BytesIO(data))
            add(MANIFEST_NAME, (json.dumps(manifest, indent=1) + "\n").encode("utf-8"))
            for rel, _kind in files:
                add(rel, blobs[rel])
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return manifest


# ---- reading a bundle --------------------------------------------------------

def read_bundle(path):
    """(manifest, {rel: bytes}) of a bundle, every file checked against the
    manifest: its size and sha256, its path one this module moves, its kind
    the one the path implies. BundleError naming every problem found;
    nothing is written by this function."""
    try:
        tar = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError, EOFError, zlib.error) as err:
        raise BundleError("%s is not a memory export: %s" % (path, err))
    with tar:
        try:
            members = tar.getmembers()
        except (tarfile.TarError, EOFError, zlib.error, OSError) as err:
            raise BundleError("%s is damaged: %s" % (path, err))
        by_name = {}
        for m in members:
            if m.name in by_name:
                raise BundleError("%s holds %s twice" % (path, m.name))
            by_name[m.name] = m
        head = by_name.pop(MANIFEST_NAME, None)
        if head is None or not head.isfile():
            raise BundleError("%s has no %s" % (path, MANIFEST_NAME))
        try:
            manifest = json.loads(tar.extractfile(head).read().decode("utf-8"))
        except (ValueError, OSError, tarfile.TarError, EOFError, zlib.error) as err:
            raise BundleError("%s: unreadable manifest: %s" % (path, err))
        if not isinstance(manifest, dict) or manifest.get("kind") != BUNDLE_KIND:
            raise BundleError("%s: not a %s manifest" % (path, BUNDLE_KIND))
        if manifest.get("format") != FORMAT:
            raise BundleError("%s: manifest format %r, this importer reads %d"
                              % (path, manifest.get("format"), FORMAT))
        problems, blobs = [], {}
        for row in manifest.get("files") or []:
            rel = row.get("path") if isinstance(row, dict) else None
            kind = kind_of(rel)
            if kind is None:
                problems.append("%r: not a memory path" % (rel,))
                continue
            if row.get("kind") != kind:
                problems.append("%s: kind %r, the path is %r" % (rel, row.get("kind"), kind))
                continue
            member = by_name.pop(rel, None)
            if member is None or not member.isfile():
                problems.append("%s: in the manifest, not in the bundle" % rel)
                continue
            try:
                data = tar.extractfile(member).read()
            except (OSError, tarfile.TarError, EOFError, zlib.error) as err:
                problems.append("%s: unreadable: %s" % (rel, err))
                continue
            if len(data) != row.get("size") or \
                    hashlib.sha256(data).hexdigest() != row.get("sha256"):
                problems.append("%s: sha256 or size differs from the manifest" % rel)
                continue
            blobs[rel] = data
        problems += ["%s: in the bundle, not in the manifest" % name for name in sorted(by_name)]
    if problems:
        raise BundleError("%s refused, nothing written:\n  %s" % (path, "\n  ".join(problems)))
    return manifest, blobs


# ---- importing ---------------------------------------------------------------

def _lines(data):
    """The lines of `data`, each as its bytes without the newline."""
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def has_raw_memory(home):
    """True when the home holds a raw file with any bytes in it."""
    rdir = memory.raw_dir(home)
    paths = list(rdir.glob("*.jsonl")) + list((rdir / "archive").glob("*.jsonl.gz"))
    return any(p.is_file() and p.stat().st_size > 0 for p in paths)


def _raw_known(home):
    """Every line the home's raw store holds, day files, digests and
    archives alike, as bytes: a line already anywhere in raw (a day the
    home folded since) is not appended again."""
    known = set()
    rdir = memory.raw_dir(home)
    for path in sorted(rdir.glob("*.jsonl")):
        known.update(_lines(path.read_bytes()))
    for path in sorted((rdir / "archive").glob("*.jsonl.gz")):
        try:
            with gzip.open(path, "rb") as fh:
                known.update(_lines(fh.read()))
        except (OSError, EOFError, zlib.error) as err:
            raise ImportRefused("%s is unreadable (%s): a merge could not tell which"
                                " lines it holds" % (path.relative_to(home).as_posix(), err))
    return known


def _target(home, rel):
    """The path `rel` names in `home`, refused when a link leads it
    elsewhere."""
    try:
        path = resolve_in(home, rel)
    except PathRefused as err:
        raise ImportRefused("%s: %s" % (rel, err))
    if path != Path(home).joinpath(*rel.split("/")):
        raise ImportRefused("%s: a link in the home leads elsewhere" % rel)
    return path


def _is_stub(path):
    """The distiller's stub for a distilled file, which an import replaces."""
    from cousin_lib import distill
    try:
        return path.read_text() == distill._stub(path.name)
    except (OSError, UnicodeDecodeError):
        return False


def plan_import(home, manifest, blobs, *, merge=False):
    """One row per bundle file: {"path", "kind", "action", "lines"}, the
    action one of write (absent here: the file as its bytes), append (the
    lines this home does not hold, as their bytes), skip (nothing new),
    replace-stub (a distilled stub gives way) and kept (a whole file that
    differs here stays as it is). ImportRefused, nothing written, for a
    home with raw memory without `merge` and for an archive of the same
    name with other bytes."""
    home = Path(home)
    if not merge and has_raw_memory(home):
        raise ImportRefused("%s already has raw memory; --merge adds the bundle's lines"
                            " it does not hold" % home)
    raw_known = None
    rows, problems = [], []
    for entry in manifest["files"]:
        rel, kind = entry["path"], entry["kind"]
        data, path = blobs[rel], _target(home, rel)
        exists = path.is_file()
        row = {"path": rel, "kind": kind, "lines": 0}
        if exists and path.read_bytes() == data:
            row["action"] = "skip"
        elif kind == ARCHIVE:
            if exists:
                problems.append("%s: an archive of that name with other bytes is here;"
                                " archives are never merged" % rel)
                continue
            row["action"] = "write"
        elif kind == LINES:
            if rel.startswith("memory/raw/"):
                if raw_known is None:
                    raw_known = _raw_known(home)
                known = raw_known
            else:
                known = set(_lines(path.read_bytes())) if exists else set()
            new = [line for line in _lines(data) if line.strip() and line not in known]
            if not exists and len(new) == len([l for l in _lines(data) if l.strip()]):
                row["action"], row["lines"] = "write", len(new)
            elif new:
                row["action"], row["lines"], row["new"] = "append", len(new), new
            else:
                row["action"] = "skip"
        elif not exists:
            row["action"] = "write"
        elif _is_stub(path):
            row["action"] = "replace-stub"
        else:
            row["action"] = "kept"
        rows.append(row)
    if problems:
        raise ImportRefused("refused, nothing written:\n  %s" % "\n  ".join(problems))
    return rows


def _write(path, data):
    perimeter.assert_writable(path, writer="memory_export.import")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.import-tmp" % path.name)
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _append(path, lines):
    """Append `lines` (bytes, no newline) to `path`, each with its own
    newline; a file whose last line has none gets one first, so the
    first appended line never joins it."""
    perimeter.assert_writable(path, writer="memory_export.import")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        if fh.tell() > 0:
            with open(path, "rb") as head:
                head.seek(-1, os.SEEK_END)
                if head.read(1) != b"\n":
                    fh.write(b"\n")
        fh.write(b"".join(line + b"\n" for line in lines))


def import_bundle(home, bundle, *, merge=False, apply=False):
    """Check the bundle, plan the import into `home` and, with `apply`,
    carry it out. Returns {"source", "rows", "applied", "distilled"}.
    BundleError or ImportRefused before anything is written. The plan and
    the writes are one section under the home's memory write lock; the
    distiller runs after it, as after a trash restore, and the search
    index sees the new files on its next staleness check."""
    home = Path(home).resolve()
    if not home.is_dir():
        raise ImportRefused("%s is not a directory (an import never creates a home)" % home)
    manifest, blobs = read_bundle(bundle)
    with memory_lock.write_lock(home):
        rows = plan_import(home, manifest, blobs, merge=merge)
        if apply:
            for row in rows:
                path = home.joinpath(*row["path"].split("/"))
                if row["action"] in ("write", "replace-stub"):
                    _write(path, blobs[row["path"]])
                elif row["action"] == "append":
                    _append(path, row["new"])
    report = {"source": manifest.get("source"), "created_at": manifest.get("created_at"),
              "framework_version": manifest.get("framework_version"),
              "rows": [{k: v for k, v in r.items() if k != "new"} for r in rows],
              "applied": bool(apply), "distilled": False}
    if apply and any(r["action"] != "skip" and r["action"] != "kept" for r in rows):
        try:
            from cousin_lib import distill
            distill.distill(home)
            report["distilled"] = True
        except Exception as err:  # noqa: BLE001 - the import already happened
            report["distill_error"] = "%s: %s" % (type(err).__name__, err)
    return report


def format_import(report):
    counts = {}
    for row in report["rows"]:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    lines = ["memory of %s (exported %s, framework %s)"
             % (report.get("source"), report.get("created_at"), report.get("framework_version"))]
    for row in report["rows"]:
        if row["action"] == "skip":
            continue
        extra = " (%d line(s))" % row["lines"] if row["kind"] == LINES and row["lines"] else ""
        if row["action"] == "kept":
            extra = " (differs here; the home's copy stays)"
        lines.append("  %-12s %s%s" % (row["action"], row["path"], extra))
    lines.append("%s: %s" % ("imported" if report["applied"] else "dry run, nothing written",
                             ", ".join("%d %s" % (n, a) for a, n in sorted(counts.items()))
                             or "nothing"))
    if report.get("distilled"):
        lines.append("distilled views regenerated; the search index rebuilds on the next search")
    if report.get("distill_error"):
        lines.append("warning: distill failed after the import: %s" % report["distill_error"])
    if not report["applied"]:
        lines.append("re-run with --yes to import")
    return "\n".join(lines)
