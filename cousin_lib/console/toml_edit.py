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
_ANY_HEADER = re.compile(r"^\s*\[")


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
        return json.dumps(value, ensure_ascii=False)
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


def _table_span(lines, table):
    """(start, end) line indexes of `[table]`'s body, or None."""
    header = re.compile(r"^\s*\[%s\]\s*(#.*)?$" % re.escape(table))
    for i, line in enumerate(lines):
        if header.match(line):
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if _ANY_HEADER.match(lines[j]):
                    end = j
                    break
            return i + 1, end
    return None


def _value_end(lines, i, end):
    """The index after the last line of the `key = value` starting at
    line i: the fewest lines that parse on their own (a multi-line array
    or string spans several). One line when nothing parses, as before."""
    for j in range(i + 1, end + 1):
        try:
            tomllib.loads("".join(lines[i:j]))
            return j
        except tomllib.TOMLDecodeError:
            continue
    return i + 1


def set_key(text, table, key, value):
    """Return the text with `key = value` in `[table]` (value None
    removes the key). The table is created at the end when absent."""
    _check_names(table, key)
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    key_re = re.compile(r"^\s*%s\s*=" % re.escape(key))
    span = _table_span(lines, table)
    new_line = None if value is None else "%s = %s%s" % (key, _literal(value), nl)
    if span is None:
        if new_line is None:
            return text
        body = "".join(lines).rstrip("\r\n")
        return (body + nl + nl if body else "") + "[%s]%s%s" % (table, nl, new_line)
    start, end = span
    for i in range(start, end):
        if key_re.match(lines[i]):
            stop = _value_end(lines, i, end)
            lines[i:stop] = [] if new_line is None else [new_line]
            break
    else:
        if new_line is not None:
            # Insert after the last non-blank body line so a trailing
            # blank keeps separating this table from the next.
            insert_at = start
            for i in range(start, end):
                if lines[i].strip():
                    insert_at = i + 1
            if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
                lines[insert_at - 1] += nl
            lines.insert(insert_at, new_line)
    return "".join(lines)


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
