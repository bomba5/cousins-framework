"""Static files for the console: the committed frontend under
`cousin_lib/console_static/` (docs/console-spec.md, "Static files").

`serve_static(path) -> (status, headers, bytes)` is the whole surface;
the server calls it for every non-`/api/` GET. Suffix allowlist, a
resolve-then-contain traversal check (403 outside the root, 404 for
anything else that is not a served file), `Cache-Control: no-store` so
a redeploy is never masked by a browser cache, and `index.html` served
for `/` with a `?v=<mtime>` stamp on each local `src`/`href` for the
same reason.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "console_static"

# An allowlist rather than a denylist: a file type nobody thought about
# is a file type that does not get served.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".jsx": "text/jsx; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

_REF_RE = re.compile(r'\b(src|href)="([^"?#]+)"')
_REMOTE_PREFIXES = ("http://", "https://", "//", "data:", "mailto:")


class RawResponse:
    """A non-JSON body a route handler returns as the second element of
    its (status, body) pair: the server writes `headers` then `body`
    verbatim. Shared by the static server and the inbound-file route."""

    __slots__ = ("headers", "body")

    def __init__(self, headers, body):
        self.headers = list(headers)
        self.body = body


def _error(status, message):
    body = json.dumps({"error": message}).encode()
    return status, [("Content-Type", "application/json"),
                    ("Cache-Control", "no-store"),
                    ("Content-Length", str(len(body)))], body


def _stamp_index(html, root):
    """Append ?v=<mtime> to every local src/href so a redeploy is seen
    at once. Remote URLs, fragments and data: URIs are left alone; the
    stamp is the referenced file's own mtime when it exists, else the
    page's."""
    index_mtime = int((root / "index.html").stat().st_mtime)

    def repl(m):
        attr, ref = m.group(1), m.group(2)
        if ref.startswith(_REMOTE_PREFIXES):
            return m.group(0)
        target = root / ref.lstrip("/")
        try:
            stamp = int(target.stat().st_mtime) if target.is_file() \
                else index_mtime
        except OSError:
            stamp = index_mtime
        return '%s="%s?v=%d"' % (attr, ref, stamp)

    return _REF_RE.sub(repl, html)


def serve_static(path, *, root=None):
    """(status, headers, body) for a request path. `root` defaults to
    the committed static directory; tests point it elsewhere."""
    root = Path(root) if root is not None else STATIC_DIR
    name = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
    name = name.lstrip("/") or "index.html"
    try:
        base = root.resolve()
        candidate = (root / name).resolve()
    except OSError:
        return _error(404, "not found")
    if candidate != base and not candidate.is_relative_to(base):
        return _error(403, "forbidden")
    if candidate.suffix.lower() not in STATIC_TYPES \
            or not candidate.is_file():
        return _error(404, "not found")
    try:
        payload = candidate.read_bytes()
    except OSError:
        return _error(404, "not found")
    if candidate.name == "index.html" and candidate.parent == base:
        payload = _stamp_index(payload.decode("utf-8", "replace"),
                               base).encode()
    headers = [("Content-Type", STATIC_TYPES[candidate.suffix.lower()]),
               ("Cache-Control", "no-store"),
               ("Content-Length", str(len(payload)))]
    return 200, headers, payload
