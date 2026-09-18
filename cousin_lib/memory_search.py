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
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import tomllib
import urllib.request
from pathlib import Path

from cousin_lib import reinforce
from cousin_lib.config import (FrameworkConfig, MissingConfigError,
                               expand_harness_path, harness_config)

_EMBED_CAP_CHARS = 6000
_RRF_K = 60
_SNIPPET_CHARS = 160
_DEFAULTS = {"chunk_chars": 2000, "chunk_overlap": 200}
_RECALL_DEFAULTS = {"min_chars": 24, "min_score": 0.45, "top": 3}
_TRASH_DIR = ".trash"


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
    return home / "memory" / "embeddings.json"


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
    out = []
    for collection, base in bases:
        if base.is_dir():
            for path in sorted(base.rglob("*.md")):
                rel = path.relative_to(base)
                # The memory trash (cousin_lib.memory_trash) keeps
                # removed files for restore; removed is not recalled.
                if _TRASH_DIR in rel.parts:
                    continue
                out.append((collection, path, rel.as_posix()))
    return out


def _read(path):
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


# ------------------------------------------------------------ keyword leg

def build_index(home=None):
    """(Re)build the FTS index over the current sources. Returns the
    number of files indexed."""
    home = Path(home) if home else _home()
    db_path = _fts_path(home)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS memory_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE memory_fts USING fts5("
            " collection, path, body)"
        )
        count = 0
        latest = 0.0
        for collection, path, _rel in _sources(home):
            body = _read(path)
            if body is None:
                continue
            conn.execute(
                "INSERT INTO memory_fts (collection, path, body)"
                " VALUES (?, ?, ?)",
                (collection, str(path), body),
            )
            latest = max(latest, path.stat().st_mtime)
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


def _index_stale(home):
    """The index is stale when any source is newer than the newest
    source at build time, or when the file counts differ (a deleted
    file also invalidates)."""
    db_path = _fts_path(home)
    if not db_path.exists():
        return True
    conn = sqlite3.connect(db_path)
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
    sources = _sources(home)
    if len(sources) != built_files:
        return True
    return any(p.stat().st_mtime > built_mtime for _, p, _r in sources)


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


def _keyword_search(query, home, top, collection=None):
    if _index_stale(home):
        build_index(home)
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
    conn = sqlite3.connect(_fts_path(home))
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
    payload = json.dumps({
        "model": config.get("model", ""),
        "prompt": text[:_EMBED_CAP_CHARS],
    }).encode()
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
        body = _read(path)
        if body is None:
            continue
        for i, text in enumerate(_chunk_text(body, size=size,
                                             overlap=overlap)):
            out["%s:%s#%d" % (collection, rel, i)] = (collection, path,
                                                       text)
    return out


def _load_index(home):
    try:
        data = json.loads(_index_path(home).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_index_atomic(home, index):
    """Write then rename: a reader never sees a half-written index and
    a crash mid-write leaves the previous one intact."""
    path = _index_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(index))
    os.replace(tmp, path)


def ensure_index(home, config, *, force=False, root=None):
    """Bring <home>/memory/embeddings.json up to date, embedding only
    what changed, and report what the pass did:

      {"embedded": chunks embedded this pass,
       "reused": chunks whose stored vector was kept,
       "dropped": stored keys whose chunk no longer exists,
       "failed": chunks the service could not embed,
       "stale_reason": why any work was needed, or None}

    A chunk is unchanged when its text hash matches the stored one; an
    mtime bump alone is not a change. A failed embed keeps the prior
    vector when there is one (stale beats invisible) and stores nothing
    otherwise, so a dead service never poisons the index with empty
    vectors. The file is rewritten only when something changed."""
    home = Path(home)
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
    for key, (_collection, path, text) in chunks.items():
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
    dropped = len(set(old) - set(chunks))
    if reason is None and (fresh or dropped):
        reason = "%d new or changed chunk(s), %d gone" % (fresh, dropped)
    if missing or fresh or dropped or index != old:
        _write_index_atomic(home, index)
    return {"embedded": embedded, "reused": reused, "dropped": dropped,
            "failed": failed, "stale_reason": reason}


def _first_line(text):
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line[:_SNIPPET_CHARS]
    return ""


def _semantic_search(query, home, top, config, collection=None):
    """Embed the query, bring the index up to date, cosine-rank every
    chunk and keep the best chunk per file. The query is embedded
    FIRST: a dead service fails once here instead of once per file,
    and the failure raises to the caller, which degrades WITH a
    notice - the contract forbids this leg dying quietly. Returns
    (hits, failed) where failed counts chunks the pass could not
    embed."""
    query_vector = _embed(query, config)
    report = ensure_index(home, config)
    index = _load_index(home) or {}
    chunks = _chunks(home, config)
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
    return hits[:top], report["failed"]


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


def search(query, *, top=5, home=None, collection=None):
    """Ranked hits plus the degrade notice: (hits, notice). Each hit is
    {"path", "collection", "score", "snippet", "chunk", "similarity"}.
    collection limits both legs to one of memory, notes, harness.
    notice is None whenever the result honors everything the install's
    configuration promised, and a human-readable explanation whenever
    the semantic leg was promised and could not fully serve. Every
    return that carries hits applies the usage bonus and records the
    surfaced paths, the keyword-only ones included."""
    home = Path(home) if home else _home()
    keyword_hits = _keyword_search(query, home, top, collection)
    config = _embedding_config()
    semantic_hits = []
    notice = None
    if config == "broken":
        notice = ("embedding config exists but is unusable; keyword-only"
                  " results (fix or remove config/embedding.toml)")
    elif config is not None:
        try:
            semantic_hits, failed = _semantic_search(
                query, home, top, config, collection)
        except Exception as err:
            notice = ("embedding service unreachable (%s); keyword-only"
                      " results" % err)
        else:
            if failed:
                notice = ("embedding service failed for %d chunk(s);"
                          " prior vectors kept where available, new"
                          " text unranked by meaning" % failed)
    hits = _fuse(keyword_hits, semantic_hits, top,
                 _bonuses(home, keyword_hits, semantic_hits))
    _record(home, query, hits)
    return hits, notice


def print_results(hits):
    if not hits:
        print("no matches")
        return
    for i, hit in enumerate(hits, 1):
        print("%d. [%.3f] [%s] %s" % (i, hit["score"],
                                      hit["collection"], hit["path"]))
        print("   %s" % hit["snippet"].replace("\n", " "))
