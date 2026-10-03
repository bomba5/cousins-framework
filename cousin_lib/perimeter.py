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

Anchored on the root: the check is pure and offline (paths are
normalised, never resolved), so the Bash chokepoint can apply it to a
parsed write target cheaply. A caller that knows the framework root
(the tool gate does) passes it, and the three surfaces are then exact
places: <root>/config/law.md, <root>/shared/<name>.md and
<root>/cousins/<slug>/self-portrait.md. Anything else is the caller's
own file: another repository, and a checkout's templates/shared/ even
when the checkout is the root (the default install). Without a root, or
for a relative path with no known working directory, the check falls
back to the shapes alone, which errs toward refusing. The framework's
own writers call assert_writable without a root: they only write inside
an install.

What it does not do:

- It is not the shared tier's boundary. `shared_tier._check_reviewer` is,
  and that one is stronger: no configuration expresses it away. This
  module only stops a writer from landing a file it should not have.
- It is about WRITES. A subagent reads the law, its portrait and the
  shared rules as a matter of course, so Read, Grep and Glob name no
  target here and `cat`, `grep` and `git diff --` on a protected path
  pass. Edit, Write, MultiEdit and NotebookEdit name their path exactly
  and are not best-effort.
- It cannot see every Bash write. `check_tool` parses redirects,
  destinations and the arguments of the commands that write, so a write
  hidden behind a variable, a glob or `find -exec` is not seen, and a
  command that does not tokenise loses its redirect. That scan is
  therefore BEST-EFFORT, and an operator who wants Bash covered too
  writes `deny_bash_patterns` in policy.toml.
- It does not make the primary session unable to edit its own law or
  portrait. Operator-directed edits are legitimate and frequent; the
  rule this enforces is that a BACKGROUND pass cannot make them. So the
  tool chokepoint (`runner.hooks.gate`) applies this only to a payload
  carrying `agent_id`, and the framework's own writers call
  `assert_writable` unconditionally, because a framework writer is
  agent-initiated by definition.
"""
import os
import shlex
from pathlib import Path

LAW_TAIL = ("config", "law.md")
PORTRAIT_NAME = "self-portrait.md"
SHARED_DIR = "shared"
SHARED_SUBDIRS = ("proposed", "examples")

# Tools that WRITE the path they name. Read, Grep and Glob name paths too
# and are absent on purpose: a subagent reads the law and the shared rules
# as a matter of course, and a perimeter that refuses the read is a
# perimeter that gets switched off.
WRITE_PATH_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# The fields those tools carry their path in.
PATH_FIELDS = ("file_path", "notebook_path")
BASH_FIELD = "command"
# Shell: a redirect's target is a write; `>` `>>` `>|` are what a command
# writes through, `>&` only duplicates a descriptor.
REDIRECTS = (">", ">>", ">|", ">&")
SEGMENT_SEPARATORS = (";", "&&", "||", "|", "&")
# Commands whose LAST positional argument is the destination, so `cp
# config/law.md /tmp/x` is a read and passes while `cp /tmp/x
# shared/a.md` is a write and does not.
DESTINATION_COMMANDS = ("cp", "mv", "install", "ln")
# Commands whose every positional argument is a write target.
ARGUMENT_COMMANDS = ("tee", "rm", "unlink", "truncate", "chmod", "chown",
                     "shred", "touch")


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


_LAW_REASON = ("config/law.md is the Framework Law: operator-owned, seeded"
               " once at install and read by every cousin on the host")
_PORTRAIT_REASON = ("self-portrait.md is the committed portrait: authored by the"
                    " operator, and a candidate in .self-portrait-candidate.md"
                    " is the writable side of it")


def _shared_reason(name):
    return ("shared/%s is canonical shared memory: a cousin proposes to"
            " shared/proposed/ and a configured reviewer promotes it" % name)


def _under_root(path, root, cwd):
    """The path's parts relative to the framework `root` (an absolute
    path, or a relative one joined to `cwd`, normalised without touching
    the filesystem); None when it is not under the root, and "shape"
    when it cannot be placed (no root given, or a relative path with no
    cwd): the caller then checks by shape alone, the safe direction."""
    text = str(path or "").strip()
    if root is None or not text:
        return "shape"
    if text.startswith("~"):
        text = os.path.expanduser(text)      # the shell expands it before writing
    if not os.path.isabs(text):
        if not cwd:
            return "shape"
        text = os.path.join(str(cwd), text)
    full = os.path.normpath(text)
    base = os.path.normpath(str(root))
    if not (full == base or full.startswith(base.rstrip("/") + "/")):
        return None
    return tuple(p for p in os.path.relpath(full, base).split(os.sep) if p and p != ".")


def protected_reason(path, *, root=None, cwd=None, home=None, slug=None):
    """Why `path` is protected, or None when it is not. The reason names
    the surface and its owner, because a refusal a caller cannot act on
    is a refusal the caller routes around. With `root` (the framework
    root) the three surfaces are anchored where an install keeps them:
    <root>/config/law.md, <root>/shared/<name>.md and
    <root>/cousins/<slug>/self-portrait.md. Anything else is the
    caller's own file, a checkout's templates/shared/ included (the
    default install's checkout IS the root).

    `home` and `slug` name the cousin whose session is writing. The tool
    gate passes them for a subagent; a framework writer (assert_writable)
    never does."""
    placed = _under_root(path, root, cwd)
    if placed is None:
        return None
    if placed != "shape":
        if placed == LAW_TAIL:
            return _LAW_REASON
        if len(placed) == 3 and placed[0] == "cousins" and placed[2] == PORTRAIT_NAME:
            return _PORTRAIT_REASON
        if len(placed) == 2 and placed[0] == SHARED_DIR and placed[1].endswith(".md"):
            return _shared_reason(placed[1])
        return None
    parts = _parts(path)
    if not parts:
        return None
    if parts[-len(LAW_TAIL):] == LAW_TAIL:
        return _LAW_REASON
    if parts[-1] == PORTRAIT_NAME:
        return _PORTRAIT_REASON
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
    return _shared_reason(tail[0])


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


def _shell_tokens(command):
    """`shlex` split with punctuation as its own tokens, so a redirect is
    visible as one: `cat >> f` is three tokens, not two. A command that
    does not tokenise (an unclosed quote) falls back to whitespace, which
    loses the redirect and therefore the write - the direction a
    best-effort check should fail in, and the reason this is documented
    as best-effort rather than called sound."""
    lex = shlex.shlex(str(command or ""), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError:
        return str(command or "").split()


def _segments(tokens):
    """`tokens` split at the operators that start a new command, so
    `mv a b; rm c` is checked as the two commands it is."""
    out, current = [], []
    for token in tokens:
        if token in SEGMENT_SEPARATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def _positional(args):
    return [a for a in args if not a.startswith("-")]


def _bash_write_targets(command):
    """The paths a shell command would WRITE: redirect targets, the
    destination of cp/mv/install/ln, the arguments of tee/rm/chmod and
    friends, the files `sed -i` edits, and dd's `of=`.

    Everything else in the command line is a read and is not returned, so
    `cat config/law.md`, `grep -n rule config/law.md` and `git diff --
    config/law.md` all pass. A write the command hides behind a variable
    (`L=...; cp $L /tmp/x`) is not seen: this is path matching on the
    command text, and the docstring's Bash caveat is the whole of it."""
    targets = []
    for segment in _segments(_shell_tokens(command)):
        if not segment:
            continue
        for index, token in enumerate(segment[:-1]):
            if token in REDIRECTS and token != ">&":
                targets.append(segment[index + 1])
        name = os.path.basename(segment[0])
        args = segment[1:]
        if name in DESTINATION_COMMANDS:
            positional = _positional(args)
            if positional:
                targets.append(positional[-1])
        elif name in ARGUMENT_COMMANDS:
            targets.extend(_positional(args))
        elif name == "sed" and any(a.startswith("-i") for a in args):
            # The first positional is the script, not a file.
            targets.extend(_positional(args)[1:])
        elif name == "dd":
            targets.extend(a[3:] for a in args if a.startswith("of="))
    return [t for t in targets if t]


def write_targets(tool_name, tool_input):
    """The paths one tool call would WRITE. The path tools name them in an
    exact field, so their entry is the path and not a guess; Bash carries
    a command, so its entries are the write targets parsed out of the
    command text. Every other tool returns nothing, reads included."""
    name = str(tool_name or "")
    if not isinstance(tool_input, dict):
        return []
    if name == "Bash":
        return _bash_write_targets(tool_input.get(BASH_FIELD))
    if name not in WRITE_PATH_TOOLS:
        return []
    for field in PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    return []


def check_tool(tool_name, tool_input, *, root=None, cwd=None, home=None, slug=None):
    """The refusal for a tool call that would WRITE a protected surface,
    or None. One reason per call: the first path, not every path, because
    the caller turns this into a deny message and a list of them reads
    like a policy document. `home` and `slug` are protected_reason's."""
    for path in write_targets(tool_name, tool_input):
        reason = protected_reason(path, root=root, cwd=cwd, home=home, slug=slug)
        if reason is not None:
            return ("memory perimeter: %s may not write %s: %s"
                    % (tool_name or "this tool", path, reason))
    return None