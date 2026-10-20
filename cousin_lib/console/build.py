"""The console's release-time build: JSX compiled ahead of time, and the
libraries the page uses kept beside it (docs/development.md, "The
console build").

    python -m cousin_lib.console.build            compile every .jsx
    python -m cousin_lib.console.build check      the compiled copy is current
    python -m cousin_lib.console.build verify     a fresh compile matches it
    python -m cousin_lib.console.build vendor     fetch the pinned libraries

`build` and `verify` need Node (they run a pinned esbuild through npx);
`check` and the console itself need nothing but Python. Each `.jsx`
the page names compiles to `compiled/<name>.js` on its own, so the
files keep the load order and the shared global scope they have under
Babel. `compiled/manifest.json` records each source's and output's
content stamp; static.py serves the compiled files only while every
stamp still matches, and otherwise falls back to compiling in the
browser, so an edited `.jsx` works on the next reload before anyone
rebuilds.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "console_static"
COMPILED = "compiled"
VENDOR = "vendor"
MANIFEST = "manifest.json"

ESBUILD = "esbuild@0.25.10"
# What the browsers we serve all run (a phone's WebView included); the
# JSX is the only thing compiled away.
TARGET = "es2020"

# The libraries the page loads from vendor/, pinned, with the SRI hash
# of each file. `vendor` fetches them and refuses a file whose hash
# differs.
LIBS = [
    ("react.production.min.js",
     "https://unpkg.com/react@18.3.1/umd/react.production.min.js",
     "sha384-DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z"),
    ("react-dom.production.min.js",
     "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js",
     "sha384-gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1"),
    ("marked.min.js",
     "https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js",
     "sha384-948ahk4ZmxYVYOc+rxN1H2gM1EJ2Duhp7uHtZ4WSLkV4Vtx5MUqnV+l7u9B+jFv+"),
    ("xterm.min.css",
     "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css",
     "sha384-9ftsg11+LSxVUaknegCfeKvlkO9EdIPI2op725RqY87IvhyGjElmpjZlP3LhTQjn"),
    ("xterm.min.js",
     "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js",
     "sha384-xjfWUeCWdMtvpAb/SmM6lMzS6pQGcQa0loOl1d97j6Odw0vjK9nW3+dTb/bn/mwH"),
    ("xterm-addon-fit.min.js",
     "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js",
     "sha384-dpjGwSSISUTz2taP54Bor7qkyMR20sSO9oe11UVYnGs2/YdUBf7HW30XKQx9PCzn"),
]

# Loaded from the CDN on the first diagram, pinned with its SRI hash in
# index.html, and not kept in vendor/: its minified identifiers trip the
# publish gate's name rules.
CDN_LIBS = [
    "https://cdn.jsdelivr.net/npm/mermaid@10.9.8/dist/mermaid.min.js",
]

_JSX_RE = re.compile(r'<script type="text/babel" src="([^"?#]+\.jsx)"></script>')


def stamp(data):
    """The content stamp static.py puts in `?v=`: 12 hex of sha256."""
    return hashlib.sha256(data).hexdigest()[:12]


def page_sources(root):
    """The `.jsx` files index.html loads, in page order."""
    return _JSX_RE.findall((root / "index.html").read_text())


def compiled_name(source):
    return "%s/%s.js" % (COMPILED, source[:-len(".jsx")])


def read_manifest(root):
    try:
        data = json.loads((root / COMPILED / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _file_stamp(path):
    return stamp(path.read_bytes())


def stale(root, file_stamp=_file_stamp):
    """Why the compiled copy cannot stand in for the sources, or "" when
    it can: every source the page names has an output whose recorded
    stamps match both files as they are now. `file_stamp(path)` may be
    a cached version of the stamp (static.py's)."""
    root = Path(root)
    manifest = read_manifest(root)
    if manifest is None:
        return "no %s/%s" % (COMPILED, MANIFEST)
    files = manifest.get("files") or {}
    for source in page_sources(root):
        entry = files.get(source)
        if not isinstance(entry, dict):
            return "%s is not compiled" % source
        try:
            src = file_stamp(root / source)
            out = file_stamp(root / compiled_name(source))
        except OSError as err:
            return "%s: %s" % (source, err)
        if entry.get("source") != src:
            return "%s changed since the last build" % source
        if entry.get("output") != out:
            return "%s does not match the manifest" % compiled_name(source)
    return ""


def _esbuild(sources, src_dir, out_dir):
    npx = shutil.which("npx")
    if not npx:
        raise SystemExit("build: npx not found; the build needs Node")
    cmd = [npx, "--yes", ESBUILD, *sources,
           "--outdir=" + str(out_dir), "--loader:.jsx=jsx",
           "--target=" + TARGET, "--charset=utf8", "--log-level=warning"]
    subprocess.run(cmd, cwd=src_dir, check=True)


def build(root=STATIC_DIR, out=None):
    """Compile the page's sources into `out` (default root/compiled)
    and write its manifest. Returns the manifest."""
    root = Path(root)
    out = Path(out) if out is not None else root / COMPILED
    sources = page_sources(root)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.js"):
        old.unlink()
    _esbuild(sources, root, out)
    files = {}
    for source in sources:
        files[source] = {
            "source": stamp((root / source).read_bytes()),
            "output": stamp((out / (source[:-4] + ".js")).read_bytes()),
        }
    manifest = {"esbuild": ESBUILD, "target": TARGET, "files": files}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1,
                                           sort_keys=True) + "\n")
    return manifest


def verify(root=STATIC_DIR):
    """A fresh compile into a temporary directory is byte for byte what
    is committed. Returns a list of the files that differ."""
    root = Path(root)
    with tempfile.TemporaryDirectory() as tmp:
        build(root, out=Path(tmp))
        differ = []
        for fresh in sorted(Path(tmp).iterdir()):
            committed = root / COMPILED / fresh.name
            if not committed.is_file() \
                    or committed.read_bytes() != fresh.read_bytes():
                differ.append(fresh.name)
        return differ


def sri(data):
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()


def vendor(root=STATIC_DIR):
    """Fetch each pinned library into root/vendor, refusing a file whose
    SRI hash is not the pinned one."""
    target = Path(root) / VENDOR
    target.mkdir(parents=True, exist_ok=True)
    for name, url, want in LIBS:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        got = sri(data)
        if got != want:
            raise SystemExit("vendor: %s hashes %s, pinned %s" % (url, got, want))
        (target / name).write_bytes(data)
        print("vendor: %s (%d bytes)" % (name, len(data)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m cousin_lib.console.build",
                                     description="the console's build")
    parser.add_argument("command", nargs="?", default="build",
                        choices=("build", "check", "verify", "vendor"))
    args = parser.parse_args(argv)
    if args.command == "build":
        manifest = build()
        print("build: %d files into %s/" % (len(manifest["files"]), COMPILED))
    elif args.command == "check":
        why = stale(STATIC_DIR)
        if why:
            print("check: stale: %s; run python -m cousin_lib.console.build"
                  % why, file=sys.stderr)
            return 1
        print("check: compiled copy is current")
    elif args.command == "verify":
        differ = verify()
        if differ:
            print("verify: a fresh compile differs: %s" % ", ".join(differ),
                  file=sys.stderr)
            return 1
        print("verify: a fresh compile matches")
    else:
        vendor()
    return 0


if __name__ == "__main__":
    sys.exit(main())
