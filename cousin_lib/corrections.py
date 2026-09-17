"""Corrections capture: the calibration trajectory, not just the rule.

An operator's "no", "stop", "actually", "instead" in an injected message
is calibration data. Recording each one with its class gives a
persona-anchored cousin the cause -> correction arc at boot, where a
distilled rule alone says what to do but not what it was corrected from.

Storage is per-cousin: <home>/data/corrections.jsonl, one object per
line. Detection is a fixed list of generic English patterns; the first
match in priority order names the class. Corrections outrank acceptance
so "good, but don't ..." lands as a correction.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# (pattern, class) in priority order: the first match wins. Halts and
# negative directives before the softer classes; acceptance last.
CORRECTION_PATTERNS = [
    (r"\bstop\b", "halt"),
    (r"\bdon'?t\b", "negative_directive"),
    (r"\bdo not\b", "negative_directive"),
    (r"\byou shouldn'?t\b", "negative_directive"),
    (r"\bno\b[,.\s]", "rejection"),
    (r"\bI notice (?:you|that)\b", "meta_correction"),
    (r"\b(?:tone|voice|register) (?:is|should)\b", "style_coaching"),
    (r"\binstead\b", "redirect"),
    (r"\byou should\b", "directive"),
    (r"\btry\b", "directive"),
    (r"\bactually\b", "soft_correction"),
    (r"\bI want\b", "preference_positive"),
]

ACCEPTANCE_PATTERNS = [
    r"\bgood\b", r"\bperfect\b", r"\bexactly\b", r"\bnice\b",
    r"^\s*ok\s*[.!]?\s*$", r"^\s*ack\b", r"^\s*yes\s*[.,!]",
]

CLASSES = frozenset(kind for _, kind in CORRECTION_PATTERNS) | {"acceptance"}

EMPTY_MARKER = "(no corrections recorded yet)"
_SUMMARY_TEXT_CHARS = 140
_STORED_TEXT_CHARS = 1000


def _path(home):
    return Path(home) / "data" / "corrections.jsonl"


def detect(text):
    """The correction class of one operator message, or None."""
    if not text:
        return None
    for pattern, kind in CORRECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return kind
    for pattern in ACCEPTANCE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            return "acceptance"
    return None


def record(home, *, user, text, kind):
    """Append one correction to <home>/data/corrections.jsonl."""
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": user,
        "kind": kind,
        "text": (text or "")[:_STORED_TEXT_CHARS],
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def detect_and_record(home, *, user, text):
    """Detect and, on a hit, record. Returns the class or None."""
    kind = detect(text)
    if kind is not None:
        record(home, user=user, text=text, kind=kind)
    return kind


def _entries(home):
    path = _path(home)
    if not path.is_file():
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
    return out


def summary_for_boot(home, n=15):
    """The last n corrections, newest first, one line each, or the
    empty marker. Text is flattened and bounded so one pasted wall
    cannot eat the calibration layer's budget."""
    entries = _entries(home)[-n:]
    if not entries:
        return EMPTY_MARKER
    lines = ["# Recent operator corrections (last %d)" % len(entries)]
    for entry in reversed(entries):
        text = (entry.get("text") or "")[:_SUMMARY_TEXT_CHARS]
        text = " ".join(text.split())
        lines.append('- [%s] "%s"' % (entry.get("kind", "?"), text))
    return "\n".join(lines)
