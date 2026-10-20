"""Static files for the console: the committed frontend under
`cousin_lib/console_static/` (docs/reference/console-api.md, "Static files").

`serve_static(path) -> (status, headers, bytes)` is the whole surface;
the server calls it for every non-`/api/` GET. Suffix allowlist, a
resolve-then-contain traversal check (403 outside the root, 404 for
anything else that is not a served file), and `index.html` served for
`/` with a `?v=<hash>` stamp (the referenced file's content hash) on
each local `src`/`href`. While `compiled/manifest.json` matches the
sources (build.py), the page's app block loads `compiled/<name>.js`
in place of each `.jsx` and drops Babel; otherwise it is served as
written and the browser compiles the JSX. The page itself is `Cache-Control: no-store`,
so a redeploy is seen on the next load; a file requested with its
current stamp is cached for good (a changed file gets a new stamp, so a
new URL), and anything else is `no-store`. Text bodies are gzipped
when the browser accepts it.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
import threading
import urllib.parse
from pathlib import Path

from cousin_lib.console import build

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

# Worth compressing: the text types. A body under GZIP_MIN goes as is.
GZIP_TYPES = {".html", ".jsx", ".js", ".css", ".json", ".webmanifest",
              ".svg"}
GZIP_MIN = 1024

NO_STORE = "no-store"
IMMUTABLE = "public, max-age=31536000, immutable"

# path -> (mtime_ns, size, stamp, gzipped body or None): a file is
# hashed and compressed once per version, not once per request.
_FILE_CACHE = {}
_FILE_CACHE_LOCK = threading.Lock()

_APP_BLOCK_RE = re.compile(r"(<!-- app:start.*?-->)(.*?)(<!-- app:end -->)",
                           re.S)
_BABEL_TAG_RE = re.compile(
    r'<script src="[^"]*/@babel/standalone@[^"]*"[^>]*></script>\n?')
_JSX_TAG_RE = re.compile(
    r'<script type="text/babel" src="([^"?#]+)\.jsx"></script>')
# The last reason the compiled copy was passed over, said once.
_last_stale = [None]

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


def _entry(path):
    """(stamp, payload, gzipped payload or None) for a file, from the
    cache while its mtime and size hold. The stamp is the first 12 hex
    digits of the content's sha256."""
    st = path.stat()
    key = str(path)
    with _FILE_CACHE_LOCK:
        hit = _FILE_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2], hit[3], hit[4]
    payload = path.read_bytes()
    stamp = hashlib.sha256(payload).hexdigest()[:12]
    packed = None
    if path.suffix.lower() in GZIP_TYPES and len(payload) >= GZIP_MIN:
        packed = gzip.compress(payload, compresslevel=6, mtime=0)
    with _FILE_CACHE_LOCK:
        _FILE_CACHE[key] = (st.st_mtime_ns, st.st_size, stamp, payload,
                            packed)
    return stamp, payload, packed


def _stamp(path):
    """The content stamp of a servable file, or None."""
    try:
        if path.is_file() and path.suffix.lower() in STATIC_TYPES:
            return _entry(path)[0]
    except OSError:
        pass
    return None


def _stamp_index(html, root):
    """Append ?v=<hash> to every local src/href so a redeploy is seen
    at once and an unchanged file is never fetched twice. Remote URLs,
    fragments and data: URIs are left alone; the stamp is the
    referenced file's content hash when it is a servable file, else the
    page's own."""
    index_stamp = _stamp(root / "index.html") or "0"

    def repl(m):
        attr, ref = m.group(1), m.group(2)
        if ref.startswith(_REMOTE_PREFIXES):
            return m.group(0)
        stamp = _stamp(root / ref.lstrip("/")) or index_stamp
        return '%s="%s?v=%s"' % (attr, ref, stamp)

    return _REF_RE.sub(repl, html)


def _cached_stamp(path):
    stamp = _stamp(path)
    if stamp is None:
        raise OSError("%s is not a servable file" % path.name)
    return stamp


def _app_block(html, root):
    """The page with its app block on the compiled files when they are
    current, else as written (the browser compiles the JSX)."""
    m = _APP_BLOCK_RE.search(html)
    if not m:
        return html
    why = build.stale(root, file_stamp=_cached_stamp)
    if why:
        if why != _last_stale[0]:
            _last_stale[0] = why
            print("cousin-console: serving the JSX uncompiled: %s;"
                  " run python -m cousin_lib.console.build" % why,
                  file=sys.stderr)
        return html
    _last_stale[0] = None
    body = _BABEL_TAG_RE.sub("", m.group(2))
    body = _JSX_TAG_RE.sub(
        lambda t: '<script src="%s/%s.js"></script>'
        % (build.COMPILED, t.group(1)), body)
    return html[:m.start(2)] + body + html[m.end(2):]


def accepts_gzip(accept_encoding):
    """True when an Accept-Encoding header allows gzip (q=0 refuses)."""
    for part in (accept_encoding or "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() not in ("gzip", "*"):
            continue
        q = params.strip().lower()
        if q.startswith("q=") and q[2:].strip() in ("0", "0.0", "0.00",
                                                    "0.000"):
            return False
        return True
    return False


def serve_static(path, *, root=None, accept_encoding=""):
    """(status, headers, body) for a request path, its query included
    (the `v` stamp decides the cache header). `root` defaults to the
    committed static directory; tests point it elsewhere.
    `accept_encoding` is the request's Accept-Encoding header."""
    root = Path(root) if root is not None else STATIC_DIR
    split = urllib.parse.urlsplit(path)
    name = urllib.parse.unquote(split.path)
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
        stamp, payload, packed = _entry(candidate)
    except OSError:
        return _error(404, "not found")
    suffix = candidate.suffix.lower()
    cache = NO_STORE
    if candidate.name == "index.html" and candidate.parent == base:
        html = _app_block(payload.decode("utf-8", "replace"), base)
        payload = _stamp_index(html, base).encode()
        packed = gzip.compress(payload, compresslevel=6, mtime=0) \
            if len(payload) >= GZIP_MIN else None
    elif urllib.parse.parse_qs(split.query).get("v") == [stamp]:
        # The URL names this exact content, so it can never go stale.
        cache = IMMUTABLE
    headers = [("Content-Type", STATIC_TYPES[suffix]),
               ("Cache-Control", cache)]
    if suffix in GZIP_TYPES:
        headers.append(("Vary", "Accept-Encoding"))
        if packed is not None and accepts_gzip(accept_encoding):
            payload = packed
            headers.append(("Content-Encoding", "gzip"))
    headers.append(("Content-Length", str(len(payload))))
    return 200, headers, payload
