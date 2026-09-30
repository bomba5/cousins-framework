"""Search over a cousin's memory and notes: keyword always, semantic
when an embedding service is configured.

The keyword leg is SQLite FTS5 with BM25. The semantic leg embeds
files, in overlapping chunks, through an HTTP embedding service
declared in <root>/config/embedding.toml (url, model, timeout_s - the
endpoint accepts {"model", "prompt"} and returns {"embedding": [...]};
front any service with that contract). Results merge by
reciprocal-rank fusion, and every hit carries the semantic leg's
cosine as "similarity" (None when only the keyword leg found it): the
fused score is rank-based and cannot be compared against a cosine
threshold, so a caller that wants "relevant enough" reads similarity.

Recall is usage-weighted (cousin_lib.reinforce): every search records
the files it surfaced, and a file's fused score is multiplied by
(1 + bonus), a bounded, decaying nudge for what keeps being recalled.
Reinforcement is fail-open; it can never take a search down with it.

Collections: `memory` (<home>/memory), `notes` (<home>/notes) and
`harness`, the agent harness's own auto-memory directory for this
cousin, declared in <root>/config/harness.toml and included only when
that directory exists. Without a framework root there is no config/
and so no harness collection and no semantic leg.

The vector index (<home>/memory/embeddings.json) is incremental and
self-healing: every search re-embeds only new or changed chunks,
reuses the rest, drops what is gone, keeps a prior vector when the
service fails mid-pass (stale beats invisible), treats an empty index
over non-empty sources as stale, and is written atomically.

The degrade contract: soft-degrade is silent ONLY when nothing was
promised. No embedding config -> keyword, silently - the install
never claimed semantic search. Configured but unreachable or broken
-> keyword PLUS a notice on the result, never silently: a quietly
dead semantic leg leaves someone believing they have semantic recall
until the belief costs them a lookup.

Context comes from COUSIN_HOME and fails loud: a search that silently
reads nothing teaches its caller that memory is empty, which is worse
than an error.
"""
import fcntl
import gzip
import array
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import tomllib
import urllib.request
import zlib
from pathlib import Path

from cousin_lib import reinforce
from cousin_lib.config import (FrameworkConfig, MissingConfigError,
                               expand_harness_path, harness_config)
from cousin_lib.sqlite_util import wal

_EMBED_CAP_CHARS = 6000
_RRF_K = 60
_SNIPPET_CHARS = 160
# search() asks each leg for this many candidates, not just `top`: a
# hit ranked just past `top` in BOTH legs would out-score a
# single-leg hit once RRF sums the two, but only if fusion ever saw
# it (tracker #83). _fuse still cuts the fused result to `top`.
FUSION_DEPTH_MIN = 20
FUSION_DEPTH_FACTOR = 4
_DEFAULTS = {"chunk_chars": 2000, "chunk_overlap": 200}
_RECALL_DEFAULTS = {"min_chars": 24, "min_score": 0.45, "top": 3}
_TRASH_DIR = ".trash"
# A long refresh saves what it has every this many embedded chunks, so
# a pass that dies (a recall past its budget, a killed server) leaves
# its work behind instead of starting the next one from zero.
_CHECKPOINT_CHUNKS = 32


def _home():
    home = os.environ.get("COUSIN_HOME")
    if not home:
        raise MissingConfigError(
            "COUSIN_HOME is not set; refusing to search without a"
            " cousin context"
        )
    return Path(home)


def _root():
    """The framework root when one is declared, else None: without a
    root there is no config/ to read, and the module behaves as an
    install that promised nothing (keyword only, own files only).

    Declared means FrameworkConfig.resolve(): FRAMEWORK_ROOT, or else
    the root the cousin's own home names (<root>/cousins/<slug> with a
    <root>/config). Without the second, a shell that exported only
    COUSIN_HOME quietly lost the semantic leg the install configured
    (found by a clean-machine install test)."""
    try:
        return FrameworkConfig.resolve().root
    except MissingConfigError:
        return None


def _fts_path(home):
    return home / "memory" / "fts_index.db"


def _index_path(home):
    """The vector store: SQLite, one row per chunk, the vector a
    float32 blob.

    It was a single JSON object, read and parsed in full on every
    search. Measured 2026-09-21 on a real cousin: 31.6 MB and 1283 ms
    per query, against the 938 ms embedding call the index exists to
    serve, and growing with the memory. Same data, same access pattern
    (every vector is scored), but unpacking binary beats parsing text
    by an order of magnitude, and a row can be written without
    rewriting the file.
    """
    return home / "memory" / "vectors.db"


def _legacy_index_path(home):
    return home / "memory" / "embeddings.json"


def _pack(vector):
    return array.array("f", vector).tobytes()


def _unpack(blob):
    out = array.array("f")
    out.frombytes(blob)
    return out.tolist()


# ---------------------------------------------------------------- sources

def _harness_dir(home, root):
    """The harness auto-memory directory for this cousin, when
    config/harness.toml names one and it exists."""
    if root is None:
        return None
    harness = harness_config(root)
    template = (harness or {}).get("auto_memory_dir")
    if not template:
        return None
    path = expand_harness_path(template, home)
    return path if path.is_dir() else None


def _sources(home, root=None):
    """(collection, path, relpath) for every indexable file. relpath is
    relative to the collection's base directory, so the index key
    "<collection>:<relpath>" stays valid when a home moves; index
    artifacts are excluded by suffix."""
    if root is None:
        root = _root()
    bases = [("memory", home / "memory"), ("notes", home / "notes")]
    harness = _harness_dir(home, root)
    if harness is not None:
        bases.append(("harness", harness))
    imported = _imported(home) if harness is not None else {}
    out = []
    for collection, base in bases:
        if base.is_dir():
            for path in sorted(base.rglob("*.md")):
                rel = path.relative_to(base)
                # The memory trash (cousin_lib.memory_trash) keeps
                # removed files for restore; removed is not recalled.
                if _TRASH_DIR in rel.parts:
                    continue
                if collection == "harness" and _imported_current(path, imported):
                    continue
                out.append((collection, path, rel.as_posix()))
    return out


def _imported(home):
    """{name: sha256 of the source copied} from memory_import's manifest;
    {} when nothing was imported."""
    from cousin_lib import memory_import
    return {name: (row or {}).get("source")
            for name, row in memory_import.load_manifest(home).items()
            if isinstance(row, dict)}


def _imported_current(path, imported):
    """True when a harness file's imported copy is current: one memory is
    one hit, found as the copy. A file changed since the import stays
    searchable here until it is imported again (R9). Hashed only for the
    names the manifest holds."""
    if path.name not in imported:
        return False
    from cousin_lib import memory_import
    text = _read(path)
    return text is not None and memory_import.sha256(text) == imported[path.name]


# The raw store is JSONL, one entry per line, and it is where
# `cousin-memory decide` and `remember`, the flip's transcript miner,
# the jobs ledger and framework events all write. It was not indexed
# at all: `_sources` collects `*.md`, so an entry reached recall only
# through `distill`, which keeps one truncated line per topic and caps
# each file. Measured 2026-09-21 on a real cousin: 904 entries over
# 789 topics survived as 139 lines, so 82% of topics could not be
# found. Entries are indexed one per entry, not one per day file: a
# day file mixes unrelated topics, which is a bad unit for BM25 and a
# worse one for an embedding.
_RAW_FIELDS = ("topic", "content", "cite", "source", "truth_level")


def _raw_files(home):
    """The raw store's files, hot days first then the monthly
    archives raw_fold leaves behind."""
    base = Path(home) / "memory" / "raw"
    if not base.is_dir():
        return []
    return (sorted(base.glob("*.jsonl"))
            + sorted((base / "archive").glob("*.jsonl.gz")))


def _raw_entries(home):
    """(collection, key, body, mtime) per raw entry.

    `key` is the real file path with the entry's line appended, so
    every entry is a distinct hit (hits merge by path) while still
    naming the file it lives in. A line that is not JSON is skipped,
    never fatal: one bad write must not cost the entries around it.

    The same entry is indexed once however many files hold it:
    raw_fold keeps a month in both `<YYYY-MM>-digest.jsonl` and
    `archive/<YYYY-MM>.jsonl.gz` (measured on a real cousin: 149 of
    149 entries identical), and indexing both returns one memory as
    two hits. Files are walked hot-first, so the copy that survives is
    the readable one.
    """
    out = []
    seen = set()
    for path in _raw_files(home):
        try:
            mtime = path.stat().st_mtime
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", errors="replace") as fh:
                lines = fh.readlines()
        except (OSError, EOFError, zlib.error):
            continue
        for number, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            body = "\n".join(
                str(entry[f]) for f in _RAW_FIELDS if entry.get(f))
            if not body:
                continue
            when = str(entry.get("timestamp", ""))[:19]
            text = "%s\n%s" % (when, body) if when else body
            # Dedupe on the memory, not on its metadata: a digest twin
            # carries the same topic and content under a different
            # source, id, entry count and first/last timestamps.
            same = "%s\n%s" % (entry.get("topic", ""),
                               entry.get("content", ""))
            fingerprint = hashlib.sha256(
                same.encode("utf-8", "replace")).digest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append(("raw", "%s#%d" % (path, number), text, mtime))
    return out


def _read(path):
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _source_text(collection, path, rel):
    """The text both legs index for a source: the file's text, except that
    a memory_import copy (memory/imported/auto/*.md) is indexed as the
    original it was rendered from. The copy's provenance lines would
    otherwise shift every chunk window and re-embed every chunk, and a
    replay would measure that noise as a lost memory (P7-10)."""
    body = _read(path)
    if body is None or collection != "memory":
        return body
    from cousin_lib import memory_import
    if rel.startswith("/".join(memory_import.TARGET[1:]) + "/"):
        return memory_import.original_text(body)
    return body


# ------------------------------------------------------------ keyword leg

def build_index(home=None, root=None):
    """(Re)build the FTS index over the current sources. Returns the
    number of files indexed. `root` as in _sources: None discovers it,
    a path is used as given."""
    home = Path(home) if home else _home()
    db_path = _fts_path(home)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = wal(sqlite3.connect(db_path))
    try:
        conn.execute("DROP TABLE IF EXISTS memory_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE memory_fts USING fts5("
            " collection, path, body)"
        )
        count = 0
        latest = 0.0
        for collection, path, rel in _sources(home, root):
            body = _source_text(collection, path, rel)
            if body is None:
                continue
            conn.execute(
                "INSERT INTO memory_fts (collection, path, body)"
                " VALUES (?, ?, ?)",
                (collection, str(path), body),
            )
            latest = max(latest, path.stat().st_mtime)
            count += 1
        for collection, key, body, mtime in _raw_entries(home):
            conn.execute(
                "INSERT INTO memory_fts (collection, path, body)"
                " VALUES (?, ?, ?)",
                (collection, key, body),
            )
            latest = max(latest, mtime)
            count += 1
        conn.execute("DROP TABLE IF EXISTS index_meta")
        conn.execute("CREATE TABLE index_meta (built_at REAL,"
                     " source_mtime REAL, files INTEGER)")
        conn.execute("INSERT INTO index_meta VALUES (?, ?, ?)",
                     (time.time(), latest, count))
        conn.commit()
        return count
    finally:
        conn.close()


def _index_stale(home, root=None):
    """The index is stale when any source is newer than the newest
    source at build time, or when the file counts differ (a deleted
    file also invalidates)."""
    db_path = _fts_path(home)
    if not db_path.exists():
        return True
    conn = wal(sqlite3.connect(db_path))
    try:
        row = conn.execute(
            "SELECT source_mtime, files FROM index_meta").fetchone()
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()
    if not row:
        return True
    built_mtime, built_files = row
    sources = _sources(home, root)
    raw = _raw_entries(home)
    if len(sources) + len(raw) != built_files:
        return True
    if any(p.stat().st_mtime > built_mtime for _, p, _r in sources):
        return True
    return any(m > built_mtime for _c, _k, _b, m in raw)


def index_stale(home, root=None):
    """Either index behind its sources: the keyword index by its own
    rule, the vector index when a source is newer than embeddings.json
    (or it is missing while an embedding service is configured)."""
    home = Path(home)
    if _index_stale(home, root):
        return True
    if _embedding_config(root) in (None, "broken"):
        return False
    try:
        built = _index_path(home).stat().st_mtime
    except OSError:
        return True
    return any(p.stat().st_mtime > built for _, p, _r in _sources(home, root))


def refresh_if_stale(home, root=None):
    """Bring both indexes level with the sources, embedding only what
    changed; the unattended counterpart of the refresh a search does.
    Returns what it did, or None when both were fresh. Never waits on a
    pass another process holds: that pass is doing this work already."""
    home = Path(home)
    if not index_stale(home, root):
        return None
    report = {"files": None, "embedded": 0, "failed": 0, "busy": False}
    if _index_stale(home, root):
        report["files"] = build_index(home, root)
    config = _embedding_config(root)
    if config not in (None, "broken"):
        out = ensure_index(home, config, root=root, wait=False)
        report.update(embedded=out["embedded"], failed=out["failed"],
                      busy=out["busy"])
    return report


def _sanitize(query, max_tokens=32):
    """Make a natural-language query safe for FTS5 MATCH. FTS5 treats
    -, /, ', ( as query syntax; a raw cousin question used to error,
    and the keyword leg swallowed it silently - a search that never
    errors and never matches is the worst version. Reduce to quoted
    alphanumeric tokens OR-joined."""
    tokens = re.findall(r"[A-Za-z0-9_]+", query)[:max_tokens]
    if not tokens:
        return ""
    return " OR ".join('"%s"' % t for t in tokens)


def _keyword_search(query, home, top, collection=None, root=None):
    if _index_stale(home, root):
        build_index(home, root)
    match = _sanitize(query)
    if not match:
        return []
    sql = ("SELECT collection, path, bm25(memory_fts) AS score,"
           " snippet(memory_fts, 2, '[', ']', '...', 12)"
           " FROM memory_fts WHERE memory_fts MATCH ?")
    params = [match]
    if collection:
        sql += " AND collection = ?"
        params.append(collection)
    sql += " ORDER BY score LIMIT ?"
    params.append(top)
    conn = wal(sqlite3.connect(_fts_path(home)))
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [
        {"collection": c, "path": p, "score": s, "snippet": snip,
         "chunk": 0, "similarity": None}
        for c, p, s, snip in rows
    ]


# ----------------------------------------------------------- semantic leg

def _embedding_config(root=None):
    """The semantic leg's declaration: <root>/config/embedding.toml
    with url, model, timeout_s and the optional chunk_chars,
    chunk_overlap and [recall] keys (defaults filled in). None when
    absent (nothing promised); the string 'broken' when present but
    unparsable (promised and not delivered - the caller must notice,
    not shrug)."""
    if root is None:
        root = _root()
    if root is None:
        return None
    path = Path(root) / "config" / "embedding.toml"
    if not path.exists():
        return None
    try:
        config = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return "broken"
    if not config.get("url"):
        return "broken"
    for key, default in _DEFAULTS.items():
        config.setdefault(key, default)
    recall = dict(_RECALL_DEFAULTS)
    recall.update(config.get("recall") or {})
    config["recall"] = recall
    return config


def _embed(text, config):
    body = {
        "model": config.get("model", ""),
        "prompt": text[:_EMBED_CAP_CHARS],
    }
    # [options] passes through to the service as is: for Ollama,
    # num_thread caps the cores one embedding takes (it uses them all
    # by default, which on a CPU-only host is the whole machine).
    if config.get("options"):
        body["options"] = dict(config["options"])
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        config["url"], data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(
            request, timeout=config.get("timeout_s", 10)) as response:
        return json.loads(response.read())["embedding"]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(
        sum(x * x for x in b))
    return dot / norm if norm else 0.0


def _chunk_text(body, *, size=2000, overlap=200):
    """Split a body into overlapping chunks of at most `size` chars.
    Overlap keeps a sentence that straddles a boundary findable in one
    piece. A short body is one chunk; an empty body is none; the step
    is always positive so a careless overlap cannot stall."""
    if not body:
        return []
    if len(body) <= size:
        return [body]
    step = max(1, size - overlap)
    chunks = []
    start = 0
    while start < len(body):
        chunks.append(body[start:start + size])
        start += step
    return chunks


def _text_hash(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _chunks(home, config, root=None):
    """Every embeddable chunk as {key: (collection, path, text)} with
    key "<collection>:<relpath>#<chunk>"."""
    size = int(config.get("chunk_chars", _DEFAULTS["chunk_chars"]))
    overlap = int(config.get("chunk_overlap", _DEFAULTS["chunk_overlap"]))
    out = {}
    for collection, path, rel in _sources(home, root):
        body = _source_text(collection, path, rel)
        if body is None:
            continue
        for i, text in enumerate(_chunk_text(body, size=size,
                                             overlap=overlap)):
            out["%s:%s#%d" % (collection, rel, i)] = (collection, path,
                                                       text)
    for collection, key, body, _mtime in _raw_entries(home):
        # An entry is its own chunk when it fits, which is the usual
        # case (median 881 chars against a 2000-char window); a long
        # one still splits rather than being truncated.
        for i, text in enumerate(_chunk_text(body, size=size,
                                             overlap=overlap)):
            out["%s:%s#%d" % (collection, key, i)] = (collection,
                                                      Path(key), text)
    return out


def _migrate_legacy_index(home):
    """Import a JSON index written before the store was SQLite, then
    remove it, so the migration happens once and nothing reads two
    stores. A JSON file that will not parse is left where it is: it is
    evidence, and the caller rebuilds from the sources anyway."""
    legacy = _legacy_index_path(home)
    try:
        data = json.loads(legacy.read_text())
    except OSError:
        return None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    _write_index_atomic(home, data)
    legacy.unlink(missing_ok=True)
    return _load_index(home)


def _load_index(home):
    """Every stored chunk as {key: {mtime, text_hash, vector}}, or None
    when there is no readable store. A vector that was never embedded
    (the embed failed and there was no prior one) comes back without a
    `vector` key, which is how the next pass tells known-but-unembedded
    from new."""
    path = _index_path(home)
    if not path.exists():
        return _migrate_legacy_index(home)
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT key, mtime, text_hash, vector FROM vectors"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    out = {}
    for key, mtime, text_hash, blob in rows:
        entry = {"mtime": mtime, "text_hash": text_hash}
        if blob:
            entry["vector"] = _unpack(blob)
        out[key] = entry
    return out


_VECTORS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS vectors ("
    " key TEXT PRIMARY KEY, mtime REAL, text_hash TEXT, vector BLOB)"
)


def _write_index_atomic(home, index):
    """Replace the store's contents with `index`, in one transaction:
    a reader never sees a half-written index and a crash mid-write
    leaves the previous contents intact. Keys absent from `index` are
    gone, which is what the JSON write did by rewriting the file."""
    path = _index_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = wal(sqlite3.connect(path))
        conn.execute(_VECTORS_SCHEMA)
    except sqlite3.DatabaseError:
        # Not a database, or one too damaged to open: the index is a
        # cache of what the sources say, so replace it rather than
        # refuse. Nothing is lost that a refresh cannot recompute.
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - never fail on the way out
            pass
        path.unlink(missing_ok=True)
        conn = wal(sqlite3.connect(path))
    try:
        with conn:
            conn.execute(_VECTORS_SCHEMA)
            conn.execute("DELETE FROM vectors WHERE key NOT IN (%s)"
                         % ",".join("?" * len(index)) if index
                         else "DELETE FROM vectors", tuple(index))
            conn.executemany(
                "INSERT OR REPLACE INTO vectors"
                " (key, mtime, text_hash, vector) VALUES (?, ?, ?, ?)",
                [(key, entry.get("mtime"), entry.get("text_hash"),
                  _pack(entry["vector"]) if entry.get("vector") else None)
                 for key, entry in index.items()])
    finally:
        conn.close()


def _lock_path(home):
    return Path(home) / "memory" / ".embeddings.lock"


# A foreground pass (a search) embeds at most this many chunks and
# leaves the rest to the loops daemon. A search must never pay for a
# whole backfill: with the raw store indexed a cousin has thousands of
# chunks, and on 2026-09-21 a peer's first query after the change sat
# over three minutes with the embedding service pinned.
FOREGROUND_BUDGET = 24


def ensure_index(home, config, *, force=False, root=None, wait=True,
                 budget=None):
    """Bring <home>/memory/embeddings.json up to date, embedding only
    what changed, and report what the pass did:

      {"embedded": chunks embedded this pass,
       "reused": chunks whose stored vector was kept,
       "dropped": stored keys whose chunk no longer exists,
       "failed": chunks the service could not embed,
       "stale_reason": why any work was needed, or None,
       "busy": True when another pass held the index and this one
               did nothing (only with wait=False),
       "incomplete": True when the pass stopped at its budget with
               work left, so a caller knows the index is still behind,
       "ranked": current chunks the store holds a vector for whose
               stored text_hash still matches, i.e. what the semantic
               leg ranks on current text; a carried-over or failed
               chunk keeps its old vector and does NOT count
               (None when busy),
       "total": current chunks in the corpus (None when busy)}

    One pass per home at a time, under an flock on
    memory/.embeddings.lock: concurrent searches each re-embedding the
    same chunks is a thundering herd on the embedding service (seen
    2026-09-18: one recall thread per operator message, 19 at once, the
    service at 18 s a request and every recall past its budget). With
    wait=False a held lock returns at once with busy=True and the
    caller searches the index as it stands; wait=True queues behind it.

    A chunk is unchanged when its text hash matches the stored one; an
    mtime bump alone is not a change. A failed embed keeps the prior
    vector when there is one (stale beats invisible) and stores nothing
    otherwise, so a dead service never poisons the index with empty
    vectors. The file is rewritten only when something changed, and
    every _CHECKPOINT_CHUNKS embeddings along the way."""
    home = Path(home)
    lock_path = _lock_path(home)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX
                        | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            return {"embedded": 0, "reused": 0, "dropped": 0,
                    "failed": 0, "stale_reason": None, "busy": True,
                    "incomplete": True, "ranked": None, "total": None}
        try:
            return _refresh_index(home, config, force=force, root=root,
                                  budget=budget)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _refresh_index(home, config, *, force, root, budget=None):
    chunks = _chunks(home, config, root)
    old = None if force else _load_index(home)
    missing = old is None
    if force:
        reason = "forced"
    elif missing:
        reason = "no index"
    elif not old and chunks:
        reason = "empty index over %d source chunk(s)" % len(chunks)
    else:
        reason = None
    old = old or {}

    index = {}
    embedded = reused = failed = 0
    fresh = 0
    incomplete = False
    for key, (_collection, path, text) in chunks.items():
        if budget is not None and embedded >= budget:
            # Out of budget: carry every key not reached, so a bounded
            # pass adds what it paid for and never drops the rest.
            incomplete = True
            for rest, entry in old.items():
                index.setdefault(rest, entry)
            break
        digest = _text_hash(text)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        prev = old.get(key)
        if (prev and prev.get("vector")
                and prev.get("text_hash") == digest):
            reused += 1
            index[key] = {"mtime": mtime, "vector": prev["vector"],
                          "text_hash": digest}
            continue
        fresh += 1
        try:
            vector = _embed(text, config)
        except Exception:
            failed += 1
            if prev and prev.get("vector"):
                index[key] = prev
            continue
        embedded += 1
        index[key] = {"mtime": mtime, "vector": vector,
                      "text_hash": digest}
        if embedded % _CHECKPOINT_CHUNKS == 0:
            # Everything embedded so far, and the prior entry for every
            # key not reached yet: a pass that dies here loses nothing
            # it already paid for.
            partial = dict(old)
            partial.update(index)
            _write_index_atomic(home, partial)
    dropped = 0 if incomplete else len(set(old) - set(chunks))
    if reason is None and (fresh or dropped):
        reason = "%d new or changed chunk(s), %d gone" % (fresh, dropped)
    if missing or fresh or dropped or index != old:
        _write_index_atomic(home, index)
    # What the semantic leg can rank against ON CURRENT TEXT. A vector
    # alone is not enough: the budget break carries old entries over
    # untouched, and a failed embed keeps the prior vector, so a chunk
    # whose text changed still holds a vector for text that is gone.
    # Counting those would report 30 of 30 while 6 were stale. Same
    # predicate `reused` uses above.
    ranked = sum(1 for key, (_c, _p, text) in chunks.items()
                 if (index.get(key) or {}).get("vector")
                 and (index.get(key) or {}).get("text_hash")
                 == _text_hash(text))
    return {"embedded": embedded, "reused": reused, "dropped": dropped,
            "failed": failed, "stale_reason": reason, "busy": False,
            "incomplete": incomplete, "ranked": ranked,
            "total": len(chunks)}


def _first_line(text):
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line[:_SNIPPET_CHARS]
    return ""


def _semantic_search(query, home, top, config, collection=None, root=None):
    """Embed the query, bring the index up to date, cosine-rank every
    chunk and keep the best chunk per file. The query is embedded
    FIRST: a dead service fails once here instead of once per file,
    and the failure raises to the caller, which degrades WITH a
    notice - the contract forbids this leg dying quietly. Returns
    (hits, failed) where failed counts chunks the pass could not
    embed."""
    query_vector = _embed(query, config)
    report = ensure_index(home, config, wait=False, root=root,
                          budget=FOREGROUND_BUDGET)
    index = _load_index(home) or {}
    chunks = _chunks(home, config, root)
    best = {}
    for key, (coll, path, text) in chunks.items():
        if collection and coll != collection:
            continue
        entry = index.get(key)
        if not entry or not entry.get("vector"):
            continue
        score = _cosine(query_vector, entry["vector"])
        chunk = int(key.rsplit("#", 1)[1])
        current = best.get(str(path))
        if current is None or score > current["score"]:
            best[str(path)] = {
                "path": str(path), "collection": coll, "score": score,
                "snippet": _first_line(text), "chunk": chunk,
                "similarity": score,
            }
    hits = sorted(best.values(), key=lambda h: h["score"], reverse=True)
    return hits[:top], report


# ----------------------------------------------------------------- fusion

def _fuse(keyword_hits, semantic_hits, top, bonuses=None):
    """Reciprocal-rank fusion by path: order-based, so the two legs'
    incomparable score scales never fight each other. The keyword
    snippet (with its match markers) wins when both legs found the
    file; the chunk and the similarity come from the semantic leg,
    which knows them. bonuses is {path: usage bonus}; each fused
    score is multiplied by (1 + bonus) before ranking, so a file that
    keeps being recalled is nudged up, never carried past a better
    match (the cap is reinforce.MAX_BONUS). A keyword-only search is
    a fusion with an empty semantic leg: one score scale everywhere,
    higher is better."""
    scores = {}
    byname = {}
    chunk_of = {}
    similarity_of = {}
    for hits in (keyword_hits, semantic_hits):
        for rank, hit in enumerate(hits):
            scores[hit["path"]] = scores.get(hit["path"], 0.0) \
                + 1.0 / (_RRF_K + rank)
            byname.setdefault(hit["path"], hit)
    for hit in semantic_hits:
        chunk_of[hit["path"]] = hit["chunk"]
        similarity_of[hit["path"]] = hit.get("similarity")
    for path, bonus in (bonuses or {}).items():
        if path in scores:
            scores[path] *= 1.0 + bonus
    ranked = sorted(scores, key=scores.get, reverse=True)[:top]
    out = []
    for path in ranked:
        hit = dict(byname[path])
        hit["score"] = scores[path]
        hit["chunk"] = chunk_of.get(path, hit.get("chunk", 0))
        hit["similarity"] = similarity_of.get(path, hit.get("similarity"))
        out.append(hit)
    return out


# The collections a cousin writes ON PURPOSE as durable topic files,
# best first. Raw entries are short and dense, so BM25's length
# normalisation ranks them above a long curated file that mentions the
# term once: measured 2026-09-21, indexing the raw store took curated
# files from 13 of 45 top-three slots to 2. One slot is reserved so the
# summary a cousin wrote cannot be crowded out of its own search.
_CURATED = ("memory", "harness")


def _curated_floor(ranked, top, query, home, root=None):
    """Keep one curated hit in the result when the ranking would drop
    every one of them.

    The curated hit is FETCHED, not hoped for: widening the pool does
    not reach it, because a flood of short entries can fill any pool
    (measured: a long topic file that names the term once ranked below
    ten entries that are almost entirely the term). One extra keyword
    query per collection, and only when the floor actually applies.

    The reserved slot is the LAST, so the best match is never
    displaced, and nothing is reserved when there is only one slot or
    when no curated file matches at all.
    """
    if top < 2 or any(h["collection"] in _CURATED for h in ranked):
        return ranked
    have = {h["path"] for h in ranked}
    for collection in _CURATED:
        for hit in _keyword_search(query, home, 1, collection, root):
            if hit["path"] not in have:
                return ranked[:top - 1] + [hit]
    return ranked


def _bonuses(home, *legs):
    """{path: usage bonus} for every path any leg surfaced. Fail-open:
    a broken reinforcement store means no bonus, never no search."""
    out = {}
    for hits in legs:
        for hit in hits:
            if hit["path"] in out:
                continue
            try:
                out[hit["path"]] = reinforce.bonus(home, hit["path"])
            except Exception:
                out[hit["path"]] = 0.0
    return out


def _record(home, query, hits):
    """Record what this search surfaced. Fail-open, same reason."""
    if not hits:
        return
    try:
        reinforce.record(home, [hit["path"] for hit in hits], query=query)
    except Exception:
        pass


def search(query, *, top=5, home=None, collection=None, root=None, record=True):
    """Ranked hits plus the degrade notice: (hits, notice). Each hit is
    {"path", "collection", "score", "snippet", "chunk", "similarity"}.
    collection limits both legs to one of memory, notes, harness.
    notice is None whenever the result honors everything the install's
    configuration promised, and a human-readable explanation whenever
    the semantic leg was promised and could not fully serve. Every
    return that carries hits applies the usage bonus and records the
    surfaced paths, the keyword-only ones included, unless record=False
    (a recall or a replay is not a search the cousin made, and must not
    reinforce what it measures). `root`: None
    discovers the framework root (the CLI, the tmux lane); a path is
    used as given and the environment is never read (the runner)."""
    home = Path(home) if home else _home()
    if collection in (None, "raw"):
        from cousin_lib import memory
        memory.try_backfill(home)   # decisions only the old log holds reach raw first (R2)
    depth = max(FUSION_DEPTH_MIN, FUSION_DEPTH_FACTOR * top)
    keyword_hits = _keyword_search(query, home, depth, collection, root)
    config = _embedding_config(root)
    semantic_hits = []
    notice = None
    if config == "broken":
        notice = ("embedding config exists but is unusable; keyword-only"
                  " results (fix or remove config/embedding.toml)")
    elif config is not None:
        try:
            semantic_hits, report = _semantic_search(
                query, home, depth, config, collection, root)
        except Exception as err:
            notice = ("embedding service unreachable (%s); keyword-only"
                      " results" % err)
        else:
            failed = report["failed"]
            if report.get("busy"):
                notice = ("another search is refreshing the index; ranked"
                          " by meaning against the index as it stands")
            elif report.get("incomplete"):
                # A bounded foreground pass leaves the rest to the
                # daemon. Without this the hits come back looking like
                # a complete result over the whole corpus.
                notice = ("the index is still catching up; %s of %s"
                          " chunk(s) ranked by meaning on current text,"
                          " the rest keyword-only or ranked on text that"
                          " has since changed, until the refresh"
                          " finishes"
                          % (report.get("ranked"), report.get("total")))
                if failed:
                    notice += (" (embedding service also failed for %d"
                               " chunk(s))" % failed)
            elif failed:
                notice = ("embedding service failed for %d chunk(s);"
                          " prior vectors kept where available, new"
                          " text unranked by meaning" % failed)
    # An explicit collection filter is never overridden: the caller
    # asked for one collection and gets one.
    # Bonuses are computed from each leg's first `top` entries only
    # (the pre-#83 reach): the depth-widened lists feed _fuse's rank
    # sums so a hit strong in both legs can still be found, but a
    # bonus must never let a path _fuse could not have fetched at
    # `top` before #83 (a low keyword-only or semantic-only rank)
    # outrank a path that was already within `top` on its own merit -
    # that would carry it past a better match, which _fuse's contract
    # forbids (ruling P1131-1).
    hits = _fuse(keyword_hits, semantic_hits, top,
                 _bonuses(home, keyword_hits[:top], semantic_hits[:top]))
    if collection is None:
        hits = _curated_floor(hits, top, query, home, root)
    if record:
        _record(home, query, hits)
    return hits, notice


def format_results(hits):
    if not hits:
        return "no matches"
    lines = []
    for i, hit in enumerate(hits, 1):
        lines.append("%d. [%.3f] [%s] %s" % (i, hit["score"],
                                              hit["collection"], hit["path"]))
        lines.append("   %s" % hit["snippet"].replace("\n", " "))
    return "\n".join(lines)


def print_results(hits):
    print(format_results(hits))


# ------------------------------------------------------- proactive recall
# The gates and the line the runner's UserPromptSubmit hook adds
# (runner/hooks.py default_recall).

RECALL_PREFIX = "[fw-recall] possibly relevant from your memory: "
RECALL_SUFFIX = " - cousin-memory search for details; ignore if not."


def recall_thresholds(root=None):
    """The [recall] table of config/embedding.toml, defaults when the
    seam is absent or unusable. Returns (thresholds, configured):
    configured says whether a semantic leg was promised, which decides
    how a hit qualifies."""
    config = _embedding_config(root)
    if isinstance(config, dict):
        return config["recall"], True
    return dict(_RECALL_DEFAULTS), config is not None


def _hit_title(path):
    """The file's first markdown heading, else its stem."""
    try:
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                if title:
                    return title
    except OSError:
        pass
    return path.stem


def raw_entry(key):
    """The raw entry a `raw` hit names ("<file>#<line>", the key
    _raw_entries gives it), or None when the line is gone or unreadable."""
    path, _, number = str(key).rpartition("#")
    try:
        wanted = int(number)
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if n == wanted:
                    entry = json.loads(line)
                    return entry if isinstance(entry, dict) else None
    except (OSError, ValueError, EOFError, zlib.error):
        return None
    return None


def _hit_name(hit):
    """What a recall line calls a hit: a raw entry by its topic (its
    file stem is only a date), a file by its first heading."""
    if hit.get("collection") == "raw":
        topic = str((raw_entry(hit["path"]) or {}).get("topic") or "").strip()
        if topic:
            return topic
    return _hit_title(Path(hit["path"]))


def _hit_relpath(home, hit, root=None):
    """<relpath> inside the hit's collection: memory/ and notes/ live
    under the home; the harness collection is wherever config/harness.toml
    put it. A path outside every known base falls back to its name."""
    path = Path(hit["path"])
    collection = hit.get("collection") or ""
    bases = [home / collection] if collection in ("memory", "notes") else []
    if collection == "harness":
        try:
            base_root = root if root is not None else FrameworkConfig.resolve().root
            template = (harness_config(base_root) or {}).get("auto_memory_dir")
            if template:
                bases.append(expand_harness_path(template, home))
        except MissingConfigError:
            pass
    for base in bases:
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.name


def recall_entries(home, text, *, config=None, root=None):
    """The hits that qualify for proactive recall, each rendered as
    'Title (collection:relpath)'; [] when a gate says no. The gates:
    `[memory] proactive_recall` (config, the cousin's CousinConfig,
    loaded from `home` when not given), `[recall] min_chars` and `top`,
    and how a hit qualifies: with the embedding seam configured by its
    semantic similarity against `[recall] min_score`; without it nothing
    qualifies unless the cousin opted in with `[memory]
    recall_keyword_only = true`, in which case every keyword hit does."""
    if config is None:
        from cousin_lib.config import CousinConfig
        config = CousinConfig.load(home)
    home = Path(home)
    if not config.proactive_recall:
        return []
    thresholds, configured = recall_thresholds(root)
    if not configured and not config.recall_keyword_only:
        # no semantic leg: a keyword match on an OR-joined query is too
        # loose to interrupt with; the cousin opts in per install
        return []
    if len(text.strip()) < int(thresholds["min_chars"]):
        return []
    hits, _notice = search(text, top=int(thresholds["top"]), home=home, root=root)
    kept = []
    for hit in hits:
        if configured:
            similarity = hit.get("similarity")
            if similarity is None or similarity < float(thresholds["min_score"]):
                continue
        kept.append("%s (%s:%s)" % (_hit_name(hit), hit.get("collection"),
                                    _hit_relpath(home, hit, root)))
    return kept


def recall_line(entries):
    """The one-line '[fw-recall] ...' text for `entries`, or None when
    there are none. Single-line by construction (the delivery paste
    cannot carry a newline)."""
    if not entries:
        return None
    return " ".join((RECALL_PREFIX + "; ".join(entries) + RECALL_SUFFIX).split())


def recall_context(home, text, *, config=None, root=None):
    """The '[fw-recall] ...' line for `text`, or None: recall_entries'
    gates, rendered by recall_line."""
    return recall_line(recall_entries(home, text, config=config, root=root))
