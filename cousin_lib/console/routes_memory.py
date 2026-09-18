"""Memory routes (docs/console-spec.md, "Memory and the shared-tier
review" and "Memory explorer").

`GET /api/memory`: the tree of shared/*.md and each cousin's
memory/*.md with size, age and a preview (the old flat view; kept for
callers that read it).

`/api/memory/<slug>/...`: the per-cousin explorer, a projection of
cousin_lib.memory_explorer (layers, raw entries, decisions, files) and
cousin_lib.memory_trash (remove to trash, restore). Every path is
confined to the cousin home by cousin_lib.home_files; the harness
layer reads its own configured directory the same way."""
from __future__ import annotations

import time
from pathlib import Path

from cousin_lib import home_files, memory_explorer, memory_trash
from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router
from cousin_lib.console._common import cousin_home
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


register()
