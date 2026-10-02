"""The memory perimeter: the surfaces an agent-initiated write never reaches.

Law rules 11 and 12 say a private cousin is never named in cross-cousin
chat, shared memory or a fleet-visible commit, and that private content
never appears in another cousin's boot packet. Until this module those
two rules were prose with nothing behind them: every writer in the
framework would have accepted the path if it had been handed one, and a
subagent's Write would have gone wherever it was pointed.

The perimeter is three shapes, matched on the path and not on a root, so
a caller needs no configuration to be checked:

| shape              | what it is                                        |
|--------------------|---------------------------------------------------|
| `.../config/law.md`   | the Framework Law, seeded once at install        |
| `.../self-portrait.md`| a cousin's committed portrait (law rule 3: authored, never improvised) |
| `.../shared/*.md`     | a canonical file in the shared tier              |

Three deliberate exclusions, each because the protected side is not the
writing side:

- `shared/proposed/**` is the tier's single agent-side entry path
  (`shared_tier.propose`); a proposal is the one shared write a cousin
  legitimately makes, and promotion is a reviewed move that a reviewer
  authorises, not a path a flag can authorise.
- `shared/examples/**` and `shared/audit.jsonl` are install and audit
  furniture, not memory.
- `templates/law.md` is the shipped copy the install seeds from.
- Operator-initiated surfaces are outside the writer list below, because
  they are not agent-initiated: the seed (`shared_tier.seed_law`), the
  console law editor (`console.routes_system`, which backs up and
  audits), `self_portrait.commit_candidate` and the body-swap's identity
  trade (`lifecycle._swap_bodies`).

Shape, not root: the check is pure and offline, so the Bash chokepoint
can apply it to a path-like token without resolving an install, and a
cousin in a worktree is held to the same rule as one in the live home.

What it does not do:

- It is not the shared tier's boundary. `shared_tier._check_reviewer` is,
  and that one is stronger: no configuration expresses it away. This
  module only stops a writer from landing a file it should not have.
- It cannot see a Bash command that hides the path behind a variable, a
  glob or `find -exec`. `check_tool`'s command scan is path matching on
  the command text and is therefore BEST-EFFORT; Edit, Write and
  NotebookEdit name their path exactly and are not. An operator who
  wants Bash covered too writes `deny_bash_patterns` in policy.toml.
- It does not make the primary session unable to edit its own law or
  portrait. Operator-directed edits are legitimate and frequent; the
  rule this enforces is that a BACKGROUND pass cannot make them. So the
  tool chokepoint (`runner.hooks.gate`) applies this only to a payload
  carrying `agent_id`, and the framework's own writers call
  `assert_writable` unconditionally, because a framework writer is
  agent-initiated by definition.
"""
import shlex
from pathlib import Path

LAW_TAIL = ("config", "law.md")
PORTRAIT_NAME = "self-portrait.md"
SHARED_DIR = "shared"
SHARED_SUBDIRS = ("proposed", "examples")

# Tools that name their target path as an exact field.
PATH_FIELDS = ("file_path", "notebook_path")
BASH_FIELD = "command"


class PerimeterRefused(Exception):
    """A write to a protected surface was refused; the message names the
    shape and says where the write belongs instead."""


def _parts(path):
    """The path's parts, whether it was given as a string, a Path, or
    with a trailing slash. Never resolves: a symlink's target is the
    caller's business, and resolving would reach into the filesystem on
    a check that must stay cheap enough for the Bash chokepoint."""
    text = str(path or "").strip()
    if not text:
        return ()
    return tuple(p for p in Path(text).as_posix().split("/") if p)


def protected_reason(path):
    """Why `path` is protected, or None when it is not. The reason names
    the surface and its owner, because a refusal a caller cannot act on
    is a refusal the caller routes around."""
    parts = _parts(path)
    if not parts:
        return None
    if parts[-len(LAW_TAIL):] == LAW_TAIL:
        return ("config/law.md is the Framework Law: operator-owned, seeded"
                " once at install and read by every cousin on the host")
    if parts[-1] == PORTRAIT_NAME:
        return ("self-portrait.md is the committed portrait: authored by the"
                " operator, and a candidate in .self-portrait-candidate.md"
                " is the writable side of it")
    if SHARED_DIR not in parts[:-1]:
        return None
    idx = max(i for i, p in enumerate(parts[:-1]) if p == SHARED_DIR)
    tail = parts[idx + 1:]
    if not tail:
        return None
    if tail[0] in SHARED_SUBDIRS:
        return None                     # proposed/ is the entry path; examples/ is furniture
    if len(tail) != 1 or not tail[0].endswith(".md"):
        return None
    return ("shared/%s is canonical shared memory: a cousin proposes to"
            " shared/proposed/ and a configured reviewer promotes it"
            % tail[0])


def assert_writable(path, *, writer=None):
    """Raise PerimeterRefused for a protected path; return the path
    otherwise, so a writer can check and use in one line. `writer` names
    the calling writer in the message, which is the difference between a
    refusal that is fixed and one that is worked around."""
    reason = protected_reason(path)
    if reason is None:
        return path
    where = "%s: " % writer if writer else ""
    raise PerimeterRefused("%scan't write %s: %s. Write memory/ instead, and"
                           " ask the operator for a law or portrait change."
                           % (where, path, reason))


def _tokens(command):
    """Path-like tokens in a shell command, best-effort: `shlex` split,
    with the punctuation a command wraps its arguments in stripped. A
    token that is not a path (a flag, a string literal with no slash)
    simply never matches a protected shape."""
    try:
        raw = shlex.split(str(command or ""), comments=False, posix=True)
    except ValueError:
        raw = str(command or "").split()
    out = []
    for token in raw:
        cleaned = token.strip("\"'`|&;<>(){}[]*?!$,=:\\")
        cleaned = cleaned.lstrip("-")
        if cleaned:
            out.append(cleaned)
    return out


def tool_paths(tool_name, tool_input):
    """The paths one tool call names. Edit, Write, NotebookEdit and every
    other path tool carry an exact field, so their entry is the path and
    not a guess. Bash carries a command, so its entries are path-like
    tokens: path matching on the command text, best-effort by
    construction, which is why the module docstring calls it that."""
    name = str(tool_name or "")
    if not isinstance(tool_input, dict):
        return []
    if name == "Bash":
        return _tokens(tool_input.get(BASH_FIELD))
    for field in PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    return []


def check_tool(tool_name, tool_input):
    """The refusal for a tool call that would write a protected surface,
    or None. One reason per call: the first path, not every path, because
    the caller turns this into a deny message and a list of them reads
    like a policy document."""
    for path in tool_paths(tool_name, tool_input):
        reason = protected_reason(path)
        if reason is not None:
            return ("memory perimeter: %s may not write %s: %s"
                    % (tool_name or "this tool", path, reason))
    return None