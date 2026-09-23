"""The two checkpoint files in-process: Python equivalents of the harness
hooks `hooks/session_checkpoint.sh` (Stop) and `hooks/pre_compact.sh`
(PreCompact), same files and headings. Open work comes from
`data/state.json` when present, else STATUS.md's current `## Open loops`
block, else its open checkboxes; the pre-compact file adds the newest
event stream's tail, the SDK lane's terminal capture. Writes only under
<home>/data/; an empty home gets a file naming what was missing."""
import json
import os
import re
from datetime import datetime
from pathlib import Path

DECISIONS = 5
STATUS_LINES = 20
STREAM_EVENTS = 20
STREAM_TAIL_BYTES = 64 * 1024
EVENT_CHARS = 160
DISK_FILES = ("CLAUDE.md", "MEMORY.md", "STATUS.md", "PROFILE.md")
_OPEN_LOOPS = re.compile(r"^## Open loops[^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S)
_OPEN_BOX = re.compile(r"^- \[[ ~]\]")


def _section(title, body):
    return "## %s\n%s\n" % (title, body.rstrip("\n"))


def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def _decisions(data):
    text = _read(data / "decisions.jsonl")
    if text is None:
        return "None recorded."
    out = []
    for line in text.splitlines()[-DECISIONS:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append("- [%s] %s (why: %s)" % (e.get("topic", "?"), e.get("decision", ""),
                                               e.get("reasoning", "")))
    return "\n".join(out) or "None readable."


def _open_work(home):
    try:
        loops = json.loads(_read(home / "data" / "state.json") or "null")["open_loops"]
    except (ValueError, TypeError, KeyError):
        loops = None
    if isinstance(loops, list):
        items = ["- [%s] %s" % ("~" if i.get("partial") else " ", i.get("text", ""))
                 for i in loops if isinstance(i, dict) and not i.get("done")]
        return "\n".join(items) or "No open loops."
    status = _read(home / "STATUS.md")
    if status is None:
        return "No STATUS.md."
    m = _OPEN_LOOPS.search(status)
    if m and m.group(1).strip():
        lines = m.group(1).strip("\n").splitlines()
    else:
        lines = [ln for ln in status.splitlines() if _OPEN_BOX.match(ln)]
    return "\n".join(lines[:STATUS_LINES]) or "No open loops in STATUS.md."


def _event_text(payload):
    if not isinstance(payload, dict):
        return str(payload)
    for key in ("text", "error", "message"):
        if isinstance(payload.get(key), str):
            return payload[key]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:  # removed between the glob and the stat
        return -1.0


def _tail_lines(path):
    """The complete lines in the file's last STREAM_TAIL_BYTES."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - STREAM_TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[1:] if size > STREAM_TAIL_BYTES else lines


def _stream_tail(data):
    files = sorted((data / "stream").glob("*.jsonl"), key=_mtime)
    if not files:
        return "No event stream."
    out = []
    for line in _tail_lines(files[-1])[-STREAM_EVENTS:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        text = " ".join(_event_text(e.get("payload")).split())[:EVENT_CHARS]
        out.append("- %s: %s" % (e.get("kind", "?"), text))
    return "(%s)\n%s" % (files[-1].name, "\n".join(out) or "No events.")


def _files_on_disk(home):
    out = []
    for name in DISK_FILES:
        text = _read(home / name)
        if text is not None:
            out.append("- %s: %d lines" % (name, text.count("\n")))
    return "\n".join(out) or "None of %s." % ", ".join(DISK_FILES)


def _write(home, name, title, slug, now, sections):
    data = home / "data"
    data.mkdir(exist_ok=True)
    stamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%dT%H:%M:%S%z")
    slug = slug or os.environ.get("COUSIN_SLUG") or home.name
    body = "# %s - %s - %s\n\n" % (title, slug, stamp)
    body += "\n".join(_section(t, b) for t, b in sections)
    path, tmp = data / name, data / (name + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, path)
    return path


def write_session_checkpoint(home, *, slug=None, now=None):
    home, data = Path(home), Path(home) / "data"
    return _write(home, "session-checkpoint.md", "Session checkpoint", slug, now, [
        ("What was happening", _read(data / "last-activity.txt") or "No activity recorded."),
        ("Open work (from STATUS.md)", _open_work(home)),
        ("Last decisions", _decisions(data))])


def write_pre_compact_checkpoint(home, *, slug=None, now=None):
    home, data = Path(home), Path(home) / "data"
    return _write(home, "pre-compact-checkpoint.md", "Pre-compaction checkpoint", slug, now, [
        ("Current activity", _read(data / "last-activity.txt") or "Unknown."),
        ("Recent decisions", _decisions(data)),
        ("Open loops", _open_work(home)),
        ("Recent events (event stream tail)", _stream_tail(data)),
        ("Files on disk", _files_on_disk(home))])
