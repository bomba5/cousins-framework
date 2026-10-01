"""The prompt is composed, not copied (spec, same heading).

    system prompt = the claude_code preset
                  + the law                 immutable, never truncated
                  + the framework contract  generated (contract.py)
                  + the authored identity   the operator's words
                  + operator rules          the shared tier's kind: rule entries
    first message = the state digest (state_digest, below)

The system prompt MUST be byte-stable across generations: nothing here
reads a clock, a counter, the generation, a hash or any state file. The digest carries all of that, under the boot
packet's budget rules (boot.fit). Every read is under the `root` the
caller passes; nothing here discovers a root from the environment.

The text never goes on a command line: an argv is readable by every
local user (ps, /proc/<pid>/cmdline) and lands in process listings and
crash reports. It is written to a private file in the cousin's home
(write_private: 0600 in a 0700 directory, atomically) and the CLI gets
the file's path with `--append-system-prompt-file`. data/run/ is where
the runner already keeps the files it writes for the CLI it starts (the
tmux kind's context block and resume pointer)."""
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import boot, corrections, distill, self_portrait, template_sync, trace
from cousin_lib.runner import contract

IDENTITY_ABSENT = (
    "## Identity (degraded)\n\n"
    "No authored identity is on disk for this cousin: no committed self-portrait"
    " and no authored sections in CLAUDE.md. This is a degraded state, not a"
    " licence to invent one. Use the plain professional register of your role:"
    " no persona, no pet names, no invented history. Tell the operator the"
    " identity layer is missing when it matters.")

_KEEP_HEADINGS = ("## Identity", "## Voice")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TITLE = re.compile(r"^# .+ - .+$")
_PARA = re.compile(r"\n\s*\n")


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _norm(text):
    return " ".join(text.split())


def _paragraphs(text):
    return [p.strip() for p in _PARA.split(_COMMENT.sub("", text)) if p.strip()]


def _template_paragraphs(root, home):
    """The rendered CLAUDE.md template's paragraphs, normalised: framework
    text, never authored. A paragraph still holding a {{placeholder}} (the
    role paragraph, the voice guide) is the authored slot, not template
    text, and is left out. No template or no cousin.toml: an empty set,
    so nothing is filtered (and the doctrine may then reach the prompt;
    the contract's precedence sentence covers that case)."""
    try:
        text = template_sync._render(template_sync._template_text(root),
                                     template_sync._values(home))
    except Exception:  # noqa: BLE001 - a missing template filters nothing
        return set()
    return {_norm(p) for p in _paragraphs(text) if "{{" not in p}


def _claude_identity(text, template):
    """The operator-authored parts of a CLAUDE.md."""
    if not text.strip():
        return ""
    kept = []
    first = text.splitlines()[0].strip()
    if _TITLE.match(first):
        kept.append(first)
    head, marker, tail = text.partition(template_sync.MARKER)
    _pre, sections = template_sync._sections(head)
    for title, body in sections:
        if title not in _KEEP_HEADINGS:
            continue
        paras = [p for p in _paragraphs(body) if _norm(p) not in template]
        if paras:
            kept.append("%s\n\n%s" % (title, "\n\n".join(paras)))
    if marker:
        paras = [p for p in _paragraphs(tail) if _norm(p) not in template]
        if paras:
            kept.append("\n\n".join(paras))
    return "\n\n".join(kept)


def authored_identity(home, *, root):
    """(text, degraded). The CLAUDE.md authored parts, then the committed
    self-portrait; the fixed IDENTITY_ABSENT when neither has any."""
    home = Path(home)
    portrait = _read(self_portrait.committed_path(home)).strip()
    claude = _claude_identity(_read(home / "CLAUDE.md"), _template_paragraphs(root, home))
    title_only = bool(claude) and _TITLE.fullmatch(claude) is not None
    if not portrait and (not claude or title_only):
        return IDENTITY_ABSENT, True
    return "\n\n".join(p for p in (claude, portrait) if p), False


def compose_system_prompt(home, *, root, registry, version=None, tool_name=None, runner=None,
                          other_servers=contract.OTHER_SERVERS):
    """law + contract + identity + operator rules. Never truncated.
    `tool_name` names the tools for the lane (contract.render); None is
    the SDK lane's `mcp__cousin__<name>`. `runner` is the lane's name in
    the contract (contract.render); None is the SDK runner."""
    identity, _degraded = authored_identity(home, root=root)
    return _compose(root, registry, version, identity=identity.strip(), tool_name=tool_name,
                    runner=runner, other_servers=other_servers)


def compose_context_block(home, *, root, registry, version=None):
    """The tmux kind's block: law + contract (the pane's
    runner label, the stdio server's `cousin` tool names) + operator rules,
    no identity: the pane's CLI reads the identity from CLAUDE.md itself.
    The runner writes it to data/run/tmux-context.md; the launcher appends it
    on a fresh start."""
    return _compose(root, registry, version, identity=None, runner=contract.PANE_RUNNER)


def _compose(root, registry, version, *, identity, tool_name=None, runner=None,
             other_servers=contract.OTHER_SERVERS):
    if version is None:
        from cousin_lib.version import version as _v
        version = _v()
    law = boot.law_text(root).strip()
    rules, _index = boot.shared_parts(root)
    sections = []
    if law:
        sections.append("# Framework law\n\n" + law)
    sections.append(contract.render(registry, version, tool_name=tool_name,
                                    runner=runner, other_servers=other_servers).strip())
    if identity is not None:
        sections.append(identity.strip())
    if rules:
        sections.append("# Operator rules every cousin follows\n\n" + "\n\n".join(rules))
    return "\n\n".join(sections) + "\n"


SYSTEM_PROMPT_FILE = ("data", "run", "system-prompt.md")
APPEND_FILE_FLAG = "append-system-prompt-file"     # the CLI's flag, without its dashes


def system_prompt_path(home):
    """Where the SDK runner's composed prompt is handed to the CLI: an
    absolute path, so the CLI reads it whatever its cwd."""
    return Path(os.path.abspath(Path(home).joinpath(*SYSTEM_PROMPT_FILE)))


def write_private(path, text):
    """`text` to `path`, readable by this user only: the directory made or
    narrowed to 0700, the file 0600, written to a temporary file beside it
    and renamed over the old one, so a reader sees the old text or the new,
    never a part. A failure leaves the old file and no temporary one."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def system_prompt_option(home, *, root, registry, version=None):
    """The SDK's SystemPromptPreset for this cousin, WITHOUT the text: the
    SDK would put an `append` on the CLI's argv. The composed text is
    written here, at every call (every connect: a rollover or a restart
    with a changed identity gets the new text), to system_prompt_path(home);
    the caller passes that path with APPEND_FILE_FLAG in extra_args."""
    write_private(system_prompt_path(home),
                  compose_system_prompt(home, root=root, registry=registry, version=version))
    return {"type": "preset", "preset": "claude_code",
            "exclude_dynamic_sections": True, "snapshot": True}


# ---------------------------------------------------------------- the digest

DIGEST_LAYERS = ("calibration", "active_state", "task_packet", "trace_summary",
                 "memories", "shared_index")
DIGEST_BUDGETS = {name: boot.LAYER_BUDGETS["shared" if name == "shared_index" else name]
                  for name in DIGEST_LAYERS}
DIGEST_ORDER = [("shared_index" if v == "shared" else v) for v in boot.TRUNCATE_ORDER
                if ("shared_index" if v == "shared" else v) in DIGEST_LAYERS]
DIGEST_HEADER_ALLOWANCE = 600
DIGEST_MAX_CHARS = boot.TOTAL_MAX_CHARS - DIGEST_HEADER_ALLOWANCE

# Absent when empty BY DESIGN: calibration (the prompt carries the
# portrait's) and the shared index (no entries). Every other layer always
# prints its header, as boot does: an empty layer that is legitimately empty
# (a new cousin's memories) is information, not a missing section.
_OMIT_WHEN_EMPTY = ("calibration", "shared_index")

_TITLES = (("Operator Calibration", "calibration"), ("Active State", "active_state"),
           ("Current Task Packet", "task_packet"), ("Recent Tool Trace Summary", "trace_summary"),
           ("Retrieved Memories", "memories"), ("Shared Reference", "shared_index"))


# The digest reads STATUS.md's open loops exactly the way the handoff writes
# them: the one shared definition, the same one the boot packet reads.
_open_loops = boot._open_loops_section


def _calibration(home):
    """The distilled calibration and the recent corrections, or "".
    Never the portrait's section: the prompt already carries it."""
    parts = []
    body = boot._distilled_body(Path(home) / "memory" / "distilled" / "operator-calibration.md")
    if body:
        parts.append("## operator-calibration.md\n\n" + body)
    summary = corrections.summary_for_boot(home, n=15)
    if summary != corrections.EMPTY_MARKER:
        parts.append(summary)
    return "\n\n".join(parts)


def _shared_index(root):
    _rules, index = boot.shared_parts(root)
    if not index:
        return ""
    return ("Read an entry with `cousin-shared read <file>` when it is relevant:\n"
            + "\n".join(index))


def state_digest(home, *, root, slug, generation=None):
    """The first message of a generation: the volatile layers, budgeted by
    boot.fit with the boot packet's numbers and order. The layers that
    moved into the system prompt (law, identity, operator rules) and the
    retired tool surface are not here. Every read is under `root`."""
    home = Path(home)
    if generation is None:
        generation = boot.read_generation(home)
    try:
        distill.distill(home)   # the floor is regenerated by its consumer, as assemble does
    except Exception:  # noqa: BLE001 - a failed distill digests a staler floor, never none
        pass
    sections = {
        "calibration": _calibration(home),
        "active_state": boot._active_state(home, open_loops=_open_loops),
        "task_packet": boot._task_packet(home),
        "trace_summary": trace.summary_for_boot(slug, root=root),
        "memories": boot._memories(home, DIGEST_BUDGETS["memories"][1]),
        "shared_index": _shared_index(root),
    }
    degraded = [k for k in ("active_state", "task_packet", "trace_summary", "memories")
                if boot._is_degraded(k, sections[k], sections)]
    if authored_identity(home, root=root)[1]:
        degraded.append("identity")
    sections = boot.fit(sections, DIGEST_BUDGETS, DIGEST_ORDER, DIGEST_MAX_CHARS)
    state = _read(home / "STATUS.md") + _read(home / "data" / "handoff.md")
    state_hash = hashlib.sha256(state.encode()).hexdigest()[:12]
    body = ["STATE DIGEST FOR COUSIN: %s" % slug,
            "Generation: %d" % generation,
            "state_hash: %s" % state_hash,
            "memory_snapshot: %s" % datetime.now(timezone.utc).isoformat(timespec="seconds")]
    if degraded:
        body.append("DEGRADED layers: %s" % ", ".join(sorted(degraded)))
    number = 0
    for title, key in _TITLES:
        if not sections[key] and key in _OMIT_WHEN_EMPTY:
            continue
        number += 1
        body += ["", "## %d. %s" % (number, title), sections[key]]
    text = "\n".join(body) + "\n"
    return {"text": text, "chars": len(text), "generation": generation,
            "degraded_sections": sorted(degraded), "state_hash": state_hash}
