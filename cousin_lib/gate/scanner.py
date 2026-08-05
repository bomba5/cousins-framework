"""Contamination scanner: one engine behind the gate and the triage.

The denylist of real terms never lives in this repository; it is loaded
from a path outside the tree. See docs/gate.md.
"""
import ast
import io
import json
import os
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

# Binaries the gate cannot read are failed as opaque carriers rather than
# silently passed; only these extensions are presumed-safe image/font assets.
_BINARY_ALLOWLIST = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"}

_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


# Position -> disposition. Structural positions mean the term participates
# in behavior; mechanical positions mean prose a rename clears. Binary and
# unclassified code positions are structural by default: unknown is unsafe.
_MECHANICAL_POSITIONS = {"comment", "docstring", "prose"}


@dataclass
class GateResult:
    passed: bool
    hits: list


def run_gate(root, name_terms=None, denylist_path=None):
    terms = list(name_terms or [])
    if denylist_path is not None:
        terms += load_denylist(denylist_path)
    hits = Scanner(name_terms=terms).scan_tree(root)
    return GateResult(passed=not hits, hits=hits)


def triage_verdicts(hits):
    verdicts = {}
    for h in hits:
        disp = "mechanical" if h.position in _MECHANICAL_POSITIONS else "structural"
        if verdicts.get(h.file) != "structural":
            verdicts[h.file] = disp
    return verdicts


def manifest_lines(hits):
    records = sorted(
        (
            {
                "file": h.file,
                "line": h.line,
                "col": h.col,
                "kind": h.kind,
                "position": h.position,
                "term": h.term,
                "context": h.context,
            }
            for h in hits
        ),
        key=lambda r: (r["file"], r["line"], r["col"], r["term"]),
    )
    return [json.dumps(r, sort_keys=True) for r in records]


class DenylistLocationError(Exception):
    """The denylist sits somewhere version-controlled or declaratively
    managed. Placement is a property, not a path: a list that gets adopted
    into a repo by housekeeping publishes exactly what it exists to hide."""


def load_denylist(path, store_prefixes=("/nix/store",)):
    resolved = Path(path).resolve()
    for prefix in store_prefixes:
        if str(resolved).startswith(str(Path(prefix).resolve())):
            raise DenylistLocationError(
                "denylist resolves into a managed store: %s" % resolved
            )
    for ancestor in resolved.parents:
        if (ancestor / ".git").exists():
            raise DenylistLocationError(
                "denylist sits inside a git work tree: %s" % resolved
            )
    terms = []
    for line in resolved.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


@dataclass
class Hit:
    term: str
    file: str = ""
    line: int = 0
    col: int = 0
    context: str = ""
    position: str = "prose"
    kind: str = "name"


# RFC1918 plus the CGNAT /10 block: a private address literal in a public
# tree is topology disclosure regardless of which name scheme it belongs to.
_ADDRESS_RE = re.compile(
    r"\b(?:"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r")\b"
)

# An absolute /home/<user> path names an account on a real machine. Generic
# code resolves homes at runtime; only personalised code spells one out.
_HOME_PATH_RE = re.compile(r"/home/[A-Za-z0-9._-]+")

# JWT structure: base64url header (JSON objects encode to "eyJ..."), then
# payload and signature segments. Shape-based, so rotation state is moot.
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\b"
)


def _python_spans(source):
    """(start, end, kind) spans for comments and strings; docstrings are
    told apart from executable strings via the ast, because that distinction
    is the whole point of position-based triage."""
    doc_ranges = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        scopes = [tree] + [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        for scope in scopes:
            body = getattr(scope, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                c = body[0].value
                doc_ranges.append((c.lineno, c.end_lineno))
    spans = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                spans.append((tok.start, tok.end, "comment"))
            elif tok.type == tokenize.STRING:
                kind = (
                    "docstring"
                    if any(a <= tok.start[0] <= b for a, b in doc_ranges)
                    else "code-string"
                )
                spans.append((tok.start, tok.end, kind))
    except tokenize.TokenError:
        pass
    return spans


def _position_at(spans, row, col0):
    for (sr, sc), (er, ec), kind in spans:
        if row < sr or row > er:
            continue
        if row == sr and col0 < sc:
            continue
        if row == er and col0 >= ec:
            continue
        return kind
    return "code"


def _language_for(filename, text):
    if filename.endswith(".py"):
        return "python"
    if filename.endswith((".sh", ".bash")):
        return "bash"
    if filename.endswith((".md", ".markdown")):
        return "markdown"
    first = text.split("\n", 1)[0] if text else ""
    if first.startswith("#!"):
        if "python" in first:
            return "python"
        if re.search(r"\b(?:ba|z|da)?sh\b", first):
            return "bash"
    return "unknown"


def _bash_position(lineno, line):
    # Full-line comments are the only mechanical position in bash. The
    # shebang is code: an interpreter path is behaviour. Trailing comments
    # are left as code on purpose - quote-aware parsing is not worth the
    # risk of calling a behaviour change mechanical.
    if lineno == 1 and line.startswith("#!"):
        return "code"
    if line.lstrip().startswith("#"):
        return "comment"
    return "code"


class Scanner:
    def __init__(self, name_terms=None):
        self.name_terms = list(name_terms or [])
        self._name_res = [
            re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE)
            for t in self.name_terms
        ]

    def scan_text(self, text, filename):
        lang = _language_for(filename, text)
        spans = _python_spans(text) if lang == "python" else None
        hits = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            matches = []
            for term, rx in zip(self.name_terms, self._name_res):
                for m in rx.finditer(line):
                    matches.append((term, m, "name"))
            for m in _ADDRESS_RE.finditer(line):
                matches.append((m.group(0), m, "address"))
            for m in _HOME_PATH_RE.finditer(line):
                matches.append((m.group(0), m, "home-path"))
            for m in _JWT_RE.finditer(line):
                matches.append((m.group(0), m, "secret"))
            for term, m, kind in matches:
                if lang == "python":
                    position = _position_at(spans, lineno, m.start())
                elif lang == "bash":
                    position = _bash_position(lineno, line)
                elif lang == "markdown":
                    position = "prose"
                else:
                    position = "unclassified"
                hits.append(
                    Hit(
                        term=term,
                        file=filename,
                        line=lineno,
                        col=m.start() + 1,
                        context=line.strip(),
                        position=position,
                        kind=kind,
                    )
                )
        return hits

    def scan_tree(self, root):
        root = Path(root)
        hits = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                raw = path.read_bytes()
                if b"\x00" in raw[:8192]:
                    if path.suffix.lower() not in _BINARY_ALLOWLIST:
                        hits.append(
                            Hit(term=name, file=rel, kind="binary", position="binary")
                        )
                    continue
                hits.extend(self.scan_text(raw.decode("utf-8", "replace"), rel))
        return hits
