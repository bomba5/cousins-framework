"""The previous conversation, handed across a move to the SDK runner (#103).

`cousin-migrate apply` stops the tmux session cleanly and the runner then
starts a FRESH session on the state digest: by design the working
conversation does not carry. It is still on disk, as the harness
transcript of the tmux lane's last session. After the close, `apply`
records where (record()); the runner's first fresh start that finds the
record appends note() to its digest message and then consume()s the
record, so a later rollover does not repeat it; a rollback removes it
(remove()).

The record, data/previous-transcript.json:

  lane         "tmux"
  ended_at     when the close finished (ISO, UTC): data, not a clock read
               at start time
  session_id   the tmux lane's last session ([runtime] session_id), or None
  transcripts  [{which, session_id, path}]: "last" is that session's
               transcript; "before" is the newest other transcript in the
               same directory (by modification time), when there is one
  missing      why the last session's transcript was not found, or None

The path is resolved as the rest of the framework resolves harness
transcripts (transcript_mine: config/harness.toml transcripts_dir).
Nothing here raises into its caller: a transcript that cannot be found
is recorded as missing, never a failed migration."""
import json
import os
from pathlib import Path

RECORD = "data/previous-transcript.json"
CONSUMED = RECORD + ".consumed"

COST_LINE = ("The working conversation does not carry: the new session starts from the"
             " state digest, the handoff and memory, and is handed the previous"
             " transcript path")


def locate(home, root, *, ended_at):
    """The record for `home`'s tmux lane (see the module doc). Never raises."""
    from cousin_lib import transcript_mine
    from cousin_lib.config import read_session_id
    home = Path(home)
    rec = {"lane": "tmux", "ended_at": ended_at, "session_id": None, "transcripts": [],
           "missing": None}
    try:
        sid = read_session_id(home) or None
        rec["session_id"] = sid
        base = transcript_mine.transcripts_dir(home, root)
        if base is None:
            rec["missing"] = "config/harness.toml names no transcripts_dir"
            return rec
        last = base / ("%s.jsonl" % sid) if sid else None
        if last is not None and last.is_file():
            rec["transcripts"].append({"which": "last", "session_id": sid, "path": str(last)})
        elif last is None:
            rec["missing"] = "no runtime.session_id in cousin.toml"
        else:
            rec["missing"] = "%s is not on disk" % last
        others = []
        for path in base.glob("*.jsonl"):
            if path == last:
                continue
            try:
                others.append((path.stat().st_mtime, path))
            except OSError:
                continue
        if others:
            before = max(others)[1]
            rec["transcripts"].append({"which": "before", "session_id": before.stem,
                                       "path": str(before)})
    except Exception as err:  # noqa: BLE001 - a lookup that fails is recorded, never raised
        rec["missing"] = "%s: %s" % (type(err).__name__, err)
    return rec


def record(home, root, *, ended_at):
    """locate(), written atomically to RECORD. Returns the record; one
    that could not be written says so in `missing`, never raises."""
    rec = locate(home, root, ended_at=ended_at)
    path = Path(home) / RECORD
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(rec, indent=1) + "\n")
        os.replace(tmp, path)
    except OSError as err:
        rec["missing"] = "%s; and %s could not be written: %s" % (
            rec["missing"] or "found", RECORD, err)
    return rec


def read(home):
    """The record, or None when there is none (or it does not parse)."""
    try:
        rec = json.loads((Path(home) / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


_WHICH = {"last": "the last session", "before": "the one before it"}


def paragraph(rec):
    """The fixed paragraph for a record: no clock, only the record's own
    data, so the same record always gives the same bytes."""
    found = [t for t in rec.get("transcripts") or () if isinstance(t, dict) and t.get("path")]
    lines = ["", "## Your conversation before the move",
             "You were moved from the tmux lane to the SDK runner; the tmux session ended"
             " at %s. The working conversation did not carry: this session starts from"
             " the digest above, the handoff and memory." % (rec.get("ended_at") or "an"
                                                              " unrecorded time")]
    if rec.get("missing"):
        lines.append("The last session's transcript was not found: %s." % rec["missing"])
    if found:
        lines.append("Your conversation from before the move is on disk, read-only:")
        lines += ["- %s (%s)" % (t["path"], _WHICH.get(t.get("which"), "a transcript"))
                  for t in found]
        lines.append("Never read it into this context. Have a subagent read it from the"
                     " end and extract only the user and assistant text, then rebuild"
                     " your active work into STATUS.md, data/handoff.md and memory.")
    else:
        lines.append("Rebuild your active work into STATUS.md, data/handoff.md and memory"
                     " from what you have.")
    return "\n".join(lines) + "\n"


def note(home):
    """The paragraph for `home`'s record, or None when there is none."""
    rec = read(home)
    return None if rec is None else paragraph(rec)


def consume(home):
    """The record, once handed: renamed to CONSUMED. Best-effort."""
    try:
        os.replace(Path(home) / RECORD, Path(home) / CONSUMED)
    except OSError:
        pass


def remove(home):
    """Rollback's half: the record and a consumed one removed. The
    names removed."""
    gone = []
    for rel in (RECORD, CONSUMED):
        try:
            (Path(home) / rel).unlink()
            gone.append(rel)
        except FileNotFoundError:
            pass
    return gone
