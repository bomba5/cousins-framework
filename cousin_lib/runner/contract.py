"""The framework contract (spec, "The prompt is composed, not copied").

What a cousin must know about the framework, GENERATED from the one tool
registry and the release, instead of copied into every home's identity
file. It sits in the byte-stable system prompt (phase 0 finding 3: one
changed byte re-creates the whole appended block), so it is a pure
function of (registry, MAJOR.MINOR): no clock, no path, no slug, no
count of anything that grows. A PATCH release never changes a schema,
so it changes a byte here only when it edits the static text below."""
import re
import textwrap

from cousin_lib.mcp_server import SERVER_NAME

_VERSION = re.compile(r"^(\d+)\.(\d+)")

HEADER = """# Framework contract (generated for framework {version})

You are a cousin: a persistent agent whose agent loop the framework
runs. This section is generated from the framework's tool registry and
changes only with a release or a registry edit. Your authored identity
follows it. Where your identity text describes how the framework works
(how to reply, which command to run, how a session ends) and this
contract says otherwise, this contract is right: identity files were
written for an older framework and are not always updated with it."""

# A `[[<tools>|text]]` segment names a registry tool: it stays, byte for
# byte, when the runner serves every tool named, and goes when it does not
# (#97), so a registry that disables a tool gets no instruction to use it.
# `<tools>` is one name or several joined by `+` (_segment). A paragraph or
# bullet that lost or changed a segment is re-wrapped; with every tool
# served the text is the hand-wrapped one below, unchanged.
STATIC = """## How messages reach you

Every user message is an envelope. Its first line is
`[<thread>] <source> from <sender> at <time>`, where the thread is one of
operator:<name>, person:<name>, peer:<slug>, meeting:<id>, loop:<name>,
schedule or system. A block headed
`--- context (not the sender's words) ---` is recall from your memory,
added by the framework; it is never the sender speaking.

## How you answer

Your plain text, thinking and tool calls go to your reasoning stream,
which the operator can watch. Only the `reply` tool writes the chat
surface. With one live thread, `reply` answers it; with two (a message
folded into your running turn), name the thread.[[send| A peer is answered
with `send`, never with `reply`.]][[meeting| A meeting line is answered with the
`meeting` tool on your turn only.]]

[[memory|## Memory]]

[[memory|Memory writes default to your own conclusion (L3). What the operator
told you is operator-stated: `remember` with `level=operator` and a
`cite` saying where it was said. An unverified guess is
`level=hypothesis`. Recall arrives on its own; search when you need
more.]]

## Tools, not the terminal CLIs

You run on {runner}, not in a terminal. Your identity text, your
memories and your notes may tell you to reply with `cousin-reply`, to
message a peer with `cousin-chat send`, or to write memory with
`cousin-memory`: that is the terminal lane's habit, and it is not the
way on this lane. Here:

- A person on your chat surface is answered with the `reply` tool
  (`{reply}`), never with `cousin-reply` through Bash.
[[send|- A peer cousin is messaged with the `send` tool
  (`{send}`), never with `cousin-chat send`.]]
[[memory|- Memory (search, decide, remember, recall, obsolete, activity) goes
  through the `memory` tool (`{memory}`), never through
  `cousin-memory`.]]
[[memory|- When a person asks you to keep something (they say remember, note
  that, from now on, always or never), call the `memory` tool's
  `remember` command in that same turn, before you reply, with the chat
  message as the cite: at level `operator` when the operator said it,
  at the default level when anyone else did. A remark nobody asked you
  to keep is not a memory write.]]
[[memory|- When you are asked what you know or remember about something, run
  the `memory` tool's `search` or `recall` first, and open a memory
  file directly only where a search result points.]]
[[job+schedule+meeting|- Jobs, schedules and meetings go through the `job`, `schedule` and
  `meeting` tools.]][[job| A long shell command is the `job` tool's `run`,
  which launches it, logs its output and closes the row with its exit
  code (or a Bash call with `run_in_background`, which the hooks
  track), never `cousin-job` through Bash.]]

Read an identity line that says `cousin-reply --user <name>` as a call
to `reply`[[memory|, and one that says `cousin-memory decide` as the `memory`
tool's `decide` command]]. A `cousin-*` CLI is the fallback only: use it
when the matching tool is missing from your tools or returns an error,
never as the first choice.

## Generations

Your session ends at a rollover: on context pressure, or at the
operator's daily cadence. When a system message asks for your handoff,
call `handoff` exactly once, with `position`, `next_action` and
`status`, plus `active_threads` and `learned` when you have them. The
framework writes STATUS.md's open loops, your thread list, your new
memories and the handoff file from that one call, in that order. The
next generation starts from a digest of that state as its first
message. A new generation is not announced; the work continues."""


# One fixed sentence, never the servers themselves: a home's .mcp.json
# changes with the home, and this text must not (byte-stable prompt).
OTHER_SERVERS = """Other MCP servers, from your home's .mcp.json, may be present beside
these; their tools are named mcp__<server>__<tool>."""


_SEGMENT = re.compile(r"\[\[([a-z_+]+)\|(.*?)\]\]", re.S)
_BULLET = re.compile(r"\n(?=(?:\[\[[a-z_+]+\|)?- )")
_WRAP = 72

# The plural a list of served tools is said with (_segment).
_NOUNS = {"job": "jobs", "schedule": "schedules", "meeting": "meetings"}


def _and(words):
    return words[0] if len(words) == 1 else "%s and %s" % (", ".join(words[:-1]), words[-1])


def _segment(names, text, served):
    """(text, touched) for one segment: the text when every named tool is
    served; else, for several tools, the sentence again for the served
    ones ("Schedules and meetings go through ..."), or nothing."""
    if all(n in served for n in names):
        return text, False
    if len(names) == 1:
        return "", True
    kept = [n for n in names if n in served]
    if not kept:
        return "", True
    nouns = _and([_NOUNS.get(n, n) for n in kept])
    lead = "- " if text.startswith("- ") else ""
    return "%s%s go through the %s %s." % (
        lead, nouns[0].upper() + nouns[1:], _and(["`%s`" % n for n in kept]),
        "tool" if len(kept) == 1 else "tools"), True


def _unit(text, served):
    """One paragraph or bullet with its segments resolved: verbatim when
    no segment changed, re-wrapped when one did, "" when nothing is left."""
    touched = False

    def sub(m):
        nonlocal touched
        out, changed = _segment(m.group(1).split("+"), m.group(2), served)
        touched = touched or changed
        return out

    text = _SEGMENT.sub(sub, text)
    if not touched:
        return text
    flat = " ".join(text.split())
    if not flat:
        return ""
    indent = "  " if flat.startswith("- ") else ""
    return textwrap.fill(flat, width=_WRAP, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False)


def doctrine(served, **names):
    """STATIC for the tools the runner serves (`served`: their registry
    names), formatted with `names` (runner, reply, send, memory)."""
    paragraphs = []
    for paragraph in STATIC.format(**names).split("\n\n"):
        units = [u for u in (_unit(u, served) for u in _BULLET.split(paragraph)) if u]
        if units:
            paragraphs.append("\n".join(units))
    return "\n\n".join(paragraphs)


def major_minor(version):
    m = _VERSION.match(str(version or ""))
    return "%s.%s" % m.groups() if m else "unknown"


def sdk_tool_name(name):
    """The SDK lane's name for a framework tool: its in-process server is
    registered as SERVER_NAME, so the model sees `mcp__cousin__<name>`."""
    return "mcp__%s__%s" % (SERVER_NAME, name)


def _tool_block(definition, registry, tool_name):
    name = definition["name"]
    lines = ["- `%s`: %s" % (tool_name(name), " ".join(definition["description"].split()))]
    tool = registry["tools"].get(name)
    if tool is not None and tool.get("commands"):
        commands = set(tool["commands"])
        if tool.get("kind") == "job":
            commands |= {"status", "result"}
        lines.append("  Commands: %s." % ", ".join(sorted(commands)))
    return "\n".join(lines)


# The tmux kind's name in "Tools, not the terminal CLIs" (phase 11 I8, R10).
PANE_RUNNER = "an interactive Claude Code pane"


def render(registry, version, *, tool_name=None, runner=None, other_servers=OTHER_SERVERS):
    """The contract for this registry at this release. Same inputs, same bytes.
    Its prose names only the tools tool_definitions(registry) serves (#97).

    `tool_name(name) -> str` is the name the model sees for a tool on its
    lane (phase 9 R7): None is the SDK lane's (sdk_tool_name), whose bytes
    the prompt cache keys on; the opencode lane passes `cousin_<name>`. It
    names the tools in "Tools, not the terminal CLIs" too. `runner` is that
    section's name for the lane ("You run on <runner>"): None is "the SDK
    runner"; the opencode lane passes its own.

    `other_servers` is the paragraph on the home's other MCP servers, whose
    tools the SDK lane names `mcp__<server>__<tool>`. A lane that loads no
    other server (opencode: exactly one) passes None.
    """
    from cousin_lib.runner.tools import tool_definitions
    tool_name = tool_name or sdk_tool_name
    definitions = tool_definitions(registry)
    named = {n: tool_name(n) for n in ("reply", "send", "memory")}
    static = doctrine({d["name"] for d in definitions},
                      runner=runner or "the SDK runner", **named)
    blocks = [_tool_block(d, registry, tool_name) for d in definitions]
    text = "\n\n".join([HEADER.format(version=major_minor(version)), static,
                        "## Your tools\n\n" + "\n".join(blocks)]
                       + ([other_servers] if other_servers else []))
    return text.rstrip("\n") + "\n"
