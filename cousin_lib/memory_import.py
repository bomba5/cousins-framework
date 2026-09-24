"""The one-time import of the agent CLI's own memory (spec, "One memory
system"; master plan phase 7 task 5).

The harness keeps an auto-memory directory per cousin
(config/harness.toml `auto_memory_dir`). The SDK lane switches it off
(runner.sdk.AUTO_MEMORY_OFF), so what it holds must fold into framework
memory or be lost to recall. Each `*.md` there is copied to
memory/imported/auto/<name>, the CLI's own index MEMORY.md included (it
lists the others, and on the SDK lane nothing else would): the
frontmatter kept, three provenance keys appended to it (imported_from,
imported_sha256, imported_at). Anything else there (the CLI's backups, a
database) is listed as `ignore` and never copied. Text is read with
universal newlines, so a CRLF file is copied with LF line ends.

Dry run by default: plan() writes nothing. apply() is idempotent. The
manifest (memory/imported/auto/.manifest.json) maps each name to the
sha256 of the source it copied and of the file it wrote: an unchanged
source is skipped, a changed one is updated, a copy the cousin edited
since is never overwritten (a conflict, listed by name), and a copy the
cousin removed is `dropped`: never imported again, whatever its source
does.
memory_search skips a harness file whose imported copy is current, so
one memory is one hit."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib.config import expand_harness_path, harness_config

TARGET = ("memory", "imported", "auto")
MANIFEST = ".manifest.json"
ACTIONS = ("import", "update", "skip", "conflict", "dropped", "ignore")
# A frontmatter block: `---`, its lines, `---` closing at a line end or at
# the end of the file (a file with no final newline).
_FRONT = re.compile(r"\A---\n(.*?)\n---(\n|\Z)", re.S)


def sha256(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def source_dir(home, *, root):
    """The harness auto-memory directory of `home` under `root`, or None
    when the install declares none or it does not exist."""
    template = (harness_config(root) or {}).get("auto_memory_dir")
    if not template:
        return None
    path = expand_harness_path(template, home)
    return path if path.is_dir() else None


def target_dir(home):
    return Path(home).joinpath(*TARGET)


class ManifestError(ValueError):
    """The manifest exists and cannot be read as one: an import refuses,
    because without it an edited copy looks new and a removed one looks
    never imported."""


def load_manifest(home, *, strict=False):
    """{name: {"source": sha256, "written": sha256}}; {} when absent.
    A manifest that exists but will not parse (or is not a JSON object)
    is {} for a reader that only needs a hint (search's dedupe) and a
    ManifestError with strict=True (plan and apply: the manifest is the
    record that protects an edit or a removal)."""
    path = target_dir(home) / MANIFEST
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        if strict:
            raise ManifestError("%s cannot be read (%s); fix or remove it by hand, nothing"
                                " was imported" % (path, err))
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ManifestError("%s is not a JSON object; fix or remove it by hand, nothing"
                                " was imported" % path)
        return {}
    return data


def render(name, text, *, imported_at):
    """The imported file: the source's frontmatter kept, provenance appended
    to it; a source without frontmatter gets one holding only provenance."""
    provenance = "imported_from: %s\nimported_sha256: %s\nimported_at: %s" % (
        name, sha256(text), imported_at)
    match = _FRONT.match(text)
    if match:
        cut = match.end(1)
        return text[:cut] + "\n" + provenance + text[cut:]
    return "---\n" + provenance + "\n---\n" + text


_IMPORTED_AT = re.compile(r"^imported_at: (.*)$", re.M)


def _is_its_import(name, text, target):
    """True when `target` holds exactly what importing `text` wrote: the
    source rendered again with the copy's own imported_at. A copy with no
    manifest row is then the work of a run that died before its manifest
    write, and importing it again is safe; anything else is a copy
    someone edited."""
    try:
        written = target.read_text(errors="replace")
    except OSError:
        return False
    front = _FRONT.match(written)
    stamp = _IMPORTED_AT.search(front.group(1)) if front else None
    return bool(stamp) and render(name, text, imported_at=stamp.group(1)) == written


def plan(home, *, root):
    """One row per file in the harness directory, {"name", "action",
    "reason"}, action one of ACTIONS. Writes nothing. ManifestError when
    the manifest exists and will not parse."""
    home = Path(home)
    src = source_dir(home, root=root)
    if src is None:
        return []
    manifest = load_manifest(home, strict=True)
    rows = []
    for path in sorted(p for p in src.iterdir() if p.is_file()):
        name = path.name
        if path.suffix != ".md":
            rows.append({"name": name, "action": "ignore", "reason": "not a memory file"})
            continue
        seen = manifest.get(name)
        text = path.read_text(errors="replace")
        current = sha256(text)
        target = target_dir(home) / name
        if not isinstance(seen, dict) and target.exists() \
                and not _is_its_import(name, text, target):
            rows.append({"name": name, "action": "conflict",
                         "reason": "a copy exists that the manifest does not record;"
                                   " merge by hand"})
        elif not isinstance(seen, dict):
            rows.append({"name": name, "action": "import", "reason": "new"})
        elif not target.exists():
            rows.append({"name": name, "action": "dropped",
                         "reason": "the imported copy was removed; never imported again"})
        elif seen.get("source") == current:
            rows.append({"name": name, "action": "skip", "reason": "unchanged since the last import"})
        elif target.exists() and sha256(target.read_text(errors="replace")) != seen.get("written"):
            rows.append({"name": name, "action": "conflict",
                         "reason": "the imported copy was edited since; merge by hand"})
        else:
            rows.append({"name": name, "action": "update",
                         "reason": "the source changed since the last import"})
    return rows


def apply(home, *, root, now=None):
    """plan(), then write every import and update, each file and the
    manifest atomically. Returns the plan's rows."""
    home = Path(home)
    rows = plan(home, root=root)
    todo = [r for r in rows if r["action"] in ("import", "update")]
    if not todo:
        return rows
    src = source_dir(home, root=root)
    tdir = target_dir(home)
    tdir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(home, strict=True)
    when = (now or datetime.now(timezone.utc)).isoformat()
    for row in todo:
        text = (src / row["name"]).read_text(errors="replace")
        out = render(row["name"], text, imported_at=when)
        tmp = tdir / (row["name"] + ".tmp")
        tmp.write_text(out)
        tmp.replace(tdir / row["name"])
        manifest[row["name"]] = {"source": sha256(text), "written": sha256(out)}
    tmp = tdir / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    tmp.replace(tdir / MANIFEST)
    # Usage bonuses are keyed by absolute path, and R9 hides the original
    # that carried them: the copy inherits its original's history.
    from cousin_lib import reinforce
    reinforce.carry(home, {str(src / r["name"]): str(tdir / r["name"]) for r in todo})
    return rows


def format_plan(rows, *, applied):
    """The CLI's text: a header, one line per file, then the counts."""
    head = ("applied:" if applied
            else "dry run: nothing written (pass --apply to import)")
    lines = [head] + ["  %-8s %s  (%s)" % (r["action"], r["name"], r["reason"]) for r in rows]
    counts = ", ".join("%d %s" % (sum(r["action"] == a for r in rows), a)
                       for a in ACTIONS if any(r["action"] == a for r in rows))
    lines.append(counts or "no harness auto-memory declared for this cousin")
    return "\n".join(lines)
