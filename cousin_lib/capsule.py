"""Reasoning capsules: structured, compressed summaries of a reasoning
chain, in place of "log every chain in full".

Each capsule is a conclusion (one to three sentences), its evidence
(bullets), the alternatives rejected on the way (bullets, optional),
a confidence (low / medium / high) and a truth level (this tree's
plain `cousin-conclusion` by default; `operator-stated` when the
operator said it).

Two stores, one write:

- <home>/memory/capsules.jsonl is the record: one object per line,
  append-only, what list_capsules and the boot packet read.
- <home>/memory/distilled/reasoning-capsules.md is the readable
  mirror, one markdown block per capsule separated by `---`, so the
  memory search indexes capsules like any other memory file.

The mirror sits beside the six distilled files but is not one of
them: the distiller regenerates exactly its six and never touches
this file. Should a marker ever appear in it (a person or a later
tool adding a generated tail), every new block lands ABOVE the
marker, in the curated region the distiller copies through verbatim.

Rotation bounds the mirror, because the boot assembler reads it
whole: past the size threshold the older blocks move to a dated
sibling archive and the newest tail stays live. The jsonl record is
never rotated by this module.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import distill, memory
from cousin_lib.trace import traced_cli

CONFIDENCES = ("low", "medium", "high")
DEFAULT_CONFIDENCE = "medium"
DEFAULT_TRUTH_LEVEL = memory.DEFAULT_TRUTH_LEVEL

MARKDOWN_TITLE = "# Reasoning Capsules\n\n"
BLOCK_SEPARATOR = "\n---\n"
CAPSULES_ROTATE_BYTES = 1_000_000
CAPSULES_KEEP_TAIL = 30
# Bound for the one-line boot rendering of a conclusion.
BOOT_CONCLUSION_CHARS = 220


class _NoContext(Exception):
    pass


def capsules_path(home):
    return Path(home) / "memory" / "capsules.jsonl"


def markdown_path(home):
    return Path(home) / "memory" / "distilled" / "reasoning-capsules.md"


def _clean_bullets(items, label):
    out = [" ".join(str(item).split()) for item in (items or [])]
    if any(not item for item in out):
        raise ValueError("%s: an empty bullet is not allowed" % label)
    return out


def _render_block(entry):
    body = ["## %s" % entry["id"],
            "_created: %s_" % entry["timestamp"]]
    if entry.get("topic"):
        body.append("_topic: %s_" % entry["topic"])
    body.append("_truth_level: %s_" % entry["truth_level"])
    body.append("_confidence: %s_" % entry["confidence"])
    body.append("")
    body.append("**Conclusion**")
    body.append(entry["conclusion"])
    body.append("")
    body.append("**Evidence**")
    body.extend("- %s" % item for item in entry["evidence"])
    if entry.get("rejected"):
        body.append("")
        body.append("**Rejected alternatives**")
        body.extend("- %s" % item for item in entry["rejected"])
    return "\n".join(body)


def _split_markdown(text):
    """(head, blocks, tail): the title and any hand-written prose
    before the first block, the capsule blocks, and everything from
    the distill marker down (empty when there is no marker)."""
    tail = ""
    if distill.AUTO_MARKER in text:
        text, below = text.split(distill.AUTO_MARKER, 1)
        tail = distill.AUTO_MARKER + below
    head, blocks = [], []
    for chunk in text.split(BLOCK_SEPARATOR):
        chunk = chunk.strip()
        if not chunk:
            continue
        # The title (and any prose) share a chunk with the first block:
        # nothing but a blank line separates them.
        prose, sep, block = chunk.partition("## capsule-")
        if prose.strip():
            head.append(prose.strip())
        if sep:
            blocks.append(sep + block)
    return ("\n\n".join(head) + "\n\n" if head else ""), blocks, tail


def _blocks(text):
    """The capsule blocks of a mirror file, oldest first."""
    return _split_markdown(text)[1]


def recent_blocks(home, n=3):
    """The newest n markdown blocks of the mirror, oldest first, for a
    surface that quotes capsules in full. Marker-free by construction:
    the split keeps the generated tail out of the blocks."""
    try:
        text = markdown_path(home).read_text()
    except OSError:
        return []
    blocks = _blocks(text)
    return blocks[-n:] if n else blocks


def _join_markdown(head, blocks, tail):
    out = head or MARKDOWN_TITLE
    if blocks:
        out += BLOCK_SEPARATOR.join(blocks) + BLOCK_SEPARATOR
    if tail:
        out = out.rstrip("\n") + "\n\n" + tail
    return out


def _write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _append_block(path, block):
    """One block into the mirror, above the marker when one exists,
    at the end otherwise. Rewrites the file only when a marker forces
    an insert; the plain case is an append."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ""
    if distill.AUTO_MARKER in text:
        head, blocks, tail = _split_markdown(text)
        _write_atomic(path, _join_markdown(head, blocks + [block], tail))
        return
    with open(path, "a") as fh:
        if not text:
            fh.write(MARKDOWN_TITLE)
        elif not text.endswith("\n"):
            fh.write("\n")
        fh.write(block + BLOCK_SEPARATOR)


def _rotate_if_needed(path):
    """Archive the older blocks once the mirror passes the threshold;
    returns the archive path when a rotation happened. Append-mode
    archive, so a same-day re-rotation is safe. Head and marker tail
    stay where they are."""
    try:
        if path.stat().st_size < CAPSULES_ROTATE_BYTES:
            return None
        text = path.read_text()
    except OSError:
        return None
    head, blocks, tail = _split_markdown(text)
    if len(blocks) <= CAPSULES_KEEP_TAIL:
        return None
    older, keep = blocks[:-CAPSULES_KEEP_TAIL], blocks[-CAPSULES_KEEP_TAIL:]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    archive = path.with_name("reasoning-capsules-archive-%s.md" % stamp)
    try:
        with open(archive, "a") as fh:
            fh.write(BLOCK_SEPARATOR.join(older) + BLOCK_SEPARATOR)
        _write_atomic(path, _join_markdown(head, keep, tail))
    except OSError:
        return None
    return archive


def write_capsule(home, *, conclusion, evidence, rejected=None,
                  confidence=DEFAULT_CONFIDENCE,
                  truth_level=DEFAULT_TRUTH_LEVEL, topic=""):
    """Record one capsule in both stores. Returns the jsonl path.
    Refuses an empty conclusion, empty evidence, an empty bullet, or
    a confidence outside CONFIDENCES."""
    conclusion = " ".join(str(conclusion or "").split())
    if not conclusion:
        raise ValueError("conclusion: must not be empty")
    evidence = _clean_bullets(evidence, "evidence")
    if not evidence:
        raise ValueError("evidence: at least one bullet is required")
    rejected = _clean_bullets(rejected, "rejected")
    if confidence not in CONFIDENCES:
        raise ValueError("confidence: %r is not one of %s"
                         % (confidence, ", ".join(CONFIDENCES)))
    now = datetime.now(timezone.utc)
    entry = {
        "id": "capsule-%d-%s" % (int(now.timestamp()), os.urandom(2).hex()),
        "timestamp": now.isoformat(timespec="seconds"),
        "topic": " ".join(str(topic or "").split()),
        "conclusion": conclusion,
        "evidence": evidence,
        "rejected": rejected,
        "confidence": confidence,
        "truth_level": str(truth_level or DEFAULT_TRUTH_LEVEL),
    }
    store = capsules_path(home)
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    mirror = markdown_path(home)
    _append_block(mirror, _render_block(entry))
    _rotate_if_needed(mirror)
    return store


def list_capsules(home, n=10):
    """The newest n capsules, newest first. A missing store is empty;
    an unparsable line is skipped."""
    try:
        lines = capsules_path(home).read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("conclusion"):
            entries.append(entry)
    entries.reverse()
    return entries[:n] if n else entries


def summary_for_boot(home, n=5):
    """A bounded `## Reasoning capsules` block for the memories layer:
    the newest n conclusions as one line each, or "" when there are
    none. Reads the jsonl record, so no marker can leak into it."""
    lines = []
    for entry in list_capsules(home, n=n):
        conclusion = entry["conclusion"][:BOOT_CONCLUSION_CHARS]
        tags = [str(entry.get("timestamp", ""))[:10]]
        if entry.get("topic"):
            tags.append("topic: %s" % entry["topic"])
        lines.append("- [%s] %s (%s)" % (
            entry.get("confidence", DEFAULT_CONFIDENCE), conclusion,
            "; ".join(t for t in tags if t)))
    if not lines:
        return ""
    return "## Reasoning capsules\n\n" + "\n".join(lines)


def _home(args):
    home = getattr(args, "home", None) or os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext(
            "no cousin context - set COUSIN_HOME or pass --home"
            " (refusing to fall back into another cousin's memory)")
    return Path(home).resolve()


def _cmd_capsule(args):
    home = _home(args)
    try:
        write_capsule(home, conclusion=args.conclusion,
                      evidence=args.evidence, rejected=args.rejected,
                      confidence=args.confidence,
                      truth_level=args.truth_level, topic=args.topic or "")
    except ValueError as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2
    print(list_capsules(home, n=1)[0]["id"])
    return 0


def _format(entry):
    lines = ["%s  [%s] %s" % (entry["id"], entry.get("confidence", "?"),
                               str(entry.get("timestamp", ""))[:19])]
    if entry.get("topic"):
        lines.append("  topic: %s" % entry["topic"])
    lines.append("  %s" % entry["conclusion"])
    lines.extend("  + %s" % item for item in entry.get("evidence", []))
    lines.extend("  - %s" % item for item in entry.get("rejected", []))
    return "\n".join(lines)


def _cmd_list(args):
    entries = list_capsules(_home(args), n=args.n)
    if not entries:
        print("(no capsules)")
        return 0
    print("\n\n".join(_format(e) for e in entries))
    return 0


@traced_cli("cousin-reason")
def reason_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-reason",
        description="reasoning capsules: a conclusion with its evidence"
                    " and rejected alternatives, kept under memory/")
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("capsule", help="write a reasoning capsule")
    p.add_argument("--conclusion", required=True)
    p.add_argument("--evidence", action="append", required=True,
                   help="evidence bullet, repeatable")
    p.add_argument("--rejected", action="append", default=[],
                   help="rejected alternative, repeatable")
    p.add_argument("--confidence", default=DEFAULT_CONFIDENCE,
                   choices=CONFIDENCES)
    p.add_argument("--truth-level", dest="truth_level",
                   default=DEFAULT_TRUTH_LEVEL)
    p.add_argument("--topic")
    p.set_defaults(func=_cmd_capsule)
    p = sub.add_parser("list", help="show recent capsules, newest first")
    p.add_argument("--n", type=int, default=10)
    p.set_defaults(func=_cmd_list)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except _NoContext as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(reason_main())
