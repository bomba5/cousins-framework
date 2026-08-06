"""Search over a cousin's memory and notes: keyword always, semantic
when an embedding service is configured.

The keyword leg is SQLite FTS5 with BM25; the semantic leg embeds
files and queries through an HTTP embedding service declared in
<root>/config/embedding.toml (url, model, timeout_s - the endpoint
accepts {"model", "prompt"} and returns {"embedding": [...]}; front
any service with that contract). Results merge by reciprocal-rank
fusion.

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
import json
import math
import os
import re
import sqlite3
import time
import tomllib
import urllib.request
from pathlib import Path

from cousin_lib.config import FrameworkConfig, MissingConfigError

_EMBED_CAP_CHARS = 6000
_RRF_K = 60


def _home():
    home = os.environ.get("COUSIN_HOME")
    if not home:
        raise MissingConfigError(
            "COUSIN_HOME is not set; refusing to search without a"
            " cousin context"
        )
    return Path(home)


def _fts_path(home):
    return home / "memory" / "fts_index.db"


def _sources(home):
    """(collection, path) pairs for every indexable file. The memory
    and notes directories are the cousin's whole searchable surface in
    v1; index artifacts are excluded by suffix."""
    out = []
    for collection, sub in (("memory", "memory"), ("notes", "notes")):
        base = home / sub
        if base.is_dir():
            for path in sorted(base.rglob("*.md")):
                out.append((collection, path))
    return out


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
        for collection, path in _sources(home):
            try:
                body = path.read_text(errors="replace")
            except OSError:
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
    return any(p.stat().st_mtime > built_mtime for _, p in sources)


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


def _keyword_search(query, home, top):
    if _index_stale(home):
        build_index(home)
    match = _sanitize(query)
    if not match:
        return []
    conn = sqlite3.connect(_fts_path(home))
    try:
        rows = conn.execute(
            "SELECT collection, path, bm25(memory_fts) AS score,"
            " snippet(memory_fts, 2, '[', ']', '...', 12)"
            " FROM memory_fts WHERE memory_fts MATCH ?"
            " ORDER BY score LIMIT ?",
            (match, top),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [
        {"collection": c, "path": p, "score": s, "snippet": snip}
        for c, p, s, snip in rows
    ]


def _embedding_config():
    """The semantic leg's declaration: <root>/config/embedding.toml
    with url, model, timeout_s. None when absent (nothing promised);
    the string 'broken' when present but unparsable (promised and not
    delivered - the caller must notice, not shrug)."""
    try:
        path = FrameworkConfig.from_env().root / "config" \
            / "embedding.toml"
    except MissingConfigError:
        return None
    if not path.exists():
        return None
    try:
        config = tomllib.loads(path.read_text())
        if not config.get("url"):
            return "broken"
        return config
    except (OSError, tomllib.TOMLDecodeError):
        return "broken"


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


def _semantic_search(query, home, top, config):
    """Embed stale files, cosine-rank against the query. Any failure
    raises to the caller, which degrades WITH a notice - the contract
    forbids this leg dying quietly."""
    index_path = home / "memory" / "embeddings.json"
    try:
        index = json.loads(index_path.read_text())
    except (OSError, ValueError):
        index = {}
    changed = False
    current = {}
    for collection, path in _sources(home):
        key = str(path)
        mtime = path.stat().st_mtime
        entry = index.get(key)
        if not entry or entry["mtime"] < mtime:
            entry = {
                "mtime": mtime,
                "collection": collection,
                "vector": _embed(path.read_text(errors="replace"),
                                 config),
            }
            changed = True
        current[key] = entry
    if changed or set(current) != set(index):
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(current))
    query_vector = _embed(query, config)
    scored = sorted(
        ((_cosine(query_vector, e["vector"]), key, e)
         for key, e in current.items()),
        reverse=True,
    )
    hits = []
    for score, key, entry in scored[:top]:
        first_line = Path(key).read_text(errors="replace") \
            .strip().splitlines()
        hits.append({
            "collection": entry["collection"], "path": key,
            "score": score,
            "snippet": first_line[0] if first_line else "",
        })
    return hits


def _fuse(keyword_hits, semantic_hits, top):
    """Reciprocal-rank fusion by path: order-based, so the two legs'
    incomparable score scales never fight each other."""
    scores = {}
    byname = {}
    for hits in (keyword_hits, semantic_hits):
        for rank, hit in enumerate(hits):
            scores[hit["path"]] = scores.get(hit["path"], 0.0) \
                + 1.0 / (_RRF_K + rank)
            byname.setdefault(hit["path"], hit)
    ranked = sorted(scores, key=scores.get, reverse=True)[:top]
    out = []
    for path in ranked:
        hit = dict(byname[path])
        hit["score"] = scores[path]
        out.append(hit)
    return out


def search(query, *, top=5, home=None):
    """Ranked hits plus the degrade notice: (hits, notice). notice is
    None whenever the result honors everything the install's
    configuration promised, and a human-readable explanation whenever
    the semantic leg was promised and could not serve."""
    home = Path(home) if home else _home()
    keyword_hits = _keyword_search(query, home, top)
    config = _embedding_config()
    if config is None:
        return keyword_hits, None
    if config == "broken":
        return keyword_hits, (
            "embedding config exists but is unusable; keyword-only"
            " results (fix or remove config/embedding.toml)")
    try:
        semantic_hits = _semantic_search(query, home, top, config)
    except Exception as err:
        return keyword_hits, (
            "embedding service unreachable (%s); keyword-only results"
            % err)
    return _fuse(keyword_hits, semantic_hits, top), None


def print_results(hits):
    if not hits:
        print("no matches")
        return
    for i, hit in enumerate(hits, 1):
        print("%d. [%.3f] [%s] %s" % (i, hit["score"],
                                      hit["collection"], hit["path"]))
        print("   %s" % hit["snippet"].replace("\n", " "))
