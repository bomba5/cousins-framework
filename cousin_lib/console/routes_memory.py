"""Memory routes (docs/reference/console-api.md, "Memory and the shared-tier
review" and "Memory explorer").

`GET /api/memory`: the tree of shared/*.md and each cousin's
memory/*.md with size, age and a preview (the old flat view; kept for
callers that read it).

`/api/memory/<slug>/...`: the per-cousin explorer, a projection of
cousin_lib.memory_explorer (layers, raw entries, decisions, files) and
cousin_lib.memory_trash (remove to trash, restore). Every path is
confined to the cousin home by cousin_lib.home_files; the harness
layer reads its own configured directory the same way.

The operator actions (search, remember, decide, history, the review
gate, distill/compact/reindex, the self-portrait, callbacks and
capsules) call the same library functions the `cousin-memory`,
`cousin-self-portrait`, `cousin-callback` and `cousin-reason` CLIs call.
A review verdict and a self-portrait commit are a person's act and need
a logged-in console user (so does a change to the shared reviewer list,
routes_shared.py). The operator's word needs the operator account: an
operator-level write, and retiring or dropping an operator-level claim
(the obsolete route, a review drop). The console has no roles, so "the
operator" is the logged-in user whose name is the cousin's `[operator]
name` (case aside)."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from cousin_lib import home_files, memory, memory_explorer, memory_trash
from cousin_lib.config import FrameworkConfig
from cousin_lib.console import longop, router
from cousin_lib.console._common import cousin_home, load_cousin
from cousin_lib.console.app import HttpError

PREVIEW_CHARS = 400
PER_DIR_CAP = 50


def _entries(directory, *, cap=None):
    out = {}
    try:
        files = sorted(p for p in Path(directory).glob("*.md") if p.is_file())
    except OSError:
        return out
    if cap:
        files = files[:cap]
    now = time.time()
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        try:
            preview = path.read_text(errors="replace")[:PREVIEW_CHARS]
        except OSError:
            preview = "(unreadable)"
        out[path.name] = {"size": st.st_size,
                          "updated": int(max(0, now - st.st_mtime)),
                          "preview": preview}
    return out


def memory_tree(root):
    tree = {"shared/": _entries(Path(root) / "shared")}
    for config in FrameworkConfig(root).list_cousins():
        tree[config.slug + "/"] = _entries(config.home / "memory",
                                           cap=PER_DIR_CAP)
    return tree


def refused(err):
    return HttpError(err.status, str(err))


def _who(req):
    return req.user or "console"


def _flag(value):
    return value in (True, 1, "1", "true", "yes", "on")


def register():
    @router.route("GET", "/api/memory")
    def memory(req):
        return 200, {"tree": memory_tree(req.server.root)}

    @router.route("GET", "/api/memory/{slug}/overview")
    def overview(req, slug):
        home = cousin_home(req.server, slug)
        body = memory_explorer.overview(home, root=req.server.root)
        body["slug"] = slug
        return 200, body

    @router.route("GET", "/api/memory/{slug}/raw")
    def raw(req, slug):
        home = cousin_home(req.server, slug)
        q = req.query
        levels = [l for l in (q.get("level") or "").split(",") if l]
        try:
            body = memory_explorer.raw_entries(
                home, levels=levels, topic=q.get("topic"), q=q.get("q"),
                source=q.get("source") or None, since=q.get("since") or None,
                until=q.get("until") or None, tier=q.get("tier") or "live",
                limit=req.int_query("limit", 200),
                offset=req.int_query("offset", 0))
        except ValueError as err:
            raise HttpError(400, str(err))
        return 200, body

    @router.route("GET", "/api/memory/{slug}/decisions")
    def decisions(req, slug):
        home = cousin_home(req.server, slug)
        return 200, memory_explorer.decisions(
            home, q=req.query.get("q"), limit=req.int_query("limit", 200),
            offset=req.int_query("offset", 0),
            archives=_flag(req.query.get("archives")))

    @router.route("GET", "/api/memory/{slug}/files")
    def files(req, slug):
        home = cousin_home(req.server, slug)
        layer = req.query.get("layer") or ""
        if layer not in memory_explorer.FILE_LAYERS:
            raise HttpError(400, "layer must be one of %s"
                            % ", ".join(memory_explorer.FILE_LAYERS))
        return 200, {"layer": layer, "files": memory_explorer.layer_files(
            home, layer, root=req.server.root)}

    @router.route("GET", "/api/memory/{slug}/file")
    def read_file(req, slug):
        home = cousin_home(req.server, slug)
        base = home
        if req.query.get("layer") == "harness":
            base = memory_explorer.harness_dir(home, req.server.root)
            if base is None:
                raise HttpError(404, "no harness auto-memory directory")
        try:
            return 200, home_files.read_text_page(
                base, req.query.get("path") or "",
                start=req.int_query("start", 1),
                count=req.int_query("count", home_files.DEFAULT_PAGE_LINES))
        except home_files.PathRefused as err:
            raise refused(err)

    @router.route("GET", "/api/memory/{slug}/tensions")
    def tensions(req, slug):
        """Topics whose live claims disagree (memory.tensions), for the
        operator to settle with an entry-level obsolete."""
        from cousin_lib import memory
        home = cousin_home(req.server, slug)
        return 200, {"tensions": json.loads(json.dumps(memory.tensions(home), default=str))}

    @router.route("GET", "/api/memory/{slug}/trash")
    def trash(req, slug):
        home = cousin_home(req.server, slug)
        return 200, {"batches": memory_trash.list_trash(home)}

    @router.route("POST", "/api/memory/{slug}/delete")
    def delete(req, slug):
        """{"kind": "entry"|"decision", "path", "line_no", "sha",
        "mirrors": bool} or {"kind": "file", "path", "legacy": bool}:
        a trash move, never a destruction."""
        home = cousin_home(req.server, slug)
        body = req.body
        kind = body.get("kind")
        try:
            if kind in ("entry", "decision"):
                try:
                    line_no = int(body.get("line_no"))
                except (TypeError, ValueError):
                    raise HttpError(400, "line_no must be an integer")
                path = str(body.get("path") or "")
                if kind == "decision" and path != "data/decisions.jsonl":
                    raise HttpError(400, "a decision lives in"
                                         " data/decisions.jsonl")
                if kind == "entry" and not path.startswith("memory/raw/"):
                    raise HttpError(400, "a raw entry lives in"
                                         " memory/raw/")
                refs = [(path, line_no, body.get("sha") or None)]
                if kind == "decision" and _flag(body.get("mirrors")):
                    refs += _decision_mirrors(home, line_no,
                                              body.get("sha"))
                manifest = memory_trash.trash_lines(home, refs, by=_who(req))
            elif kind == "file":
                manifest = memory_trash.trash_file(
                    home, str(body.get("path") or ""), by=_who(req),
                    allow_legacy=_flag(body.get("legacy")))
            else:
                raise HttpError(400, "kind must be entry, decision or file")
        except home_files.PathRefused as err:
            raise refused(err)
        effects = memory_trash.after_change(home, manifest)
        req.server.emit("memory-change", {"slug": slug, "action": "trash",
                                          "id": manifest["id"]})
        return 200, {"ok": True, "trash": manifest, "effects": effects}

    @router.route("POST", "/api/memory/{slug}/obsolete")
    def obsolete(req, slug):
        """{"topic", "why", "force"?, "entry"?}: append an L5_OBSOLETE entry
        for the topic, recorded as by the logged-in user, then regenerate
        the distilled views (which leave the topic out until a later entry
        revives it). With `entry` (a claim's id, from the tensions list)
        the mark retires that one claim and the topic stays. Nothing is
        removed from raw."""
        from cousin_lib import distill, memory

        home = cousin_home(req.server, slug)
        topic = req.body.get("topic")
        why = req.body.get("why")
        claim = req.body.get("entry")
        if not isinstance(topic, str) or not isinstance(why, str):
            raise HttpError(400, "topic and why must be strings")
        if claim is not None and not isinstance(claim, str):
            raise HttpError(400, "entry must be a claim id string")
        if not why.strip():
            raise HttpError(400, "a reason is required (why): an obsolete"
                                 " mark says what superseded the topic")
        _guard_operator_claims(req, slug, home, topic, claim or None)
        try:
            entry = memory.mark_obsolete(home, topic, why, by=_who(req),
                                         force=_flag(req.body.get("force")),
                                         source="console", entry=claim or None)
        except memory.ObsoleteRefused as err:
            raise HttpError(400, str(err))
        effects = {"distilled": False}
        try:
            report = distill.distill(home)
            effects = {"distilled": True,
                       "obsolete_topics": report.get("obsolete", 0)}
        except Exception as err:  # noqa: BLE001 - the mark is written
            effects["distill_error"] = "%s: %s" % (type(err).__name__, err)
        req.server.emit("memory-change", {"slug": slug,
                                          "action": "obsolete",
                                          "topic": entry["topic"]})
        return 200, {"ok": True, "entry": entry, "effects": effects}

    _register_actions()

    @router.route("POST", "/api/memory/{slug}/restore")
    def restore(req, slug):
        home = cousin_home(req.server, slug)
        try:
            manifest = memory_trash.restore(home, req.body.get("id"),
                                            by=_who(req))
        except home_files.PathRefused as err:
            raise refused(err)
        effects = memory_trash.after_change(home, manifest)
        req.server.emit("memory-change", {"slug": slug, "action": "restore",
                                          "id": manifest["id"]})
        return 200, {"ok": True, "restored": manifest, "effects": effects}


def _decision_mirrors(home, line_no, sha):
    """The raw mirror refs of decision line `line_no`, read from the
    live file (the same line the trash call will verify by hash)."""
    import json

    path = Path(home) / "data" / "decisions.jsonl"
    try:
        lines = path.read_bytes().decode("utf-8", "replace").splitlines()
        text = lines[line_no - 1]
        if sha and memory_trash.line_sha(text) != sha:
            return []
        entry = json.loads(text)
    except (OSError, IndexError, ValueError):
        return []
    return [(r["path"], r["line_no"], r["sha"])
            for r in memory_explorer.mirror_refs(home, entry)]



# ---------------------------------------------------------------- operator actions

# What a console user may write: a person is neither the framework (L1,
# written by the framework itself) nor a retirement (L5 has its own
# action, obsolete). "operator" only for the operator account.
WRITE_LEVELS = ("operator", "tool", "conclusion", "hypothesis")
NOTE_MAX = 300
SEARCH_COLLECTIONS = ("memory", "notes", "harness", "raw")
SEARCH_TOP_MAX = 50
PORTRAIT_MAX = 64 * 1024
PORTRAIT_SHA_CHARS = 16
MAINTAIN_ACTIONS = ("distill", "compact-raw", "compact-index", "reindex")
CALLBACKS_MAX = 1000
WHY_MAX = 500
# A review of more than this many verdicts runs as the cousin's long
# operation: each settle rewrites raw and redistills.
REVIEW_SYNC_MAX = 2


def _person(req, what):
    """The logged-in user, or a 403: `what` is a person's act, and with
    no logins the console cannot tell a person from a local process (a
    cousin included)."""
    if not req.server.users.configured() or not req.user:
        raise HttpError(403, "%s needs a logged-in console user; set up"
                             " logins with `cousin-console adduser <name>`"
                        % what)
    return req.user


def _operator_of(req, slug):
    return (load_cousin(req.server, slug).operator_name or "").strip() or None


def _is_operator(req, slug):
    """The logged-in user is this cousin's operator: the user name is the
    cousin.toml `[operator] name`, case aside. No logins, no user, or no
    operator configured: nobody is."""
    operator = _operator_of(req, slug)
    return bool(operator and req.user and req.server.users.configured()
                and req.user.strip().casefold() == operator.casefold())


def _guard_operator_claims(req, slug, home, topic, claim):
    """An operator-level claim is the operator's word, so retiring it is
    the operator account's act: an entry-level mark on an L0 claim, or a
    topic-level mark on a topic with a live L0 claim, is 403 for anyone
    else (and for everyone without a login)."""
    topic = str(topic or "").strip()
    rows = [r for r in memory.validity(home)
            if str(r.get("topic") or "").strip() == topic]
    if claim:
        rows = [r for r in rows if r["id"] == claim]
    else:
        rows = [r for r in rows if not r.get("valid_to")]
    if any(memory.normalize_level(r.get("truth_level")) == memory.OPERATOR_LEVEL
           for r in rows) and not _is_operator(req, slug):
        operator = _operator_of(req, slug)
        raise HttpError(403, "an operator-level claim is the operator's to"
                             " retire; only the operator account (%s) may"
                        % (operator or "none configured in [operator] name"))


def _console_cite(req, note):
    """The cite a console-written entry carries: who and when, filled
    here and never taken from the client, then the writer's own note."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    who = ("console user %s" % req.user) if req.user else "console (no login)"
    cite = "%s, %s" % (who, stamp)
    return cite + ("; " + note if note else "")


def _text(body, key, *, required=True, limit=None):
    value = body.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise HttpError(400, "%s must be a non-empty string" % key)
    value = value.strip()
    if limit and len(value) > limit:
        raise HttpError(400, "%s is longer than %d characters" % (key, limit))
    return value


def _write_level(req, slug, body):
    level = body.get("level") or "conclusion"
    if not isinstance(level, str) or level not in WRITE_LEVELS:
        raise HttpError(400, "level must be one of %s (framework entries are"
                             " the framework's, and obsolete is its own"
                             " action)" % ", ".join(WRITE_LEVELS))
    if level == "operator" and not _is_operator(req, slug):
        operator = _operator_of(req, slug)
        raise HttpError(403, "only the operator account (%s) may write an"
                             " operator-level entry" % (operator or "none"
                             " configured in [operator] name"))
    return level


def _rel(home, path, base=None):
    """A hit path as the UI shows and opens it: relative to the home (a
    harness hit: to the harness directory, which the file route reads
    with layer=harness), else the file's name."""
    for b in (base, home):
        if b is None:
            continue
        try:
            return Path(path).relative_to(b).as_posix()
        except ValueError:
            continue
    return Path(path).name


def _search_hits(req, home, query, top, collection):
    from cousin_lib import memory_search
    root = req.server.root
    config = memory_search._embedding_config(root)
    semantic = "off" if config is None else (
        "broken" if config == "broken" else "on")
    # record=False: the operator's search is not the cousin's, so it
    # never feeds the recall log or the usage bonus it measures.
    hits, notice = memory_search.search(query, top=top, home=home,
                                        collection=collection, root=root,
                                        record=False)
    # The fused hit no longer says which leg found it. With no semantic
    # leg every hit is a keyword hit; with one, one keyword query (at the
    # depth search fused at) tells them apart.
    semantic_ran = any(h.get("similarity") is not None for h in hits)
    words = {h["path"] for h in memory_search._keyword_search(
        query, home, max(memory_search.FUSION_DEPTH_MIN,
                         memory_search.FUSION_DEPTH_FACTOR * top),
        collection, root)} if semantic_ran else {h["path"] for h in hits}
    harness = memory_explorer.harness_dir(home, root)
    out = []
    for hit in hits:
        legs = (["keyword"] if hit["path"] in words else []) + (
            ["semantic"] if hit.get("similarity") is not None else [])
        file, _, line = str(hit["path"]).rpartition("#") \
            if hit.get("collection") == "raw" else (hit["path"], "", "")
        base = harness if hit.get("collection") == "harness" else None
        row = {"collection": hit.get("collection"),
               "rel": _rel(home, file, base) + ("#" + line if line else ""),
               "score": hit.get("score"), "similarity": hit.get("similarity"),
               "snippet": hit.get("snippet") or "", "legs": legs}
        if hit.get("collection") == "raw":
            entry = memory_search.raw_entry(hit["path"]) or {}
            row["entry"] = {
                "topic": entry.get("topic"), "content": entry.get("content"),
                "level": memory.normalize_level(entry.get("truth_level")),
                "timestamp": entry.get("timestamp"), "cite": entry.get("cite"),
                "id": memory.entry_id(entry) if entry else None}
        elif hit.get("collection") == "harness":
            row["layer"] = "harness"
        out.append(row)
    return {"query": query, "hits": out, "semantic": semantic,
            "notice": notice}


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:PORTRAIT_SHA_CHARS]


def _confined(home, path):
    """Refuse a fixed store path (the portrait, callbacks, capsules) that a
    link planted at its name leads out of the home: never served."""
    try:
        home_files.resolve_in(home, Path(path).relative_to(home).as_posix())
    except home_files.PathRefused as err:
        raise refused(err)


def _portrait_read(home, path):
    from cousin_lib import self_portrait
    _confined(home, path)
    return self_portrait._read(path)


def _portrait_state(home):
    import difflib
    from cousin_lib import self_portrait
    cand_path = self_portrait.candidate_path(home)
    comm_path = self_portrait.committed_path(home)
    candidate = _portrait_read(home, cand_path)
    committed = _portrait_read(home, comm_path)
    diff = "".join(difflib.unified_diff(
        committed.splitlines(keepends=True),
        candidate.splitlines(keepends=True),
        fromfile="committed", tofile="candidate")) if cand_path.exists() else ""
    return {"candidate": candidate, "committed": committed,
            "candidate_exists": cand_path.exists(),
            "committed_exists": comm_path.exists(),
            "backup_exists": (Path(home) / ".self-portrait.md.bak").exists(),
            "candidate_sha": _sha(candidate) if cand_path.exists() else None,
            "diff": diff}


def _no_link(home, path):
    """Refuse a portrait path that is a symlink, dangling or not: a write
    through it would land wherever the link points."""
    if Path(path).is_symlink():
        raise HttpError(403, "%s is a link; the console never writes"
                             " through one" % Path(path).relative_to(home))


def _write_candidate(home, text):
    import os
    import tempfile
    from cousin_lib import self_portrait
    path = self_portrait.candidate_path(home)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _maintain_work(server, slug, home, action, dry_run):
    """The op body for one maintenance action: the library call the
    `cousin-memory` subcommand makes, its report as the result."""
    def work(op):
        from cousin_lib import compact, distill, memory_search, raw_fold
        op.stage(action, "running")
        if action == "distill":
            report = distill.distill(home)
            result = {"ok": True, "report": report}
            detail = "%d topics from %d entries" % (report["topics"],
                                                   report["entries"])
        elif action == "compact-raw":
            report = raw_fold.fold_raw(home)
            result = {"ok": True, "report": report}
            detail = "%d day files folded" % report["folded_days"]
        elif action == "compact-index":
            report = compact.compact_index(home, dry_run=dry_run)
            if not report.get("ok"):
                raise longop.OpError(report.get("reason") or "compact failed")
            result = {"ok": True, "report": report}
            names = report["would_retire"] if dry_run else report["retired"]
            detail = "%s %d pointer(s)" % ("would retire" if dry_run
                                           else "retired", len(names))
        else:
            root = server.root
            count = memory_search.build_index(home, root=root)
            config = memory_search._embedding_config(root)
            result = {"ok": True, "indexed": count,
                      "semantic": "off" if config is None else (
                          "broken" if config == "broken" else "on")}
            detail = "%d file(s) indexed" % count
            if isinstance(config, dict):
                op.stage(action, "running", detail + ", embedding")
                report = memory_search.ensure_index(home, config, force=True,
                                                    root=root)
                result["embedded"] = report.get("embedded", 0)
                result["failed"] = report.get("failed", 0)
                detail += ", %d chunk(s) embedded, %d failed" % (
                    result["embedded"], result["failed"])
        op.stage(action, "done", detail)
        server.emit("memory-change", {"slug": slug, "action": action})
        return result
    return work


def _settle(server, slug, home, verdicts, why, user, operator):
    """Apply the operator's verdicts, then redistill once. review_gate.settle
    reads what is held once, under one lock; an id it does not hold comes
    back in errors. A person who is not the operator account is held to the
    reviewing model's rule (`model`): an operator-level entry is the
    operator's to drop, and stays held."""
    from cousin_lib import distill, review_gate
    done, errors = review_gate.settle(
        home, [{"id": i} for i in verdicts], verdicts,
        by="console:%s" % user, why=why, model=not operator)
    effects = {"distilled": False}
    if done:
        try:
            distill.distill(home)
            effects["distilled"] = True
        except Exception as err:  # noqa: BLE001 - the verdicts are written
            effects["distill_error"] = "%s: %s" % (type(err).__name__, err)
        server.emit("memory-change", {"slug": slug, "action": "review"})
    return {"ok": True, "done": done, "errors": errors, "effects": effects}


def _register_actions():
    @router.route("GET", "/api/memory/{slug}/search")
    def search(req, slug):
        home = cousin_home(req.server, slug)
        query = (req.query.get("q") or "").strip()
        if not query:
            raise HttpError(400, "q is required")
        collection = req.query.get("collection") or None
        if collection is not None and collection not in SEARCH_COLLECTIONS:
            raise HttpError(400, "collection must be one of %s"
                            % ", ".join(SEARCH_COLLECTIONS))
        top = max(1, min(SEARCH_TOP_MAX, req.int_query("top", 10)))
        return 200, _search_hits(req, home, query, top, collection)

    @router.route("GET", "/api/memory/{slug}/writer")
    def writer(req, slug):
        """What the write forms may offer this user on this cousin."""
        cousin_home(req.server, slug)
        can = _is_operator(req, slug)
        return 200, {"user": req.user, "operator": _operator_of(req, slug),
                     "can_write_operator": can,
                     "levels": [l for l in WRITE_LEVELS
                                if l != "operator" or can],
                     "cite_preview": _console_cite(req, "")}

    @router.route("POST", "/api/memory/{slug}/remember")
    def remember(req, slug):
        home = cousin_home(req.server, slug)
        body = req.body
        topic = _text(body, "topic", limit=200)
        fact = _text(body, "fact", limit=4000)
        note = _text(body, "note", required=False, limit=NOTE_MAX)
        level = _write_level(req, slug, body)
        try:
            line = memory.remember(home, topic, fact, level=level,
                                   cite=_console_cite(req, note))
        except ValueError as err:
            raise HttpError(400, str(err))
        req.server.emit("memory-change", {"slug": slug, "action": "remember",
                                          "topic": topic})
        return 200, {"ok": True, "line": line}

    @router.route("POST", "/api/memory/{slug}/decide")
    def decide(req, slug):
        home = cousin_home(req.server, slug)
        body = req.body
        topic = _text(body, "topic", limit=200)
        decision = _text(body, "decision", limit=4000)
        reasoning = _text(body, "reasoning", limit=4000)
        note = _text(body, "note", required=False, limit=NOTE_MAX)
        level = _write_level(req, slug, body)
        try:
            line = memory.decide(home, topic, decision, reasoning, level=level,
                                 cite=_console_cite(req, note))
        except ValueError as err:
            raise HttpError(400, str(err))
        req.server.emit("memory-change", {"slug": slug, "action": "decide",
                                          "topic": topic})
        return 200, {"ok": True, "line": line}

    @router.route("GET", "/api/memory/{slug}/history")
    def history(req, slug):
        """A topic's claims, oldest first, with their valid time
        (`cousin-memory history`)."""
        home = cousin_home(req.server, slug)
        topic = (req.query.get("topic") or "").strip()
        if not topic:
            raise HttpError(400, "topic is required")
        rows = [r for r in memory.validity(home)
                if str(r.get("topic") or "").strip() == topic]
        return 200, {"topic": topic, "claims": _jsonable(rows)}

    @router.route("GET", "/api/memory/{slug}/review")
    def review_list(req, slug):
        from cousin_lib import review_gate
        home = cousin_home(req.server, slug)
        return 200, {"held": _jsonable(review_gate.pending(home)),
                     "batch": review_gate.batch_limit(home),
                     "operator": _operator_of(req, slug),
                     "is_operator": _is_operator(req, slug)}

    @router.route("POST", "/api/memory/{slug}/review")
    def review_settle(req, slug):
        """{"verdicts": {id: "keep"|"drop"}, "why"?}: the operator's side
        of the review gate (`cousin-memory review --keep/--drop`). A
        person's act: a logged-in user, recorded as `console:<user>`. An
        operator-level entry is dropped by the operator account only (a
        drop has no undo), as the reviewing model is held to the same."""
        from cousin_lib import review_gate
        home = cousin_home(req.server, slug)
        verdicts = req.body.get("verdicts")
        if not isinstance(verdicts, dict) or not verdicts or any(
                not isinstance(k, str) or v not in review_gate.VERDICTS
                for k, v in verdicts.items()):
            raise HttpError(400, "verdicts must map entry ids to keep or drop")
        why = req.body.get("why") or ""
        if not isinstance(why, str) or len(why) > WHY_MAX:
            raise HttpError(400, "why must be a string of at most %d"
                                 " characters" % WHY_MAX)
        user = _person(req, "a review verdict")
        operator = _is_operator(req, slug)

        def work(op=None):
            if op:
                op.stage("settle", "running", "%d verdict(s)" % len(verdicts))
            result = _settle(req.server, slug, home, verdicts, why, user,
                             operator)
            if op:
                op.stage("settle", "done", "%d settled, %d left held" % (
                    len(result["done"]), len(result["errors"])))
            return result
        if len(verdicts) > REVIEW_SYNC_MAX:
            return longop.start_response(req.server, slug, "memory-review", work,
                                         params={"verdicts": len(verdicts)})
        return 200, work()

    @router.route("POST", "/api/memory/{slug}/maintain")
    def maintain(req, slug):
        """{"action": distill|compact-raw|compact-index|reindex,
        "dry_run"?}: one maintenance run as the cousin's long operation."""
        home = cousin_home(req.server, slug)
        action = req.body.get("action")
        if action not in MAINTAIN_ACTIONS:
            raise HttpError(400, "action must be one of %s"
                            % ", ".join(MAINTAIN_ACTIONS))
        dry_run = action == "compact-index" and _flag(req.body.get("dry_run"))
        return longop.start_response(
            req.server, slug, "memory-" + action,
            _maintain_work(req.server, slug, home, action, dry_run),
            params={"action": action, "dry_run": dry_run})

    @router.route("GET", "/api/memory/{slug}/portrait")
    def portrait(req, slug):
        return 200, _portrait_state(cousin_home(req.server, slug))

    @router.route("POST", "/api/memory/{slug}/portrait/synthesize")
    def portrait_synthesize(req, slug):
        """Draft a candidate from the cousin's sources (no model call).
        An existing candidate may hold edits: `replace: true` to draft
        over it, else 409."""
        from cousin_lib import self_portrait
        home = cousin_home(req.server, slug)
        _no_link(home, self_portrait.candidate_path(home))
        if self_portrait.candidate_path(home).exists() \
                and not _flag(req.body.get("replace")):
            raise HttpError(409, "a candidate exists and may hold edits;"
                                 " pass replace to draft over it")
        self_portrait.synthesize_candidate(home, slug)
        req.server.emit("memory-change", {"slug": slug,
                                          "action": "portrait-synthesize"})
        return 200, {"ok": True, **_portrait_state(home)}

    @router.route("POST", "/api/memory/{slug}/portrait/candidate")
    def portrait_candidate(req, slug):
        home = cousin_home(req.server, slug)
        text = req.body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HttpError(400, "text must be a non-empty string")
        if len(text.encode("utf-8")) > PORTRAIT_MAX:
            raise HttpError(400, "a portrait is at most %d bytes"
                            % PORTRAIT_MAX)
        from cousin_lib import self_portrait
        _no_link(home, self_portrait.candidate_path(home))
        _write_candidate(home, text)
        req.server.emit("memory-change", {"slug": slug,
                                          "action": "portrait-edit"})
        return 200, {"ok": True, **_portrait_state(home)}

    @router.route("POST", "/api/memory/{slug}/portrait/commit")
    def portrait_commit(req, slug):
        """{"confirm": "<slug>", "sha": "<candidate_sha>"}: promote the
        reviewed candidate (`cousin-self-portrait commit`). The identity
        gate: a logged-in person, the cousin's slug typed back, and the
        candidate still the one they read (a changed candidate is 409)."""
        from cousin_lib import self_portrait
        home = cousin_home(req.server, slug)
        user = _person(req, "a self-portrait commit")
        if req.body.get("confirm") != slug:
            raise HttpError(400, "type the cousin's slug (%s) to commit" % slug)
        _no_link(home, self_portrait.candidate_path(home))
        _no_link(home, self_portrait.committed_path(home))
        state = _portrait_state(home)
        if not state["candidate_exists"]:
            raise HttpError(404, "no candidate to commit; synthesize or"
                                 " write one first")
        if req.body.get("sha") != state["candidate_sha"]:
            raise HttpError(409, "the candidate changed since it was read;"
                                 " review it again",
                            candidate_sha=state["candidate_sha"])
        self_portrait.commit_candidate(home)
        req.server.emit("memory-change", {"slug": slug,
                                          "action": "portrait-commit",
                                          "by": user})
        return 200, {"ok": True, "by": user, **_portrait_state(home)}

    @router.route("GET", "/api/memory/{slug}/callbacks")
    def callbacks(req, slug):
        from cousin_lib import callback
        home = cousin_home(req.server, slug)
        _confined(home, callback.library_path(home))
        rows = callback.list_all(home)
        rows.reverse()
        limit = max(1, min(CALLBACKS_MAX, req.int_query("limit", 200)))
        return 200, {"callbacks": rows[:limit]}

    @router.route("GET", "/api/memory/{slug}/capsules")
    def capsules(req, slug):
        from cousin_lib import capsule
        home = cousin_home(req.server, slug)
        n = max(1, min(500, req.int_query("n", 50)))
        _confined(home, capsule.capsules_path(home))
        return 200, {"capsules": capsule.list_capsules(home, n)}


register()
