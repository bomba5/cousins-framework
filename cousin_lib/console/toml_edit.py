"""Targeted edits of a cousin.toml text that keep every other line:
set or remove one key in one table, re-parsed before it is persisted.
The same atomic rename-into-place the rest of the framework uses."""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path


def _literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError("unsupported TOML value %r" % (value,))


def _table_span(lines, table):
    """(start, end) line indexes of `[table]`'s body, or None."""
    header = re.compile(r"^\s*\[%s\]\s*(#.*)?$" % re.escape(table))
    any_header = re.compile(r"^\s*\[")
    for i, line in enumerate(lines):
        if header.match(line):
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if any_header.match(lines[j]):
                    end = j
                    break
            return i + 1, end
    return None


def set_key(text, table, key, value):
    """Return the text with `key = value` in `[table]` (value None
    removes the key). The table is created at the end when absent."""
    lines = text.splitlines(keepends=True)
    key_re = re.compile(r"^\s*%s\s*=" % re.escape(key))
    span = _table_span(lines, table)
    new_line = None if value is None else "%s = %s\n" % (key, _literal(value))
    if span is None:
        if new_line is None:
            return text
        body = "".join(lines).rstrip("\n")
        return (body + "\n\n" if body else "") + "[%s]\n%s" % (table, new_line)
    start, end = span
    for i in range(start, end):
        if key_re.match(lines[i]):
            if new_line is None:
                del lines[i]
            else:
                lines[i] = new_line
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
                lines[insert_at - 1] += "\n"
            lines.insert(insert_at, new_line)
    return "".join(lines)


def write_key(home, table, key, value):
    """Edit <home>/cousin.toml in place: parse the result before the
    rename, so a text that cannot be read back is never persisted.
    Returns the parsed document."""
    path = Path(home) / "cousin.toml"
    new_text = set_key(path.read_text(), table, key, value)
    parsed = tomllib.loads(new_text)
    got = parsed.get(table, {}).get(key)
    if value is None and key in parsed.get(table, {}):
        raise ValueError("%s.%s still present after removal" % (table, key))
    if value is not None and got != value:
        raise ValueError("%s.%s did not round-trip" % (table, key))
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(new_text)
    os.replace(tmp, path)
    return parsed
