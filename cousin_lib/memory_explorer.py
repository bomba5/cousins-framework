"""The memory explorer's read model (docs/reference/console-api.md, "Memory
explorer"): one cousin's memory as the layers it is actually built
from, never a flat file list.

Layers, top to bottom of the recall path:

- active: STATUS.md and the data/ session files (handoff, active
  threads, checkpoints) the boot packet reads first;
- index: MEMORY.md, the pointer index;
- raw: memory/raw/<YYYY-MM-DD>.jsonl, the append-only candidates every
  producer writes (decide's bridge, transcript mining, capsules), each
  with a truth level;
- digest: memory/raw/<YYYY-MM>-digest.jsonl, one entry per topic per
  folded month (raw_fold);
- archive: memory/raw/archive/<YYYY-MM>.jsonl.gz, the byte-identical
  forensic tier;
- distilled: memory/distilled/*.md, regenerated from raw by the
  distiller;
- decisions: data/decisions.jsonl (and its dated rotations);
- memory and notes: the cousin's own Markdown files;
- harness: the agent harness's auto-memory directory, when
  config/harness.toml names one;
- search, recall, trash, legacy: the indexes, the usage log, removed
  memories and the pre-migration archive.

Truth levels: the taxonomy is L0_OPERATOR, L1_FRAMEWORK, L2_TOOL,
L3_COUSIN_CONCLUSION, L4_COUSIN_HYPOTHESIS, L5_OBSOLETE. Producers in
this tree also write the short forms `operator-stated` and
`cousin-conclusion`; normalize_level maps both onto the taxonomy and a
missing level to the default, L3.

Read-only and layout-neutral: nothing here creates a directory.
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cousin_lib import memory, reinforce
from cousin_lib.memory_trash import TRASH_NAME, line_sha, list_trash

TRUTH_LEVELS = memory.TRUTH_LEVELS
DEFAULT_LEVEL = memory.DEFAULT_TRUTH_LEVEL
_ALIASES = memory.LEVEL_ALIASES
_KNOWN_FIELDS = {"topic", "content", "truth_level", "source", "timestamp",
                 "created_at", "id", "entries", "first_at", "last_at", "cite"}
ACTIVE_FILES = ("STATUS.md", "data/handoff.md", "data/handoff-manual.md",
                "data/pre-compact-checkpoint.md")
INDEX_FILES = ("MEMORY.md",)
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
_DIGEST_RE = re.compile(r"^\d{4}-\d{2}-digest\.jsonl$")
_ARTIFACTS = {"fts_index.db", "vectors.db", "embeddings.json", ".recall-log.jsonl",
              ".recall-counts.json", ".recall-log-archive.jsonl",
              ".reindexed"}
LAYER_IDS = ("active", "index", "raw", "digest", "archive", "distilled",
             "decisions", "memory", "notes", "harness", "search", "recall",
             "trash", "legacy")
FILE_LAYERS = ("active", "index", "distilled", "memory", "notes",
               "harness", "legacy")
TIERS = ("live", "daily", "digest", "archive", "all")
MAX_LIMIT = 1000


def normalize_level(value):
    """The taxonomy name for a stored truth level; 'other' for a value
    that is not one."""
    return memory.normalize_level(value)


# ------------------------------------------------------------ helpers

def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _latest(paths):
    stamps = [m for m in (_mtime(p) for p in paths) if m is not None]
    return max(stamps) if stamps else None


def _files_under(base, *, skip_dirs=(), skip_names=()):
    """Regular, non-hidden files under base, recursively, sorted; no
    link is followed out of base."""
    out = []
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] in skip_dirs:
            continue
        if path.name in skip_names:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        out.append(path)
    return out


def harness_dir(home, root):
    if root is None:
        return None
    try:
        from cousin_lib import memory_search
        return memory_search._harness_dir(Path(home), Path(root))
    except Exception:  # noqa: BLE001 - a broken harness.toml hides a layer
        return None


def _raw_files(home):
    raw = memory.raw_dir(home)
    daily, digests = [], []
    if raw.is_dir():
        for path in sorted(raw.iterdir()):
            if not path.is_file():
                continue
            if _DAY_RE.match(path.name):
                daily.append(path)
            elif _DIGEST_RE.match(path.name) or path.suffix == ".jsonl":
                digests.append(path)
    archives = sorted((raw / "archive").glob("*.jsonl.gz")) \
        if (raw / "archive").is_dir() else []
    return daily, digests, archives


def _read_lines(path):
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", errors="replace") as fh:
                return fh.read().splitlines()
        return path.read_bytes().decode("utf-8", "replace").splitlines()
    except (OSError, EOFError):
        return []


def _stamp(entry):
    return str(entry.get("timestamp") or entry.get("created_at") or "")


def _record(home, path, line_no, text, entry, tier):
    extra = {k: v for k, v in entry.items() if k not in _KNOWN_FIELDS}
    rec = {
        "tier": tier,
        "file": path.name,
        "topic": str(entry.get("topic") or ""),
        "content": entry.get("content") if isinstance(
            entry.get("content"), str) else json.dumps(entry.get("content")),
        "truth_level": entry.get("truth_level"),
        "level": normalize_level(entry.get("truth_level")),
        "source": entry.get("source"),
        "timestamp": _stamp(entry) or None,
        "when": memory.entry_timestamp(entry),
        "id": entry.get("id"),
        # where an operator-stated entry says it came from (remember and
        # decide --cite); null when the entry carries none
        "cite": entry.get("cite"),
        "extra": extra,
        "ref": None,
    }
    if tier == "digest":
        rec.update({"entries": entry.get("entries"),
                    "first_at": entry.get("first_at"),
                    "last_at": entry.get("last_at")})
    if tier != "archive":
        rec["ref"] = {"path": path.relative_to(Path(home)).as_posix(),
                      "line_no": line_no, "sha": line_sha(text)}
    return rec


def _scan(home, tiers):
    """(records, unparsable line count) over the chosen tiers."""
    daily, digests, archives = _raw_files(home)
    sources = []
    if "daily" in tiers:
        sources += [(p, "daily") for p in daily]
    if "digest" in tiers:
        sources += [(p, "digest") for p in digests]
    if "archive" in tiers:
        sources += [(p, "archive") for p in archives]
    records, bad = [], 0
    for path, tier in sources:
        for n, text in enumerate(_read_lines(path), start=1):
            if not text.strip():
                continue
            try:
                entry = json.loads(text)
            except ValueError:
                bad += 1
                continue
            if not isinstance(entry, dict):
                bad += 1
                continue
            records.append(_record(home, path, n, text, entry, tier))
    return records, bad


def _tier_set(tier):
    return {"live": ("daily", "digest"), "daily": ("daily",),
            "digest": ("digest",), "archive": ("archive",),
            "all": ("daily", "digest", "archive")}.get(tier or "live")


def raw_entries(home, *, levels=None, topic=None, q=None, source=None,
                since=None, until=None, tier="live", limit=200, offset=0):
    """Raw entries newest first as field records (never the JSON line),
    filtered, paged, with facets over the whole filtered set. `since`
    and `until` are inclusive ISO dates; an undated entry passes a date
    filter only when none is set. A record's `ref` addresses its line
    for removal; archive entries have none (the forensic tier stays
    whole)."""
    home = Path(home)
    tiers = _tier_set(tier)
    if tiers is None:
        raise ValueError("tier must be one of %s" % ", ".join(TIERS))
    records, _bad = _scan(home, tiers)
    wanted = {normalize_level(l) for l in (levels or []) if l}
    topic_q = (topic or "").lower().strip()
    text_q = (q or "").lower().strip()
    out = []
    for rec in records:
        if wanted and rec["level"] not in wanted:
            continue
        if topic_q and topic_q not in rec["topic"].lower():
            continue
        if text_q and text_q not in (rec["content"] or "").lower() \
                and text_q not in rec["topic"].lower():
            continue
        if source and (rec["source"] or "") != source:
            continue
        day = (rec["timestamp"] or "")[:10]
        if since and (not day or day < since):
            continue
        if until and (not day or day > until):
            continue
        out.append(rec)
    out.sort(key=lambda r: (r["when"] or 0.0, r["ref"]["line_no"]
                            if r["ref"] else 0), reverse=True)
    facets = {"levels": dict(Counter(r["level"] for r in out)),
              "sources": dict(Counter(r["source"] or "-" for r in out)),
              "topics": len({r["topic"] for r in out})}
    limit = max(1, min(int(limit or 200), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    return {"entries": out[offset:offset + limit], "total": len(out),
            "offset": offset, "limit": limit, "facets": facets}


# ----------------------------------------------------------- decisions

def _decision_files(home):
    data = Path(home) / "data"
    live = data / "decisions.jsonl"
    archives = sorted(data.glob("decisions-archive-*.jsonl")) \
        if data.is_dir() else []
    return live, archives


def mirror_refs(home, decision):
    """The raw entries decide's bridge wrote for this decision: source
    'decision', same topic, content '<decision> - why: <reasoning>'."""
    want = "%s - why: %s" % (decision.get("decision", ""),
                             decision.get("reasoning", ""))
    refs = []
    records, _bad = _scan(Path(home), ("daily",))
    for rec in records:
        if (rec["source"] == "decision" and rec["topic"] == decision.get(
                "topic") and (rec["content"] or "") == want):
            refs.append(rec["ref"])
    return refs


def decisions(home, *, q=None, limit=200, offset=0, archives=False):
    """Decisions newest first as records: timestamp, topic, decision,
    reasoning, `ref` (live file only) and `mirrors`, the refs of the
    raw entries the decide bridge wrote for it."""
    home = Path(home)
    live, archive_files = _decision_files(home)
    files = [(live, True)] + ([(p, False) for p in archive_files]
                              if archives else [])
    raw_records, _bad = _scan(home, ("daily",))
    mirrors = {}
    for rec in raw_records:
        if rec["source"] == "decision":
            mirrors.setdefault((rec["topic"], rec["content"] or ""),
                               []).append(rec["ref"])
    out = []
    text_q = (q or "").lower().strip()
    for path, is_live in files:
        if not path.is_file():
            continue
        for n, text in enumerate(_read_lines(path), start=1):
            try:
                entry = json.loads(text)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            rec = {"timestamp": entry.get("timestamp"),
                   "topic": entry.get("topic") or "",
                   "decision": entry.get("decision") or "",
                   "reasoning": entry.get("reasoning") or "",
                   "file": path.name,
                   "ref": ({"path": "data/decisions.jsonl", "line_no": n,
                            "sha": line_sha(text)} if is_live else None)}
            if text_q and not any(text_q in str(rec[k]).lower() for k in
                                  ("topic", "decision", "reasoning")):
                continue
            key = (rec["topic"], "%s - why: %s" % (rec["decision"],
                                                   rec["reasoning"]))
            rec["mirrors"] = mirrors.get(key, [])
            rec["when"] = memory.entry_timestamp(entry)
            out.append(rec)
    out.sort(key=lambda r: r["when"] or 0.0, reverse=True)
    limit = max(1, min(int(limit or 200), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    return {"entries": out[offset:offset + limit], "total": len(out),
            "offset": offset, "limit": limit}


# ------------------------------------------------------------- recall

def _recall_key(home, key):
    """A recall-counts key as a home-relative path ('memory/x.md'), or
    'harness:<rel>' for the harness collection; older installs keyed
    '<collection>:<rel>'."""
    key = str(key)
    m = re.match(r"^(memory|notes|harness):(.+)$", key)
    if m:
        if m.group(1) == "harness":
            return key
        return "%s/%s" % (m.group(1), m.group(2))
    try:
        return Path(key).resolve().relative_to(
            Path(home).resolve()).as_posix()
    except (ValueError, OSError):
        return key


def recall_counts(home):
    out = {}
    for key, slot in reinforce.load_counts(home).items():
        norm = _recall_key(home, key)
        prev = out.get(norm)
        if prev is None:
            out[norm] = dict(slot)
        else:
            prev["count"] += slot["count"]
            prev["last"] = max(str(prev.get("last") or ""),
                               str(slot.get("last") or ""))
    return out


def _recall_log(home):
    path = Path(home) / "memory" / ".recall-log.jsonl"
    lines = [l for l in _read_lines(path) if l.strip()]
    last = None
    if lines:
        try:
            last = json.loads(lines[-1]).get("ts")
        except ValueError:
            last = None
    return len(lines), last, _mtime(path)


# -------------------------------------------------------------- files

def _file_rows(home, paths, *, base=None, deletable=False, legacy=False,
               recalls=None, prefix=""):
    base = Path(base or home)
    rows = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(base).as_posix()
        row = {"path": rel, "name": path.name, "size": st.st_size,
               "mtime": st.st_mtime, "deletable": deletable,
               "recalls": (recalls or {}).get(prefix + rel, {}).get("count", 0)}
        if legacy:
            row["legacy"] = True
        rows.append(row)
    return rows


def _memory_files(home):
    return _files_under(Path(home) / "memory",
                        skip_dirs=("raw", "distilled", TRASH_NAME),
                        skip_names=_ARTIFACTS)


def layer_files(home, layer, *, root=None):
    """The files of one file layer as rows: path (home-relative; for
    the harness layer, relative to the harness directory), size, mtime,
    recalls, deletable, and `stub` for distilled files."""
    home = Path(home)
    recalls = recall_counts(home)
    if layer == "active":
        paths = [home / p for p in ACTIVE_FILES if (home / p).is_file()]
        return _file_rows(home, paths, recalls=recalls)
    if layer == "index":
        paths = [home / p for p in INDEX_FILES if (home / p).is_file()]
        return _file_rows(home, paths, recalls=recalls)
    if layer == "distilled":
        ddir = memory.distilled_dir(home)
        paths = sorted(ddir.glob("*.md")) if ddir.is_dir() else []
        rows = _file_rows(home, paths, recalls=recalls)
        for row, path in zip(rows, paths):
            try:
                row["stub"] = memory.STUB_TEXT in path.read_text(
                    errors="replace")
            except OSError:
                row["stub"] = False
        return rows
    if layer == "memory":
        return _file_rows(home, _memory_files(home), deletable=True,
                          recalls=recalls)
    if layer == "notes":
        return _file_rows(home, _files_under(home / "notes"),
                          deletable=True, recalls=recalls)
    if layer == "legacy":
        return _file_rows(home, _files_under(home / "legacy"),
                          deletable=True, legacy=True)
    if layer == "harness":
        hdir = harness_dir(home, root)
        if hdir is None:
            return []
        paths = [p for p in _files_under(hdir) if p.suffix == ".md"]
        return _file_rows(home, paths, base=hdir, recalls=recalls,
                          prefix="harness:")
    raise ValueError("unknown file layer %r" % layer)


# ------------------------------------------------------------ overview

_LINK_RE = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


def dangling_index_links(home):
    """Relative links in MEMORY.md whose target is gone (a removed or
    renamed memory the index still points at). Indexes link either
    from the home or from memory/; a target found at either is live."""
    index = Path(home) / "MEMORY.md"
    try:
        text = index.read_text(errors="replace")
    except OSError:
        return []
    out = []
    for target in _LINK_RE.findall(text):
        if re.match(r"^[a-z]+:", target) or target.startswith("/"):
            continue
        if ((Path(home) / target).exists()
                or (Path(home) / "memory" / target).exists()):
            continue
        if target not in out:
            out.append(target)
    return out


def _search_layer(home, root):
    mem = Path(home) / "memory"
    fts = mem / "fts_index.db"
    emb = mem / "vectors.db"
    layer = {"id": "search", "title": "search indexes",
             "fts": {"exists": fts.is_file()},
             "embeddings": {"exists": emb.is_file()},
             "count": 0, "updated": _latest([fts, emb])}
    if fts.is_file():
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % fts, uri=True)
            try:
                row = conn.execute("SELECT built_at, source_mtime, files"
                                   " FROM index_meta").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
        if row:
            layer["fts"].update({"built_at": row[0], "files": row[2]})
            layer["count"] = row[2]
            try:
                from cousin_lib import memory_search
                sources = memory_search._sources(Path(home), root)
                layer["fts"]["stale"] = (
                    len(sources) != row[2]
                    or any(p.stat().st_mtime > row[1]
                           for _c, p, _r in sources))
            except Exception:  # noqa: BLE001 - staleness is advisory
                layer["fts"]["stale"] = None
    if emb.is_file():
        layer["embeddings"]["size"] = emb.stat().st_size
        if emb.stat().st_size < 50 * 1024 * 1024:
            try:
                data = json.loads(emb.read_text())
                chunks = data.get("chunks") if isinstance(data, dict) \
                    else None
                if isinstance(chunks, (list, dict)):
                    layer["embeddings"]["chunks"] = len(chunks)
            except (OSError, ValueError):
                pass
    return layer


def overview(home, *, root=None, now=None):
    """{"layers": [...], "insights": {...}} for one cousin home."""
    home = Path(home)
    now = now or datetime.now(timezone.utc)
    layers = []

    def add(lid, title, **kw):
        row = {"id": lid, "title": title, "count": 0, "updated": None}
        row.update(kw)
        layers.append(row)
        return row

    active = [home / p for p in ACTIVE_FILES if (home / p).is_file()]
    add("active", "active state", count=len(active),
        updated=_latest(active), files=[p.relative_to(home).as_posix()
                                        for p in active])
    index = [home / p for p in INDEX_FILES if (home / p).is_file()]
    add("index", "MEMORY.md index", count=len(index),
        updated=_latest(index))

    daily, digests, archives = _raw_files(home)
    live, bad = _scan(home, ("daily", "digest"))
    daily_recs = [r for r in live if r["tier"] == "daily"]
    digest_recs = [r for r in live if r["tier"] == "digest"]
    add("raw", "raw entries (daily)", count=len(daily_recs),
        files=len(daily), updated=_latest(daily))
    add("digest", "monthly digests", count=len(digest_recs),
        files=len(digests), updated=_latest(digests),
        represents=sum(int(r.get("entries") or 1) for r in digest_recs))
    add("archive", "raw archive (gzip)", count=len(archives),
        files=len(archives), updated=_latest(archives),
        bytes=sum(p.stat().st_size for p in archives),
        months=[p.name.split(".")[0] for p in archives])

    distilled = layer_files(home, "distilled")
    add("distilled", "distilled views", count=len(distilled),
        stubs=sum(1 for r in distilled if r.get("stub")),
        updated=max((r["mtime"] for r in distilled), default=None))

    dec_live, dec_archives = _decision_files(home)
    dec_count = sum(1 for l in _read_lines(dec_live) if l.strip()) \
        if dec_live.is_file() else 0
    add("decisions", "decisions log", count=dec_count,
        updated=_mtime(dec_live), archives=len(dec_archives))

    for lid, title in (("memory", "memory files"), ("notes", "notes")):
        rows = layer_files(home, lid)
        add(lid, title, count=len(rows),
            updated=max((r["mtime"] for r in rows), default=None),
            bytes=sum(r["size"] for r in rows))

    hdir = harness_dir(home, root)
    hrows = layer_files(home, "harness", root=root) if hdir else []
    add("harness", "harness auto-memory", configured=hdir is not None,
        count=len(hrows), directory=str(hdir) if hdir else None,
        updated=max((r["mtime"] for r in hrows), default=None))

    layers.append(_search_layer(home, root))

    events, last_recall, recall_mtime = _recall_log(home)
    add("recall", "recall log", count=events, last=last_recall,
        updated=recall_mtime)

    trash = list_trash(home)
    add("trash", "trash", count=len(trash),
        updated=_mtime(home / "memory" / TRASH_NAME / "audit.jsonl"))

    legacy = layer_files(home, "legacy")
    add("legacy", "legacy archive", count=len(legacy),
        bytes=sum(r["size"] for r in legacy),
        updated=max((r["mtime"] for r in legacy), default=None))

    # -- insights: all from what was read above, nothing re-scanned
    levels = Counter(r["level"] for r in live)
    topics = Counter(r["topic"] for r in live)
    recalls = recall_counts(home)
    top = sorted(recalls.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    md_files = [r["path"] for r in layer_files(home, "memory")
                + layer_files(home, "notes") if r["path"].endswith(".md")]
    from cousin_lib import raw_fold
    cutoff = (now - timedelta(days=raw_fold.DEFAULT_KEEP_DAYS)) \
        .date().isoformat()
    newest_raw = max((r["when"] for r in live if r["when"]), default=None)
    # When the views were last brought up to date: the distill run
    # stamp. The files' own mtimes only move when their text changes,
    # so a raw entry that changes no top line would read as "behind"
    # forever. Installs from before the stamp fall back to the mtimes.
    from cousin_lib import distill as _distill
    distilled_mtime = _distill.last_run(home) or max(
        (r["mtime"] for r in distilled), default=None)
    months = Counter((r["timestamp"] or "")[:7] for r in daily_recs
                     if r["timestamp"])
    insights = {
        "levels": {lvl: levels.get(lvl, 0) for lvl in TRUTH_LEVELS
                   + ("other",) if levels.get(lvl, 0) or lvl != "other"},
        "sources": dict(Counter(r["source"] or "-" for r in live)
                        .most_common(8)),
        "topics": len(topics),
        "multi_entry_topics": sum(1 for c in topics.values() if c > 1),
        "top_topics": [{"topic": t, "entries": c}
                       for t, c in topics.most_common(8) if c > 1],
        "obsolete": levels.get("L5_OBSOLETE", 0),
        "hypotheses": levels.get("L4_COUSIN_HYPOTHESIS", 0),
        "operator": levels.get("L0_OPERATOR", 0),
        "undated": sum(1 for r in live if not r["when"]),
        "unparsable_lines": bad,
        "pending_fold_files": sum(1 for p in daily
                                  if p.name[:10] < cutoff),
        "per_month": dict(sorted(months.items())),
        "newest_raw": newest_raw,
        "distilled_behind_raw": bool(newest_raw and distilled_mtime
                                     and newest_raw > distilled_mtime),
        "most_recalled": [{"path": k, "count": v["count"],
                           "last": v.get("last")} for k, v in top[:10]],
        "cold_files": sorted(p for p in md_files if p not in recalls),
        "dangling_index_links": dangling_index_links(home),
        "recall_events": events,
        "last_recall": last_recall,
    }
    return {"layers": layers, "insights": insights}
