"""The boot packet's layers and budget engine.

What a fresh session boots from is the runner's system prompt and its
state digest (runner/prompt.py); the digest reads its layers through
the readers here and fits them with fit(), within a hard total budget.
The generation counter and its start record live here too. Three rules
shape this module, each earned against a real incident in an earlier
version:

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
- The law is a HARD layer: fit() never cuts it, at any maximum and at
  any total. A number for it would be wrong the day someone adds a
  rule, and a truncated law is a cousin that boots having never read the
  rules that bind it - including the two that keep one cousin's memory
  out of another's. The per-layer cap used to cut it silently: the cap
  was applied by the loop that runs before the overflow loop, so the
  one layer TRUNCATE_ORDER excludes was still cut by the layer above it.
"""
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from cousin_lib import capsule, distill, memory, status_sections
from cousin_lib.config import FrameworkConfig

CHARS_PER_TOKEN = 4
TOTAL_MAX_CHARS = 8000 * CHARS_PER_TOKEN

LAYER_BUDGETS = {
    # The law is in here for the record only: HARD_LAYERS takes it out of
    # both cut passes below, so its floor is never used and its maximum
    # is never applied. See HARD_LAYERS and the module docstring.
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
}

# Layers fit() may never cut, per-layer or by overflow. A law trimmed to
# fit is a cousin that never read rules 9-14: the total ceiling is not
# met by amputating the one layer whose content is the framework's own.
HARD_LAYERS = ("law",)

# Overflow victims first to last; law is never truncated.
TRUNCATE_ORDER = [
    "memories", "trace_summary", "calibration",
    "task_packet", "active_state", "shared", "self_portrait",
]


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
    """The next generation, written through a tmp file and a rename: a
    reader in another session (a side session's boundary) sees the old
    number or the new one, never an empty file read as 0. A bump is a
    generation start, so it records one (mark_generation_start); that
    record failing never fails the bump, which has happened."""
    path = Path(home) / "data" / "generation.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    generation = read_generation(home) + 1
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(generation))
    os.replace(tmp, path)
    try:
        mark_generation_start(home, generation=generation)
    except OSError:
        pass        # generation_started falls back to generation.txt
    try:
        # a cousin dreaming "rollover" gets a pass after each generation:
        # the loops daemon picks the request up at its next tick
        from cousin_lib import dreaming
        if dreaming.settings(home)["mode"] == "rollover":
            dreaming.request(home)
    except OSError:
        pass        # a missed request is a missed pass, never a failed bump
    return generation


GENERATION_STARTED = "generation-started.json"


def mark_generation_start(home, *, generation=None, now=None):
    """Record that the current generation's session started now:
    data/generation-started.json, {"generation", "started"} (epoch
    seconds), through a tmp file and a rename. The framework writes it
    at every generation start: a bump (a rollover or a flip, every
    runner kind) and a runner's fresh start of its primary session (a
    first boot, a resume that came back as a new session), which moves
    no generation. The daily flip reads it (generation_started). Raises
    OSError; a caller that must not fail says so."""
    path = Path(home) / "data" / GENERATION_STARTED
    path.parent.mkdir(parents=True, exist_ok=True)
    if generation is None:
        generation = read_generation(home)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"generation": generation,
                               "started": time.time() if now is None else now}))
    os.replace(tmp, path)


def generation_started(home):
    """When the current generation's session started, as epoch seconds,
    or None when the cousin has never started one. The record
    mark_generation_start keeps; a home from before it falls back to
    generation.txt's last change (its last bump), then to a primary
    session on file, started at a time nobody recorded: 0.0, as old as
    can be."""
    data = Path(home) / "data"
    try:
        record = json.loads((data / GENERATION_STARTED).read_text())
        return float(record["started"])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        return (data / "generation.txt").stat().st_mtime
    except OSError:
        pass
    try:
        on_file = json.loads((data / "runner-session.json").read_text())
    except (OSError, ValueError):
        return None
    if isinstance(on_file, dict) and on_file.get("session_id"):
        return 0.0
    return None


def fit(sections, budgets, order, total_max):
    """The budget engine. Every layer `budgets` names is cut to its
    maximum; then victims in `order`, first to last, are cut to their
    minimum, one per pass, until the total fits or every victim is at
    its minimum. Pure: returns a new dict and never mutates `sections`;
    a layer `budgets` does not name is never touched (the runner's law).
    A layer in `HARD_LAYERS` is never cut.
    The module docstring's rules hold here; the runner's state digest
    calls this with the numbers and order above."""
    out = dict(sections)
    for name, (_min, max_chars) in budgets.items():
        if name in HARD_LAYERS:
            continue
        if name in out and len(out[name]) > max_chars:
            out[name] = _truncate(out[name], max_chars, name)
    while sum(len(v) for v in out.values()) > total_max:
        for victim in order:
            if victim not in out or victim not in budgets:
                continue
            if len(out[victim]) > budgets[victim][0]:
                out[victim] = _truncate(out[victim], budgets[victim][0],
                                        victim + " (overflow)")
                break
        else:
            break  # everything at minimum; cannot shrink further
    return out


def _root(root=None):
    return Path(root) if root is not None else FrameworkConfig.from_env().root


def _law_path(root=None):
    return _root(root) / "config" / "law.md"


def law_text(root=None):
    """The law as written; "" when the install has none."""
    return _read(_law_path(root))


def shared_parts(root=None):
    """(rules, index) of the canonical shared tier, every cousin's
    regardless of its own memory scope. A `kind: rule` entry is quoted
    in full (an operator rule a cousin never sees is not followed);
    every other entry is one index line. The runner puts the rules in
    its byte-stable system prompt and the index in the digest: they
    change at different rates. `root=None` reads the environment's root."""
    base = _root(root) / "shared"
    rules, index = [], []
    for path in sorted(base.glob("*.md")) if base.is_dir() else []:
        fields, body = _frontmatter(_read(path))
        if fields.get("kind") == "rule":
            rules.append("### %s\n%s" % (path.stem, body.strip()))
        else:
            index.append("- `%s`: %s" % (path.name, fields.get("description") or path.stem))
    return rules, index


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


def _open_loops_section(status):
    """STATUS.md's live open loops, read the way the handoff writes them
    (cousin_lib.status_sections): the first bare "## Open loops" heading
    up to the next level-1 or level-2 heading; None when there is none.
    The digest reads the same section."""
    return status_sections.open_loops_section(status)


def _active_state(home, *, open_loops=_open_loops_section):
    parts = []
    stale = _staleness_header(home)
    if stale:
        parts.append(stale.rstrip())
    status = _read(Path(home) / "STATUS.md")
    if status:
        section = open_loops(status)
        if section and section.strip() != "## Open loops":
            parts.append("### STATUS.md (open loops)")
            parts.append(section.strip())
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
    its consumer, the state digest), the newest reasoning capsules, recent raw-memory
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
        # the same exclusions as the distilled views: a retired or held
        # entry, an entry-level mark, the review gate's records
        from cousin_lib import review_gate
        try:
            history = memory._all_raw(home)
            hidden = memory.hidden_ids(history)
            # held entries that exist: one removed from raw (the trash) is
            # no longer held by anything, so it is not counted
            waiting = review_gate.pending_ids(history)
            held = len({memory.entry_id(e) for e in history} & waiting)
        except Exception:  # noqa: BLE001 - the digest still composes
            hidden, held = set(), 0
        lines = []
        for path in sorted(raw_dir.glob("*.jsonl"))[-14:]:
            for line in _read(path).splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or memory.view_noise(entry) \
                        or memory.entry_id(entry) in hidden:
                    continue
                lines.append("- [%s] %s"
                             % (entry.get("topic", "?"),
                                entry.get("content", "")[:200]))
        if lines:
            parts.append("### recent raw memory (newest last)")
            parts.append("\n".join(lines[-60:]))
        if held:
            parts.append("%d memory entr%s held by the review gate, out of this"
                         " packet until the operator keeps or drops %s"
                         " (`cousin-memory review`)." % (held, "y is" if held == 1 else "ies are",
                                                         "it" if held == 1 else "them"))
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


def _is_degraded(name, content, sections):
    """Per-layer explicit rules; see the module docstring for why this
    is never a substring scan."""
    if name == "law":
        # Empty is the whole of it: an absent law file is an install
        # problem (seed_law never ran, or was deleted), and a cousin
        # booting with no Framework Law is the worst boot there is, so it
        # is reported rather than filed as somebody else's problem.
        return not content
    if name in ("shared", "trace_summary", "memories"):
        # The trace idle marker and empty memories are a new cousin's
        # legitimate starting condition, not a gap.
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
    if name == "task_packet":
        if not content:
            return True
        if content.startswith("(no in-flight tasks"):
            active = sections.get("active_state", "")
            return not active or active.startswith(
                "(no active state - degraded boot)")
        return False
    return not content
