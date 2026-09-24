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
so a torn write (a SIGKILL mid-line) is read once it is complete."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from cousin_lib.config import expand_harness_path

LIMIT_WORDS = ("usage limit", "limit reached", "resets at")
NONCE_RE = re.compile(r"^\[inbox:([0-9a-f]{12})\]")
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
        if first.startswith(INTERRUPT_PREFIX):
            return Entry(offset, end, "interrupt", prompt_id=prompt_id, raw=obj)
        source = obj.get("promptSource")
        if source in TURN_SOURCES and texts and not obj.get("isMeta"):
            m = NONCE_RE.match(first.split("\n", 1)[0])
            return Entry(offset, end, "turn_start", prompt_id=prompt_id, prompt_source=source,
                         nonce=m.group(1) if m else None, raw=obj)
    return Entry(offset, end, "other", prompt_id=prompt_id, raw=obj)


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
