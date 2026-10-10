"""Keep a cousin's CLAUDE.md in step with the framework's template
(docs/cousins.md, "CLAUDE.md and the template").

A CLAUDE.md is the template rendered once, at spawn, plus the cousin's own
sections below the marker line. Without a sync a template change never
reaches a cousin spawned before it. The sync rebuilds the framework part
section by section, in template order:

- Identity and Voice are the cousin's own (the template renders them from
  per-cousin text that is not stored anywhere else): kept as they are.
- Every other framework section gets the current template text, rendered
  with the cousin's name, slug, port and role from cousin.toml.
- A section above the marker that the template does not know is kept, at
  the end of the framework part, and reported.
- Below the marker nothing changes, except a leftover copy of a framework
  section that is word for word the template's (older spawns carried some
  there): it is removed so the text is not there twice. A same-titled
  section with different text is the cousin's own and stays.

It does not run by itself: `cousin-spawn <slug> --sync-template` shows
the diff and `--apply` writes it, and `cousin-upgrade --apply-homes`
brings every home's registry to a release. Every write keeps the old file in
data/claude-md-backups/, like the console's CLAUDE.md editor.
The MCP registry gets the same treatment, additively and at every level:
a table the shipped registry has and the cousin's lacks is appended whole,
and a key inside a table they share is added to it. A value the cousin
already has is never changed, so an edited description or argv survives -
except a value still holding exactly what a past release shipped
(_MIGRATIONS below): that one gets the current one, because it is the
framework's own text, not the cousin's. Given the text the cousin was last
synced from (`base`), a table or key that text had and the shipped one no
longer has is reported as retired, and removed only when asked (`prune`,
cousin-upgrade --prune-retired).

`plan` and `_registry_sync` take the template and the shipped registry as
text too, so cousin-upgrade can plan against a release before the
checkout moves to it.
"""
import difflib
import json
import re
import time
import tomllib
from pathlib import Path

from cousin_lib.config import FrameworkConfig

MARKER = "## Append your cousin-specific sections below this line"
OWNED = ("{{ROLE_PARAGRAPH}}", "{{VOICE_GUIDE}}")
_HEAD = re.compile(r"^## ", re.M)


class SyncError(ValueError):
    """The file cannot be synced as it stands; the message says why."""


def _sections(text):
    """(preamble, [(title_line, body)]) for markdown split on '## '."""
    starts = [m.start() for m in _HEAD.finditer(text)]
    if not starts:
        return text, []
    pre = text[:starts[0]]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[s:e]
        title, _, body = chunk.partition("\n")
        out.append((title.strip(), body))
    return pre, out


def _join(pre, sections):
    return pre + "".join("%s\n%s" % (t, b) for t, b in sections)


def _norm(text):
    return " ".join(text.split())


def _template_text(root):
    for path in (Path(root) / "templates" / "cousin-CLAUDE.template.md",
                 Path(__file__).resolve().parents[1] / "templates"
                 / "cousin-CLAUDE.template.md"):
        if path.is_file():
            return path.read_text()
    raise SyncError("no CLAUDE.md template under %s/templates" % root)


def _values(home):
    data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    cousin, chat = data.get("cousin", {}), data.get("chat", {})
    slug = cousin.get("slug") or Path(home).name
    return {"NAME": cousin.get("name") or slug.capitalize(), "SLUG": slug,
            "PORT": chat.get("port", ""),
            "ROLE_ONE_LINE": cousin.get("role") or ""}


def _render(text, values):
    for key, value in values.items():
        text = text.replace("{{%s}}" % key, str(value))
    return text


# Sections a past template carried and the current one does not: the
# lane's mechanics, now only in the generated contract (meeting 11 D,
# 3.49.0). Above the marker they are reported, and removed with `prune`
# (cousin-spawn --sync-template --apply --prune-retired) only while the
# cousin's copy still reads as the last template rendered it
# (templates/retired-CLAUDE-sections.md): one with the cousin's own lines
# under a kept title stays, for a person to review (#304). A section with
# another title stays the cousin's own.
RETIRED_SECTIONS = (
    "Chat handling - IN-CHARACTER vs OUT-OF-CHARACTER",
    "Memory",
    "Tools: MCP first, CLIs as the fallback",
    "Session bookends",
    "Meetings",
)


def _retired_text(root):
    """The retired sections as the last template carried them; "" when
    the file is missing, so nothing is pruned without a rendering."""
    for path in (Path(root) / "templates" / "retired-CLAUDE-sections.md",
                 Path(__file__).resolve().parents[1] / "templates"
                 / "retired-CLAUDE-sections.md"):
        if path.is_file():
            return path.read_text()
    return ""


def plan(home, root=None, *, template=None, prune=False, retired=None):
    """(old_text, new_text, notes) for one cousin; new == old when it is
    in step. `template` is the template's text (default: the install's,
    _template_text). A retired framework section is removed only with
    `prune`, and only while it matches `retired` (the retired sections'
    last rendering; default: the install's, _retired_text). Raises
    SyncError when the file cannot be synced safely."""
    home = Path(home)
    root = Path(root) if root else FrameworkConfig.root_from_home(home)
    old = (home / "CLAUDE.md").read_text()
    if MARKER not in old:
        raise SyncError("no '%s' line: cannot tell the framework part"
                        " from the cousin's own" % MARKER)
    tpl = _template_text(root) if template is None else template
    if MARKER not in tpl:
        raise SyncError("the template has no marker line")
    tpl_top = tpl[:tpl.index(MARKER)]
    _tpl_pre, tpl_secs = _sections(tpl_top)
    values = _values(home)
    last = {t: _norm(_render(b, values))
            for t, b in _sections(_retired_text(root) if retired is None else retired)[1]}
    top, rest = old[:old.index(MARKER)], old[old.index(MARKER):]
    pre, secs = _sections(top)
    have = {t: b for t, b in secs}
    notes = []
    new_secs = []
    rendered = {}
    for title, body in tpl_secs:
        if any(p in body for p in OWNED):
            if title in have:
                new_secs.append((title, have[title]))
            else:
                notes.append("%s: missing, and it is the cousin's own"
                             " text; not recreated" % title)
            continue
        text = _render(body, values)
        rendered[title] = text
        new_secs.append((title, text))
    known = {t for t, _ in tpl_secs}
    for title, body in secs:
        if title not in known:
            if title.lstrip("#").strip() in RETIRED_SECTIONS:
                if prune and last.get(title) == _norm(body):
                    notes.append("%s: retired from the template; removed" % title)
                    continue
                new_secs.append((title, body))
                if prune:
                    notes.append("%s: retired from the template, but it differs from the"
                                 " template's last version (the cousin's own lines?);"
                                 " kept, review it by hand" % title)
                    continue
                notes.append("%s: retired from the template (the contract carries it);"
                             " kept until --prune-retired" % title)
                continue
            new_secs.append((title, body))
            notes.append("%s: not in the template; kept" % title)
    marker_line, _, below = rest.partition("\n")
    bpre, bsecs = _sections(below)
    kept = []
    for title, body in bsecs:
        if title in rendered and _norm(body) == _norm(rendered[title]):
            notes.append("%s: a copy below the marker removed (the"
                         " framework part has it)" % title)
            continue
        kept.append((title, body))
    if new_secs:
        last_t, last_b = new_secs[-1]
        if not last_b.endswith("\n\n"):
            new_secs[-1] = (last_t, last_b.rstrip("\n") + "\n\n")
    new = _join(pre, new_secs) + marker_line + "\n" + _join(bpre, kept)
    return old, new, notes


def diff(home, root=None, *, template=None, prune=False):
    old, new, notes = plan(home, root, template=template, prune=prune)
    text = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="CLAUDE.md", tofile="CLAUDE.md (synced)"))
    return text, notes


def _backup(home, text):
    folder = Path(home) / "data" / "claude-md-backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("CLAUDE-%d.md" % int(time.time()))
    path.write_text(text)
    return path


_TABLE = re.compile(r"^\[([^\[\]]+)\]\s*$")
_KEY = re.compile(r'^\s*([A-Za-z0-9_-]+|"[^"]*")\s*=')
_STR = re.compile(r'"(?:[^"\\]|\\.)*"' + r"|'[^']*'")


def _depth(line):
    """Bracket balance of a line, strings ignored. Strings go before the
    comment is cut: a `#` inside a string (a description naming
    `raw:<file>#<line>`) is not a comment, and cutting there left an
    unclosed brace that swallowed every key after it."""
    bare = _STR.sub("", line).split("#")[0]
    return (bare.count("[") - bare.count("]")
            + bare.count("{") - bare.count("}"))


def _blocks(text):
    """[(table path or None, body lines)] in file order; the first entry
    with a None path is the preamble. The '[path]' line is not in the body."""
    out, path, cur = [], None, []
    for line in text.splitlines(keepends=True):
        m = _TABLE.match(line)
        if m:
            out.append((path, cur))
            path, cur = m.group(1).strip(), []
        else:
            cur.append(line)
    out.append((path, cur))
    return out


def _entries(lines):
    """[(key or None, lines)] for a table body. A key's value may span
    lines (an array or an inline table); a None key is a blank or comment."""
    out, key, cur, depth = [], None, [], 0
    for line in lines:
        if key is None:
            m = _KEY.match(line)
            if not m:
                out.append((None, [line]))
                continue
            key, cur, depth = m.group(1).strip('"'), [line], _depth(line)
        else:
            cur.append(line)
            depth += _depth(line)
        if depth <= 0:
            out.append((key, cur))
            key, cur, depth = None, [], 0
    if key is not None:
        out.append((key, cur))
    return out


def _family(path):
    """The tool a table belongs to: the first two dotted parts of its path,
    so tools.memory.commands.obsolete belongs with tools.memory."""
    return ".".join(path.split(".")[:2])


def _place(blocks, path):
    """Where a new table goes: after the last block of its own tool, so the
    file keeps one tool's tables together; at the end for a new tool."""
    family = _family(path)
    at = len(blocks)
    for i, (p, _) in enumerate(blocks):
        if p is not None and (p == family or p.startswith(family + ".")):
            at = i + 1
    if at == len(blocks) and blocks:
        last, body = blocks[-1]
        if body and not body[-1].endswith("\n"):
            blocks[-1] = (last, body[:-1] + [body[-1] + "\n"])
    return at


# Framework-owned values the sync may migrate in a cousin's existing
# registry: (table path, key) -> ((old, new), ...), each `old` a value an
# earlier release shipped there. It is safe to replace because only a
# framework release could have put that exact value there: a cousin's own
# edit is never one of these exact values, so anything that is not this
# precise old value (including the cousin's own rewrite of it) stays
# untouched. `key` is a table's own key ("description" for its plain
# `description = "..."` line, "options" or "argv" whole), or
# "<key>.<field>" for a string field of an inline table
# ("kind.description": the `description` inside `kind = { ... }`). `new`
# is None: whatever the shipped registry holds now is written. The old
# values come from git history (registry_history), never kept by hand:
# a hand-kept list is how three releases of `options` never reached a
# home.
from cousin_lib.registry_history import SHIPPED_BEFORE

_MIGRATIONS = {key: tuple((old, None) for old in olds) for key, olds in SHIPPED_BEFORE.items()}


def _entry_value(key, lines):
    """The parsed value of one entry (`key = ...`), or None when its
    lines do not parse on their own."""
    try:
        return tomllib.loads("".join(lines)).get(key)
    except tomllib.TOMLDecodeError:
        return None


def _field(name):
    return re.compile(r"(\b%s\s*=\s*)(%s)" % (re.escape(name), _STR.pattern))


def _migrate_entry(path, key, lines, shipped_lines):
    """(lines, changed): `lines` migrated to the shipped value when the
    cousin's current value is one _MIGRATIONS records as shipped earlier
    for (path, key) or (path, "<key>.<field>"); otherwise unchanged."""
    for (table, spec), pairs in _MIGRATIONS.items():
        if table != path or spec.split(".")[0] != key:
            continue
        field = spec.partition(".")[2]
        mine, theirs = (_entry_value(key, lines),
                        _entry_value(key, shipped_lines))
        if field:
            mine = mine.get(field) if isinstance(mine, dict) else None
            theirs = theirs.get(field) if isinstance(theirs, dict) else None
        for old, new in pairs:
            if mine != old or theirs is None or theirs == old:
                continue
            if new is not None and theirs != new:
                continue
            if not field:
                text = "".join(shipped_lines)
                return [text if text.endswith("\n") else text + "\n"], True
            if not isinstance(theirs, str):
                continue
            text = _field(field).sub(
                lambda m: m.group(1) + json.dumps(theirs), "".join(lines),
                count=1)
            return [text], True
    return lines, False


def _retired(base, theirs, mine):
    """Dotted paths of the tables and keys `base` had, `theirs` no longer
    has and `mine` still has: what a release retired. A table counts
    once, not each of its keys or the tables below it."""
    def shape(text):
        out = {}
        for path, body in _blocks(text):
            if path is not None:
                out[path] = {k for k, _ in _entries(body) if k}
        return out
    base, theirs, mine = shape(base), shape(theirs), shape(mine)
    out = []
    for path, keys in base.items():
        if path not in mine or any(path.startswith(t + ".") for t in out):
            continue
        if path not in theirs:
            out.append(path)
            continue
        out.extend("%s.%s" % (path, k) for k in sorted(keys)
                   if k not in theirs[path] and k in mine[path])
    return out


def _prune(blocks, retired):
    """`blocks` without the retired tables (each with the tables below
    it) and keys; returns the dotted paths it removed."""
    paths = {p for p, _ in blocks if p is not None}
    tables = [r for r in retired if r in paths]
    keys = {}
    for r in retired:
        if r not in paths:
            table, _, key = r.rpartition(".")
            keys.setdefault(table, set()).add(key)
    removed = []
    for i in range(len(blocks) - 1, -1, -1):
        path, body = blocks[i]
        if path is None:
            continue
        if any(path == t or path.startswith(t + ".") for t in tables):
            del blocks[i]
            continue
        if path in keys:
            entries = _entries(body)
            gone = [k for k, _ in entries if k in keys[path]]
            if gone:
                blocks[i] = (path, [one for k, ls in entries
                                    if k not in keys[path] for one in ls])
                removed.extend("%s.%s" % (path, k) for k in gone)
    removed.extend(tables)
    return sorted(removed)


def _registry_sync(home, root, *, apply=False, theirs=None, base=None,
                   prune=False):
    """Bring the cousin's mcp-registry.toml up to the shipped one,
    additively at every level: a table it lacks is appended whole, and a
    key the shipped table has and its table lacks is added to that
    table. A value the cousin already has is never touched, except one
    still holding a value the framework shipped earlier (_MIGRATIONS):
    that one is corrected to the shipped value. `theirs` is the shipped
    registry's text (default: the install's, shipped_default_registry);
    `base`, when given, is the text the cousin was last synced from, and
    what it had that `theirs` lacks is reported under "retired", and
    removed only with `prune`. Returns {"path", "added", "corrected",
    "retired", "pruned"}, lists of dotted paths; "path" is None for a
    home with no registry."""
    reg = Path(home) / "mcp-registry.toml"
    if not reg.is_file():
        return {"path": None, "added": [], "corrected": [], "retired": [],
                "pruned": []}
    if theirs is None:
        from cousin_lib.mcp_server import shipped_default_registry
        theirs = shipped_default_registry(root)
    original = reg.read_text()
    retired = _retired(base, theirs, original) if base is not None else []
    mine = _blocks(original)
    added, corrected = [], []
    for path, body in _blocks(theirs):
        if path is None:
            continue
        at = next((i for i, (p, _) in enumerate(mine) if p == path), None)
        if at is None:
            added.append(path)
            mine.insert(_place(mine, path),
                        (path, ["".join(body).strip("\n") + "\n\n"]))
            continue
        entries = _entries(list(mine[at][1]))
        shipped_entries = {k: ls for k, ls in _entries(body) if k}
        known = {k for k, _ in entries if k}
        extra = [(k, ls) for k, ls in _entries(body) if k and k not in known]
        new_entries, table_changed = [], False
        for key, entry_lines in entries:
            if key and key in shipped_entries:
                entry_lines, did = _migrate_entry(
                    path, key, entry_lines, shipped_entries[key])
                if did:
                    table_changed = True
                    corrected.append("%s.%s" % (path, key))
            new_entries.append((key, entry_lines))
        if not extra and not table_changed:
            continue
        lines = [one for _, ls in new_entries for one in ls]
        end = len(lines)
        while end and not lines[end - 1].strip():
            end -= 1
        lines[end:end] = [one for _, ls in extra for one in ls]
        mine[at] = (path, lines)
        added.extend("%s.%s" % (path, k) for k, _ in extra)
    pruned = _prune(mine, retired) if prune and retired else []
    if not added and not corrected and not pruned:
        return {"path": reg, "added": [], "corrected": [],
                "retired": retired, "pruned": []}
    text = "".join(("" if p is None else "[%s]\n" % p) + "".join(b)
                   for p, b in mine)
    tomllib.loads(text)
    if apply:
        tmp = reg.with_suffix(".toml.sync-tmp")
        tmp.write_text(text)
        tmp.replace(reg)
    return {"path": reg, "added": added, "corrected": corrected,
            "retired": retired, "pruned": pruned}


def sync(home, root=None, *, apply=False, prune=False):
    """Plan, and with apply write, one cousin. Returns {"changed",
    "notes", "backup", "registry_added", "registry_corrected"}."""
    home = Path(home)
    root = Path(root) if root else FrameworkConfig.root_from_home(home)
    old, new, notes = plan(home, root, prune=prune)
    reg_result = _registry_sync(home, root, apply=apply)
    out = {"changed": new != old, "notes": notes, "backup": None,
           "registry_added": reg_result["added"],
           "registry_corrected": reg_result["corrected"]}
    if not apply or new == old:
        return out
    out["backup"] = str(_backup(home, old))
    tmp = home / "CLAUDE.md.sync-tmp"
    tmp.write_text(new)
    tmp.replace(home / "CLAUDE.md")
    return out
