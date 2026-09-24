"""The interactive CLI's transcript, read by the tmux kind (phase 11, R6,
R7, P11-9). The transcript is that kind's source of truth: which prompt
the model took, when a turn ended, how it ended. Hooks only wake the
runner up; nothing here trusts them.

The shapes are the ones measured on Claude Code 2.1.281 in a tmux pane
(docs/design/plans/phase-11-findings.md):

- a turn starts at a `user` entry with `promptSource` "typed" or "queued"
  and string or text content, not meta, carrying no `tool_result`; its
  `promptId` names the turn, and it is written when the model TAKES the
  prompt (a message queued behind a running turn is written after that
  turn ends);
- a normal end is a `system` entry with subtype `turn_duration`;
- an interrupted end is a `user` entry whose text starts
  "[Request interrupted by user" (the plain and the "for tool use"
  variant), carrying the turn's `promptId`; no `turn_duration` follows;
- an API error is an entry with `isApiErrorMessage`; one naming the usage
  limit (LIMIT_WORDS, the binary's wording; the live entry is not
  measured) is a limit, any other a failure.

A reader resumes from a byte offset and never passes a partial last line,
so a torn write (a SIGKILL mid-line) is read once it is complete. A torn
line the CLI then appends to merges with the next entry into one line
that does not parse; `turn_nonce` still finds a KNOWN nonce in it, at the
start of a JSON string (R7, P11-9), so the row it names is not typed twice."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from cousin_lib.config import expand_harness_path

LIMIT_WORDS = ("usage limit", "limit reached", "resets at")
NONCE_RE = re.compile(r"^\[inbox:([0-9a-f]{12})\]")
RAW_NONCE_RE = re.compile(r'"\[inbox:([0-9a-f]{12})\]')   # a nonce opening a JSON string
INTERRUPT_PREFIX = "[Request interrupted by user"
TURN_SOURCES = ("typed", "queued")


@dataclass(frozen=True)
class Entry:
    offset: int
    end: int
    kind: str       # turn_start | turn_end | interrupt | api_error | limit | assistant | tool_result | other
    prompt_id: str | None = None
    prompt_source: str | None = None
    nonce: str | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)


def locate(home, *, session_id, config_dir):
    """Where the CLI keeps this session's transcript: the account's config
    dir (None is the host login's ~/.claude), `projects/<home encoded>/`.
    The SessionStart hook reports the real path (run/tmux-session.json);
    this is the fallback when no hook has spoken yet."""
    base = Path(config_dir) if config_dir is not None else Path("~/.claude").expanduser()
    encoded = expand_harness_path("{home_encoded}", home)
    return base / "projects" / str(encoded) / ("%s.jsonl" % session_id)


def _texts(content):
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"]
    return []


def _has_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def classify(obj, offset, end):
    """One parsed line as an Entry (the module docstring's rules)."""
    if not isinstance(obj, dict):
        return Entry(offset, end, "other", raw={"_unparsed": repr(obj)})
    kind = obj.get("type")
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content")
    prompt_id = obj.get("promptId")
    if obj.get("isApiErrorMessage"):
        text = " ".join(_texts(content)).lower()
        is_limit = any(w in text for w in LIMIT_WORDS)
        return Entry(offset, end, "limit" if is_limit else "api_error", prompt_id=prompt_id, raw=obj)
    if kind == "system" and obj.get("subtype") == "turn_duration":
        return Entry(offset, end, "turn_end", prompt_id=prompt_id, raw=obj)
    if kind == "assistant":
        return Entry(offset, end, "assistant", prompt_id=prompt_id, raw=obj)
    if kind == "user":
        if _has_tool_result(content):
            return Entry(offset, end, "tool_result", prompt_id=prompt_id, raw=obj)
        texts = _texts(content)
        first = texts[0] if texts else ""
        source = obj.get("promptSource")
        # a turn start first: the interrupt entry carries no promptSource
        # (measured), so a typed prompt that opens with its text stays a prompt
        if source in TURN_SOURCES and any(t.strip() for t in texts) and not obj.get("isMeta"):
            m = NONCE_RE.match(first.split("\n", 1)[0])
            return Entry(offset, end, "turn_start", prompt_id=prompt_id, prompt_source=source,
                         nonce=m.group(1) if m else None, raw=obj)
        if first.startswith(INTERRUPT_PREFIX):
            return Entry(offset, end, "interrupt", prompt_id=prompt_id, raw=obj)
    return Entry(offset, end, "other", prompt_id=prompt_id, raw=obj)


def raw_nonces(entry, known):
    """The KNOWN nonces in a line that did not parse, each at the start of
    a JSON string, in order (R7, P11-9); [] for a parsed entry."""
    text = entry.raw.get("_unparsed") if isinstance(entry.raw, dict) else None
    if not text:
        return []
    return [n for n in RAW_NONCE_RE.findall(text) if n in known]


def turn_nonce(entry, known):
    """The known nonce a turn start names, or one a merged torn line holds."""
    if entry.kind == "turn_start":
        return entry.nonce if entry.nonce in known else None
    found = raw_nonces(entry, known)
    return found[-1] if found else None


def read_from(path, offset):
    """(entries, new_offset): every COMPLETE line from `offset` on. A
    partial last line is left for the next read; new_offset is just past
    the last complete line. A complete line that does not parse is an
    `other` entry with raw {"_unparsed": text}. A missing file is ([], offset)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    entries, pos = [], 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        line = data[pos:nl]
        start, end = offset + pos, offset + nl + 1
        text = line.decode("utf-8", "replace")
        try:
            obj = json.loads(text)
        except ValueError:
            entries.append(Entry(start, end, "other", raw={"_unparsed": text}))
        else:
            entries.append(classify(obj, start, end))
        pos = nl + 1
    return entries, offset + pos


class TranscriptStore:
    """The session store's two framework reads (`entries_after`,
    `tail_text`, session_store.py) over the CLI's own transcript file, so
    extract.mine_turn and the emergency handoff work unchanged for the
    tmux kind. The cursor is a byte offset into the file; `session_id`
    must be the file's own session."""

    TAIL_BYTES = 256 * 1024

    def __init__(self, path, session_id):
        self.path, self.session_id = Path(path), session_id

    def entries_after(self, session_id, cursor=0):
        if session_id != self.session_id:
            return [], int(cursor)
        entries, end = read_from(self.path, int(cursor))
        return [e.raw for e in entries if "_unparsed" not in e.raw], end

    def tail_text(self, session_id, max_chars=2000):
        """A BOUNDED tail, as the SQLite store's: the assistant text of the
        file's last TAIL_BYTES, never the whole session."""
        if session_id != self.session_id:
            return ""
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        start = max(0, size - self.TAIL_BYTES)
        entries, _ = read_from(self.path, start)
        texts = []
        for e in entries[1:] if start else entries:      # the first line may be cut
            if e.kind == "assistant":
                texts += _texts((e.raw.get("message") or {}).get("content"))
        return "\n".join(t for t in texts if t)[-max_chars:]
