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

It runs by itself at every start and flip (spawn.start_cousin), so nobody
has to remember it; `cousin-spawn <slug> --sync-template` shows the diff
and `--apply` writes it by hand. Every write keeps the old file in
data/claude-md-backups/, like the console's CLAUDE.md editor.
The MCP registry gets the same treatment, additively: a tool the shipped
registry has and the cousin's does not is appended.
"""
import difflib
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


def plan(home, root=None):
    """(old_text, new_text, notes) for one cousin; new == old when it is
    in step. Raises SyncError when the file cannot be synced safely."""
    home = Path(home)
    root = Path(root) if root else FrameworkConfig.root_from_home(home)
    old = (home / "CLAUDE.md").read_text()
    if MARKER not in old:
        raise SyncError("no '%s' line: cannot tell the framework part"
                        " from the cousin's own" % MARKER)
    tpl = _template_text(root)
    if MARKER not in tpl:
        raise SyncError("the template has no marker line")
    tpl_top = tpl[:tpl.index(MARKER)]
    _tpl_pre, tpl_secs = _sections(tpl_top)
    values = _values(home)
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


def diff(home, root=None):
    old, new, notes = plan(home, root)
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


def _registry_additions(home, root):
    """Tool blocks the shipped registry has and the cousin's lacks."""
    reg = Path(home) / "mcp-registry.toml"
    if not reg.is_file():
        return None, ""
    from cousin_lib.mcp_server import shipped_default_registry
    shipped = shipped_default_registry(root)
    mine = reg.read_text()
    have = set(tomllib.loads(mine).get("tools", {}))
    blocks, current, name = [], [], None
    for line in shipped.splitlines(keepends=True):
        m = re.match(r"^\[tools\.([A-Za-z0-9_-]+)(\.[^\]]*)?\]", line)
        if m and m.group(2) is None:
            if name and name not in have:
                blocks.append("".join(current))
            name, current = m.group(1), [line]
        elif name:
            if re.match(r"^\[(?!tools\.)", line):
                if name not in have:
                    blocks.append("".join(current))
                name, current = None, []
            else:
                current.append(line)
    if name and name not in have:
        blocks.append("".join(current))
    return reg, "".join(b.rstrip("\n") + "\n\n" for b in blocks)


def sync(home, root=None, *, apply=False):
    """Plan, and with apply write, one cousin. Returns
    {"changed", "notes", "backup", "registry_added"}."""
    home = Path(home)
    root = Path(root) if root else FrameworkConfig.root_from_home(home)
    old, new, notes = plan(home, root)
    out = {"changed": new != old, "notes": notes, "backup": None,
           "registry_added": []}
    reg, extra = _registry_additions(home, root)
    if extra:
        out["registry_added"] = re.findall(r"^\[tools\.([^\].]+)\]", extra,
                                           re.M)
    if not apply:
        return out
    if new != old:
        out["backup"] = str(_backup(home, old))
        tmp = home / "CLAUDE.md.sync-tmp"
        tmp.write_text(new)
        tmp.replace(home / "CLAUDE.md")
    if extra:
        text = reg.read_text().rstrip("\n") + "\n\n" + extra
        tomllib.loads(text)
        tmp = reg.with_suffix(".toml.sync-tmp")
        tmp.write_text(text)
        tmp.replace(reg)
    return out
