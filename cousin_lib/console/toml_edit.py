"""Targeted edits of a cousin.toml text that keep every other line:
set or remove keys in a table (a dotted name like `agent.sessions` is a
subtable), re-parsed before it is persisted. The same atomic
rename-into-place the rest of the framework uses, the file's mode kept.

Values: str, int, bool, a finite float, and a list of those. A key's
old value is replaced whole, a multi-line array or string included."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import tomllib
from pathlib import Path

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("unsupported TOML value %r (not finite)" % (value,))
        return repr(value)
    if isinstance(value, str):
        # json escapes what TOML forbids in a basic string but DEL
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    raise TypeError("unsupported TOML value %r" % (value,))


def _literal(value):
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(_scalar(v) for v in value)
    return _scalar(value)


def _check_names(table, key):
    parts = table.split(".") if isinstance(table, str) else [None]
    if not all(isinstance(p, str) and _BARE_KEY.match(p) for p in parts):
        raise ValueError("table %r is not a (dotted) bare TOML name" % (table,))
    if not isinstance(key, str) or not _BARE_KEY.match(key):
        raise ValueError("key %r is not a bare TOML key" % (key,))


_HEADER = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$")


def _statements(lines):
    """[(start, end, header)] for the text's statements: a line range that
    parses on its own (a table header, a key and its whole value, a
    multi-line array or string included), a blank or a comment line.
    `header` is the dotted table name for a `[table]` line (the raw line
    for an `[[array]]` header), else None. A
    line inside a multi-line value is never a statement of its own, so a
    key's name or a `[x]` inside a string is never taken for one. None
    when a line starts nothing that parses (the caller then refuses)."""
    out, i, n = [], 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            out.append((i, i + 1, None))
            i += 1
            continue
        for j in range(i + 1, n + 1):
            try:
                tomllib.loads("".join(lines[i:j]))
            except tomllib.TOMLDecodeError:
                continue
            header = None
            if j == i + 1 and stripped.startswith("[["):
                header = stripped              # an array of tables: never a [table] name
            elif j == i + 1 and stripped.startswith("["):
                m = _HEADER.match(lines[i])
                header = ".".join(part.strip() for part in m.group(1).split(".")) \
                    if m else stripped
            out.append((i, j, header))
            i = j
            break
        else:
            return None
    return out


def _table_body(statements, table):
    """(header index, [statements of its body]) of `[table]`, or None."""
    for n, (_i, _j, header) in enumerate(statements):
        if header == table:
            body = []
            for st in statements[n + 1:]:
                if st[2] is not None:
                    break
                body.append(st)
            return statements[n], body
    return None


def _inline_comment(line):
    """The whitespace and `# comment` after a one-line `key = value`, or ""."""
    text = line.rstrip("\r\n")
    try:
        whole = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ""
    for pos, ch in enumerate(text):
        if ch != "#":
            continue
        try:
            if tomllib.loads(text[:pos]) == whole:
                head = text[:pos]
                return head[len(head.rstrip()):] + text[pos:]
        except tomllib.TOMLDecodeError:
            continue
    return ""


def _rest(doc, table, key):
    """`doc` without table.key, and without an empty table left on that path."""
    import copy
    doc = copy.deepcopy(doc)
    parts = table.split(".")
    chain, node = [], doc
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return doc
        chain.append((node, part))
        node = node[part]
    if isinstance(node, dict):
        node.pop(key, None)
    for parent, part in reversed(chain):
        if parent[part] == {}:
            del parent[part]
        else:
            break
    return doc


def set_key(text, table, key, value):
    """Return the text with `key = value` in `[table]` (value None
    removes the key). The table is created at the end when absent. The
    key's old value is replaced whole and its inline comment kept; every
    other statement is kept byte for byte. When the text parses, the
    result is checked: the rest of the document must read back the same,
    else ValueError and nothing is changed."""
    _check_names(table, key)
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        before = None
    literal = None if value is None else "%s = %s" % (key, _literal(value))
    statements = _statements(lines) if before is not None else None
    if before is not None and statements is None:
        raise ValueError("cannot find the statements of this cousin.toml")
    key_re = re.compile(r"^\s*%s\s*=" % re.escape(key))
    found = _table_body(statements, table) if statements is not None else None
    if found is None:
        if literal is None:
            return text
        body = "".join(lines).rstrip("\r\n")
        out = (body + nl + nl if body else "") + "[%s]%s%s%s" % (table, nl, literal, nl)
    else:
        (hi, hj, _h), body = found
        target = next(((i, j) for i, j, _ in body if key_re.match(lines[i])), None)
        if target is not None:
            i, j = target
            if literal is None:
                lines[i:j] = []
            else:
                comment = _inline_comment(lines[i]) if j == i + 1 else ""
                lines[i:j] = [literal + comment + nl]
        elif literal is not None:
            # after the last non-blank statement of the body, so a trailing
            # blank keeps separating this table from the next
            insert_at = hj
            for i, j, _ in body:
                if lines[i].strip() and not lines[i].strip().startswith("#"):
                    insert_at = j
            if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
                lines[insert_at - 1] += nl
            lines.insert(insert_at, literal + nl)
        out = "".join(lines)
    if before is not None:
        try:
            after = tomllib.loads(out)
        except tomllib.TOMLDecodeError as err:
            raise ValueError("setting %s.%s would break the file: %s" % (table, key, err))
        if _rest(after, table, key) != _rest(before, table, key):
            raise ValueError("setting %s.%s would change more than that key" % (table, key))
    return out


def _lookup(parsed, table, key):
    node = parsed
    for part in table.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return False, None
    if not isinstance(node, dict) or key not in node:
        return False, None
    return True, node[key]


def _same(got, want):
    if isinstance(want, (list, tuple)):
        return isinstance(got, list) and len(got) == len(want) \
            and all(_same(g, w) for g, w in zip(got, want))
    return type(got) is type(want) and got == want


def _changes(changes):
    items = changes.items() if isinstance(changes, dict) else changes
    out = []
    for item in items:
        if isinstance(changes, dict):
            (table, key), value = item
        else:
            table, key, value = item
        out.append((table, key, value))
    return out


def write_keys(home, changes, *, validate=None):
    """Edit <home>/cousin.toml in place: every change applied to the
    text, the result parsed, each value read back as written, then
    `validate(parsed)` (raise to refuse), and only then the atomic
    rename. `changes` is a list of (table, key, value) or a mapping of
    (table, key) to value; value None removes the key. Nothing is
    written when any step fails. Returns the parsed document."""
    path = Path(home) / "cousin.toml"
    text = path.read_text()
    items = _changes(changes)
    for table, key, value in items:
        text = set_key(text, table, key, value)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ValueError("the edited cousin.toml does not parse: %s" % err)
    for table, key, value in items:
        present, got = _lookup(parsed, table, key)
        if value is None and present:
            raise ValueError("%s.%s still present after removal" % (table, key))
        if value is not None and (not present or not _same(got, list(value)
                                                          if isinstance(value, tuple) else value)):
            raise ValueError("%s.%s did not round-trip" % (table, key))
    if validate is not None:
        validate(parsed)
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cousin.", suffix=".toml.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return parsed


def write_key(home, table, key, value):
    """write_keys of one change."""
    return write_keys(home, [(table, key, value)])
