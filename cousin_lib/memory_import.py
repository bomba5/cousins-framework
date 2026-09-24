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
one memory is one hit.

verify() is the check (master task 5's recall regression test), before
against after. The queries are the cousin's own (memory/.recall-log.jsonl,
written by reinforce.record on every search). Just before apply() writes,
take_baseline() replays the newest logged queries that surfaced a
harness file and keeps what each surfaces NOW; verify() replays the same
query text and reports any that lost a memory the baseline had (a
`dropped` copy is not a loss). The log is only where the queries come
from: its own results are history, and ranking and usage bonuses move.
A logged query is at most 120 characters (reinforce keeps no more), and
before and after replay that same text."""
import hashlib
import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib.config import expand_harness_path, harness_config

TARGET = ("memory", "imported", "auto")
MANIFEST = ".manifest.json"
BASELINE = ".baseline.json"
REPLAY_SAMPLE = 50           # how many of the newest logged queries a baseline replays
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


def apply(home, *, root, now=None, sample=REPLAY_SAMPLE):
    """plan(), then, when anything is to be written, take_baseline()
    first and write every import and update, each file and the manifest
    atomically. Returns the plan's rows."""
    home = Path(home)
    rows = plan(home, root=root)
    todo = [r for r in rows if r["action"] in ("import", "update")]
    if not todo:
        return rows
    take_baseline(home, root=root, sample=sample)
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


def _logged_queries(home, base, sample):
    """(query, [harness file names], depth) for the newest `sample` logged
    searches that surfaced a file under `base`; depth is how many hits
    that search returned."""
    from cousin_lib import reinforce
    try:
        lines = reinforce._log_path(home).read_text().splitlines()
    except OSError:
        return []
    prefix = str(base) + os.sep
    out = []
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        paths = [str(p) for p in event.get("paths") or []]
        names = sorted({Path(p).name for p in paths if p.startswith(prefix)})
        if event.get("query") and names:
            out.append((event["query"], names, len(paths)))
        if len(out) >= sample:
            break
    return out


def _index_current(home, root):
    """Both indexes brought fully current, with no foreground budget,
    before a replay searches: a search embeds at most FOREGROUND_BUDGET
    stale chunks, and right after an import every copy is a new key, so a
    replay on a partly built index would measure the index filling in,
    not the import (R19)."""
    from cousin_lib import memory_search
    memory_search.build_index(home, root)
    config = memory_search._embedding_config(root)
    if isinstance(config, dict):
        memory_search.ensure_index(home, config, root=root, wait=True)


def _surfaced(home, root, query, top, src):
    """The names of the harness files, or their imported copies, that
    `query` surfaces now within `top` hits. record=False: a replay must
    not reinforce what it measures."""
    from cousin_lib import memory_search
    bases = (str(src) + os.sep, str(target_dir(home)) + os.sep)
    hits, _notice = memory_search.search(query, top=top, home=Path(home), root=root,
                                         record=False)
    return {Path(h["path"]).name for h in hits if str(h["path"]).startswith(bases)}


def take_baseline(home, *, root, sample=REPLAY_SAMPLE):
    """Before an import writes: replay the newest `sample` logged queries
    that surfaced a harness file and keep, per query, what it surfaces
    from that directory NOW. A query that surfaces none of it now cannot
    regress and is left out. Written to memory/imported/auto/.baseline.json."""
    home = Path(home)
    src = source_dir(home, root=root)
    rows = []
    if src:
        _index_current(home, root)
    for query, _names, depth in (_logged_queries(home, src, sample) if src else []):
        found = _surfaced(home, root, query, max(depth, 1), src)
        if found:
            rows.append({"query": query, "top": max(depth, 1), "names": sorted(found)})
    base = {"taken_at": datetime.now(timezone.utc).isoformat(), "queries": rows}
    tdir = target_dir(home)
    tdir.mkdir(parents=True, exist_ok=True)
    tmp = tdir / (BASELINE + ".tmp")
    tmp.write_text(json.dumps(base, indent=1) + "\n")
    tmp.replace(tdir / BASELINE)
    return base


def verify(home, *, root):
    """Replay the baseline's queries now. A query is KEPT when every name
    it surfaced in the baseline is surfaced again, as the harness file or
    as its imported copy, within the same number of hits; a `dropped`
    copy is not a loss. Returns {"baseline": bool, "queries", "kept",
    "lost": [{"query", "missing": [names]}]}."""
    home = Path(home)
    src = source_dir(home, root=root)
    try:
        base = json.loads((target_dir(home) / BASELINE).read_text())
    except (OSError, ValueError):
        return {"baseline": False, "queries": 0, "kept": 0, "lost": []}
    dropped = {r["name"] for r in plan(home, root=root) if r["action"] == "dropped"}
    _index_current(home, root)
    kept, lost = 0, []
    for row in base.get("queries") or []:
        found = _surfaced(home, root, row["query"], int(row["top"]), src)
        missing = [n for n in row["names"] if n not in found and n not in dropped]
        if missing:
            lost.append({"query": row["query"], "missing": missing})
        else:
            kept += 1
    return {"baseline": True, "queries": kept + len(lost), "kept": kept, "lost": lost}


def format_verify(report):
    if not report["baseline"]:
        return ("no baseline: run import-auto --apply first; it replays your logged"
                " queries before it writes anything")
    n = report["queries"]
    if not n:
        return ("replayed 0 queries: nothing in your recall log reached that memory,"
                " so this check proved nothing")
    lines = ["replayed %d logged %s: %d kept, %d lost" % (
        n, "query" if n == 1 else "queries", report["kept"], len(report["lost"]))]
    lines += ["  lost: %r no longer surfaces %s" % (row["query"], ", ".join(row["missing"]))
              for row in report["lost"]]
    return "\n".join(lines)


def format_plan(rows, *, applied):
    """The CLI's text: a header, one line per file, then the counts."""
    head = ("applied:" if applied
            else "dry run: nothing written (pass --apply to import)")
    lines = [head] + ["  %-8s %s  (%s)" % (r["action"], r["name"], r["reason"]) for r in rows]
    counts = ", ".join("%d %s" % (sum(r["action"] == a for r in rows), a)
                       for a in ACTIONS if any(r["action"] == a for r in rows))
    lines.append(counts or "no harness auto-memory declared for this cousin")
    return "\n".join(lines)
