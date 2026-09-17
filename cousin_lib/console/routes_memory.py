"""`GET /api/memory` (docs/console-spec.md): the tree of shared/*.md
and each cousin's memory/*.md with size, age and a preview. Read-only;
the shared-tier review has its own module."""
from __future__ import annotations

import time
from pathlib import Path

from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router

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


def register():
    @router.route("GET", "/api/memory")
    def memory(req):
        return 200, {"tree": memory_tree(req.server.root)}


register()
