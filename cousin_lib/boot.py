"""Boot packet assembly.

Deterministic composition of the cold-start packet a fresh session
boots from, within a hard total budget. Three rules shape this module,
each earned against a real incident in an earlier version:

- The TOTAL ceiling governs. Per-layer maxima exist, but their sum is
  allowed to exceed the ceiling and the composed packet still may not:
  enforcing only per-layer caps let a multi-bloated home blow well
  past the documented budget.
- A truncation marker counts INSIDE the budget it truncates to. A
  marker appended beyond the slice leaves the section longer than its
  budget, and the overflow loop then picks the same victim again,
  forever.
- Degraded detection is per-layer explicit logic, never a substring
  scan over section text: the scan flagged healthy cousins every
  morning on legitimate fallback strings.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import (capsule, corrections, distill, memory,
                        self_portrait, trace)
from cousin_lib.config import FrameworkConfig

CHARS_PER_TOKEN = 4
TOTAL_MAX_CHARS = 8000 * CHARS_PER_TOKEN

LAYER_BUDGETS = {
    "law": (500 * CHARS_PER_TOKEN, 800 * CHARS_PER_TOKEN),
    # Operator rules every cousin follows, in full, then a one-line
    # index of the rest of the shared tier.
    "shared": (400 * CHARS_PER_TOKEN, 1500 * CHARS_PER_TOKEN),
    "self_portrait": (800 * CHARS_PER_TOKEN, 1500 * CHARS_PER_TOKEN),
    "calibration": (300 * CHARS_PER_TOKEN, 800 * CHARS_PER_TOKEN),
    "active_state": (500 * CHARS_PER_TOKEN, 1500 * CHARS_PER_TOKEN),
    "task_packet": (500 * CHARS_PER_TOKEN, 2000 * CHARS_PER_TOKEN),
    "trace_summary": (500 * CHARS_PER_TOKEN, 1500 * CHARS_PER_TOKEN),
    "memories": (1000 * CHARS_PER_TOKEN, 4000 * CHARS_PER_TOKEN),
    # The tool-surface manifest is a list, not prose: bounded in
    # characters, and the first overflow victim.
    "tool_surface": (300, 1500),
}

# Overflow victims first to last; law is never truncated.
TRUNCATE_ORDER = [
    "tool_surface", "memories", "trace_summary", "calibration",
    "task_packet", "active_state", "shared", "self_portrait",
]

REQUIRED_BOOT_ACTIONS = """## 10. Required Boot Actions

You must now (INTERNALLY, do not announce):
1. Reconstruct the current objective in one mental paragraph.
2. Identify the next action from active-threads.
3. Verify boot completeness; declare degraded internally if a layer
   is missing.
4. Continue silently. Do NOT post a respawn announcement unless the
   operator explicitly asked for a confirmation.
5. Before exit, four writes in this order (the same order the flip
   and the clean stop ask for): reconcile STATUS.md; write
   data/active-threads.md, one bullet per in-flight thread; save what
   the session learned that is not in memory yet (cousin-memory
   remember / decide); LAST, data/handoff.md. The session ends as soon
   as handoff.md changes, so anything after it can be lost.
6. Memory writes default to the cousin-conclusion truth level (L3).
   What the operator told you is operator-stated (L0): record it with
   `cousin-memory remember "<topic>" "<fact>" --level operator --cite
   "<where they said it>"`; unverified guesses go in at --level
   hypothesis.
"""


def _read(path):
    try:
        return Path(path).read_text()
    except (FileNotFoundError, OSError):
        return ""


def _truncate(text, max_chars, label):
    """Truncate to max_chars INCLUDING the overflow marker."""
    if len(text) <= max_chars:
        return text
    marker = "\n\n... (truncated, %s, budget hit)" % label
    if max_chars <= len(marker):
        return marker[:max_chars]
    return text[:max_chars - len(marker)] + marker


def read_generation(home):
    try:
        return int((Path(home) / "data" / "generation.txt")
                   .read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def bump_generation(home):
    path = Path(home) / "data" / "generation.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    generation = read_generation(home) + 1
    path.write_text(str(generation))
    return generation


def _law_path():
    return FrameworkConfig.from_env().root / "config" / "law.md"


TOOL_SURFACE_ABSENT = (
    "(no tool-surface manifest at data/tool-surface.md - degraded;"
    " run cousin-tool-surface, or enable its timer)")


def _tool_surface():
    """The manifest cousin-tool-surface writes under the framework
    root, its own title line dropped (this section already has one).
    Absent or empty means degraded: the cousin boots without knowing
    its CLIs, and the fix is one command away."""
    from cousin_lib.tool_surface import MANIFEST_RELPATH
    text = _read(FrameworkConfig.from_env().root / MANIFEST_RELPATH)
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    return body or TOOL_SURFACE_ABSENT


def _calibration_base(home):
    """Distilled calibration file when present, else the committed
    portrait's calibration section. Absent means degraded: a
    persona-anchored cousin booting without calibration should know."""
    distilled = _distilled_body(
        Path(home) / "memory" / "distilled" / "operator-calibration.md")
    if distilled:
        return "## operator-calibration.md\n\n" + distilled
    section = self_portrait.md_section(
        _read(self_portrait.committed_path(home)), "Operator Calibration"
    )
    if section and "TODO" not in section:
        return "## Operator Calibration (from self-portrait)\n" + section
    return "(no operator calibration distilled yet - degraded)"


def _calibration(home):
    """The calibration base plus the recent-corrections summary when any
    are recorded. Appended, never prepended: the degraded rule keys on
    the section's first line, and corrections without a distilled
    calibration are still a degraded boot."""
    base = _calibration_base(home)
    summary = corrections.summary_for_boot(home, n=15)
    if summary == corrections.EMPTY_MARKER:
        return base
    return base + "\n\n" + summary


def _staleness_header(home):
    """Warn when decisions were logged after STATUS's last edit. The
    next session anchors on STATUS as authoritative; silent drift
    there poisons the whole orientation."""
    home = Path(home)
    try:
        status_mtime = (home / "STATUS.md").stat().st_mtime
    except OSError:
        return ""
    newer = 0
    try:
        with open(home / "data" / "decisions.jsonl") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                ts = entry.get("timestamp") or entry.get("ts") or ""
                try:
                    when = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if when > status_mtime:
                    newer += 1
    except OSError:
        pass
    if not newer:
        return ""
    age_h = (datetime.now().timestamp() - status_mtime) / 3600.0
    return (
        "> STALE WARNING: STATUS.md mtime is %.1fh old; %d decision(s)"
        " logged after. Verify against data/decisions.jsonl before"
        " acting on it.\n\n" % (age_h, newer)
    )


def _active_state(home):
    parts = []
    stale = _staleness_header(home)
    if stale:
        parts.append(stale.rstrip())
    status = _read(Path(home) / "STATUS.md")
    if status:
        m = re.search(r"## Open loops.*?(?=\n## |\Z)", status, re.DOTALL)
        if m and m.group(0).strip() != "## Open loops":
            parts.append("### STATUS.md (open loops)")
            parts.append(m.group(0).strip())
        else:
            parts.append("### STATUS.md")
            parts.append(status[:1500])
    handoff = _read(Path(home) / "data" / "handoff.md")
    if handoff:
        parts.append("### handoff.md (most recent snapshot)")
        parts.append(handoff[:1500])
    return "\n\n".join(parts) if parts else "(no active state - degraded boot)"


def _task_packet(home):
    parts = []
    threads = _read(Path(home) / "data" / "active-threads.md")
    if threads:
        parts.append("### active-threads.md")
        parts.append(threads[:1500])
    chunks = capsule.recent_blocks(home, n=3)
    if chunks:
        parts.append("### last reasoning capsules")
        parts.append("\n---\n".join(chunks))
    return "\n\n".join(parts) if parts \
        else "(no in-flight tasks - check STATUS.md)"


def _distilled_body(path):
    """A distilled file as the packet should quote it: the auto marker
    and generated header stripped; the stub reads as empty."""
    text = _read(path)
    if not text or memory.STUB_TEXT in text:
        return ""
    return distill.strip_auto_marker(text)


def _memories(home, max_chars):
    """The durable floor (memory/distilled, regenerated from raw by
    assemble), the newest reasoning capsules, recent raw-memory
    entries (the decide bridge is their producer) and the memory index
    head. Empty is the legitimate
    starting condition of a new cousin."""
    parts = []
    for fname in memory.DISTILLED_FILES:
        if fname == "operator-calibration.md":
            continue  # the calibration layer carries it
        body = _distilled_body(Path(home) / "memory" / "distilled" / fname)
        if body:
            parts.append("## %s" % fname)
            parts.append(body)
    # Newest conclusions as one line each, read from the jsonl record
    # (never the markdown mirror, so no marker can reach the packet).
    capsules = capsule.summary_for_boot(home, n=5)
    if capsules:
        parts.append(capsules)
    raw_dir = Path(home) / "memory" / "raw"
    if raw_dir.is_dir():
        lines = []
        for path in sorted(raw_dir.glob("*.jsonl"))[-14:]:
            for line in _read(path).splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                lines.append("- [%s] %s"
                             % (entry.get("topic", "?"),
                                entry.get("content", "")[:200]))
        if lines:
            parts.append("### recent raw memory (newest last)")
            parts.append("\n".join(lines[-60:]))
    index = _read(Path(home) / "MEMORY.md").strip()
    if index:
        parts.append("### memory index (head)")
        parts.append(index[:1000])
    out = "\n\n".join(parts)
    return _truncate(out, max_chars, "memories")


_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def _frontmatter(text):
    """(fields, body) of a shared entry; flat `key: value` lines only."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    fields = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields, text[m.end():]


def _shared():
    """The canonical shared tier, every cousin's regardless of its own
    memory scope (scope governs nominating, not reading). An entry
    whose frontmatter says `kind: rule` is quoted in full: it is an
    operator rule the fleet follows, and a rule a cousin never sees is
    not followed. Every other entry is one index line; the cousin reads
    it with `cousin-shared read <file>` when it is relevant."""
    root = FrameworkConfig.from_env().root / "shared"
    rules, index = [], []
    for path in sorted(root.glob("*.md")) if root.is_dir() else []:
        fields, body = _frontmatter(_read(path))
        if fields.get("kind") == "rule":
            rules.append("### %s\n%s" % (path.stem, body.strip()))
        else:
            index.append("- `%s`: %s" % (
                path.name, fields.get("description") or path.stem))
    parts = []
    if rules:
        parts.append("Operator rules every cousin follows:")
        parts.extend(rules)
    if index:
        parts.append("### Shared reference (read with `cousin-shared "
                     "read <file>` when relevant)")
        parts.append("\n".join(index))
    return "\n\n".join(parts)


def _is_degraded(name, content, sections):
    """Per-layer explicit rules; see the module docstring for why this
    is never a substring scan."""
    if name in ("law", "shared", "trace_summary", "memories"):
        # A missing law file is an install problem, not a per-cousin
        # gap; the trace idle marker and empty memories are a new
        # cousin's legitimate starting condition.
        return False
    if name == "self_portrait":
        return not content or content.startswith(
            "(no committed self-portrait yet")
    if name == "calibration":
        return not content or content.startswith(
            "(no operator calibration distilled yet")
    if name == "active_state":
        return not content or content.startswith(
            "(no active state - degraded boot)")
    if name == "tool_surface":
        return not content or content.startswith(
            "(no tool-surface manifest")
    if name == "task_packet":
        if not content:
            return True
        if content.startswith("(no in-flight tasks"):
            active = sections.get("active_state", "")
            return not active or active.startswith(
                "(no active state - degraded boot)")
        return False
    return not content


def assemble(slug, home, *, generation=None):
    """Compose the boot packet. Returns text, sizes, identity hashes,
    generation, and the named degraded layers."""
    home = Path(home)
    if generation is None:
        generation = read_generation(home)
    portrait = _read(self_portrait.committed_path(home))
    law = _read(_law_path())
    status = _read(home / "STATUS.md")
    handoff = _read(home / "data" / "handoff.md")
    hashes = {
        "identity_hash": hashlib.sha256(
            (portrait + law).encode()).hexdigest()[:12],
        "state_hash": hashlib.sha256(
            (status + handoff).encode()).hexdigest()[:12],
        "memory_snapshot": datetime.now(timezone.utc)
        .isoformat(timespec="seconds"),
    }
    # The durable layer is a derived view of raw: regenerate it here so
    # every packet reads a fresh floor without any cousin habit or
    # timer (the consumer triggers the producer, like search
    # self-heal). Best-effort: a failed distill boots a staler floor,
    # never no boot.
    try:
        distill.distill(home)
    except Exception:
        pass
    sections = {
        "law": law.strip(),
        "shared": _shared(),
        "self_portrait": self_portrait.for_boot_packet(home).strip(),
        "calibration": _calibration(home),
        "active_state": _active_state(home),
        "task_packet": _task_packet(home),
        "trace_summary": trace.summary_for_boot(slug),
        "memories": _memories(home, LAYER_BUDGETS["memories"][1]),
        "tool_surface": _tool_surface(),
    }
    degraded = [k for k, v in sections.items()
                if _is_degraded(k, v, sections)]
    for name, (_min, max_chars) in LAYER_BUDGETS.items():
        if len(sections[name]) > max_chars:
            sections[name] = _truncate(sections[name], max_chars, name)
    total_max = TOTAL_MAX_CHARS - len(REQUIRED_BOOT_ACTIONS) - 600
    while sum(len(v) for v in sections.values()) > total_max:
        for victim in TRUNCATE_ORDER:
            min_chars = LAYER_BUDGETS[victim][0]
            if len(sections[victim]) > min_chars:
                sections[victim] = _truncate(
                    sections[victim], min_chars, victim + " (overflow)")
                break
        else:
            break  # everything at minimum; cannot shrink further
    body = [
        "BOOT PACKET FOR COUSIN: %s" % slug,
        "Generation: %d" % generation,
        "identity_hash: %s" % hashes["identity_hash"],
        "state_hash: %s" % hashes["state_hash"],
        "memory_snapshot: %s" % hashes["memory_snapshot"],
    ]
    if degraded:
        body.append("DEGRADED layers: %s" % ", ".join(sorted(degraded)))
    for number, title, key in (
        (1, "Framework Law", "law"),
        (2, "Shared Rules and Fleet Memory", "shared"),
        (3, "Cousin Self-Portrait", "self_portrait"),
        (4, "Operator Calibration", "calibration"),
        (5, "Active State", "active_state"),
        (6, "Current Task Packet", "task_packet"),
        (7, "Recent Tool Trace Summary", "trace_summary"),
        (8, "Retrieved Memories", "memories"),
        (9, "Tool Surface", "tool_surface"),
    ):
        body.append("")
        body.append("## %d. %s" % (number, title))
        body.append(sections[key])
    body.append("")
    body.append(REQUIRED_BOOT_ACTIONS)
    text = "\n".join(body)
    return {
        "text": text,
        "chars": len(text),
        "approx_tokens": len(text) // CHARS_PER_TOKEN,
        "hashes": hashes,
        "generation": generation,
        "degraded_sections": sorted(degraded),
    }
