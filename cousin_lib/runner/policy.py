"""Operator policy as code: <home>/policy.toml (spec, "Hooks in-process",
`can_use_tool`). Enforced by a PreToolUse hook with no matcher, because
under bypassPermissions the CLI never consults can_use_tool. Missing:
allow all and say so once. Malformed: fail closed at start, loudly.

The hook fires for every tool call of the session, a subagent's own
calls included (the hook input then carries `agent_id`), so a deny
reaches inside a subagent's turn as well.

`deny_bash_patterns` is matched against the `command` string of any
tool whose input carries one (Bash, PowerShell, Monitor, any other),
except the cousin's own `mcp__cousin__*` tools, whose `command` is a
registry verb (`add`, `pass`), not a command line; it
never sees a command a tool builds on the far side (an MCP server that
shells out, an editor tool's own file writes). The patterns are a
guardrail against a known command line, not a sandbox: the same effect
can be spelled another way. Deny the tool itself with `deny_tools` when
the risk is the tool, not the shape of one command.

The file lives in the home it governs, so the model can rewrite it. A
rewrite mid-session only ever TIGHTENS the live session (`tightened_by`:
entries it adds apply at the next prompt, config_watch); anything it
removes takes effect at the next start, never mid-session, so a model
cannot loosen its own rules by editing the file. An operator who needs
it immutable makes it read-only to the cousin's user or owns it."""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cousin_lib.runner.base import RunnerError

FILE = "policy.toml"
# The cousin's own tools take a registry verb in `command` ("add", "pass"),
# not a command line, so the command patterns never look at them.
OWN_TOOL_PREFIX = "mcp__cousin__"
KEYS = ("deny_tools", "deny_bash_patterns", "ask", "outbound_filter")
# The framework's own command rules, before any policy.toml (#287): a git
# hook is the repository's gate, and a cousin may not step around it. Each
# is (pattern, reason); the patterns stay JavaScript-compatible (no
# lookbehind) because the opencode plugin applies them too. A commit
# message that only mentions the flag is refused as well: write it to a
# file and use `git commit -F`.
FRAMEWORK_BASH_DENY = (
    (re.compile(r"\bgit\b[^;&|\n]*\s(commit|push)\b[^;&|\n]*\s--no-verify\b"),
     "framework: git --no-verify skips the repository's hooks, which are its gate"
     " (a message that only mentions it: use git commit -F <file>)"),
    (re.compile(r"\bgit\b[^;&|\n]*\scommit\b[^;&|\n]*\s-n\b"),
     "framework: git commit -n is --no-verify, which skips the repository's hooks"),
    (re.compile(r"\bgit\b[^;&|\n]*\s-c\s*core\.hooks[Pp]ath\s*="),
     "framework: overriding core.hooksPath on the command line steps around the"
     " repository's hooks"),
)
# Every generation ends through this tool: a policy that denies it (or
# asks for it, which is enforced as a deny) leaves a cousin that cannot
# hand over. The console refuses such an edit (handoff_blockers).
HANDOFF_TOOL = OWN_TOOL_PREFIX + "handoff"


class PolicyError(RunnerError):
    pass


def _str_list(data, key):
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError("%s: %s must be a list of strings" % (FILE, key))
    return tuple(value)


@dataclass(frozen=True)
class Policy:
    deny_tools: tuple = ()
    deny_bash_patterns: tuple = ()      # compiled regexes
    ask: tuple = ()
    outbound_filter: bool = True
    source: str = "none"

    @classmethod
    def load(cls, home):
        path = Path(home) / FILE
        if not path.exists():
            return cls()
        try:
            text = path.read_text()
        except OSError as err:
            raise PolicyError("%s: cannot read: %s" % (FILE, err))
        return cls.parse(text, source=str(path))

    @classmethod
    def parse(cls, text, source="<text>"):
        """The policy a policy.toml of this text would load: the same
        checks as load, so an edit is validated before it is written.
        `source` is what describe() names."""
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as err:
            raise PolicyError("%s: cannot read: %s" % (FILE, err))
        unknown = set(data) - set(KEYS)
        if unknown:
            raise PolicyError("%s: unknown keys %s" % (FILE, ", ".join(sorted(unknown))))
        patterns = []
        for p in _str_list(data, "deny_bash_patterns"):
            try:
                patterns.append(re.compile(p))
            except re.error as err:
                raise PolicyError("%s: deny_bash_patterns %r: %s" % (FILE, p, err))
        flag = data.get("outbound_filter", True)
        if not isinstance(flag, bool):
            raise PolicyError("%s: outbound_filter must be true or false" % FILE)
        return cls(deny_tools=_str_list(data, "deny_tools"), deny_bash_patterns=tuple(patterns),
                   ask=_str_list(data, "ask"), outbound_filter=flag, source=source)

    def tightened_by(self, other):
        """The policy a live session moves to when policy.toml changes to
        `other` mid-session: this one plus everything `other` adds (the
        union), never minus anything. A removal would let the model loosen
        its own rules by editing the file, so removals wait for the next
        start; an addition that would deny the handoff is left out (the
        console refuses such an edit too). Returns (policy, added, removed,
        skipped), each a list of "key: entry" strings."""
        pats = [p.pattern for p in self.deny_bash_patterns]
        other_pats = [p.pattern for p in other.deny_bash_patterns]
        added, removed, skipped = [], [], []
        merged = {}
        for key, mine, theirs in (("deny_tools", list(self.deny_tools), list(other.deny_tools)),
                                  ("deny_bash_patterns", pats, other_pats),
                                  ("ask", list(self.ask), list(other.ask))):
            out = list(mine)
            for entry in theirs:
                if entry in mine:
                    continue
                if key != "deny_bash_patterns" and self._named((entry,), HANDOFF_TOOL):
                    skipped.append("%s: %s" % (key, entry))
                    continue
                out.append(entry)
                added.append("%s: %s" % (key, entry))
            removed += ["%s: %s" % (key, e) for e in mine if e not in theirs]
            merged[key] = tuple(out)
        if self.outbound_filter and not other.outbound_filter:
            removed.append("outbound_filter: true")
        elif other.outbound_filter and not self.outbound_filter:
            added.append("outbound_filter: true")
        live = Policy(deny_tools=merged["deny_tools"],
                      deny_bash_patterns=tuple(re.compile(p) for p in merged["deny_bash_patterns"]),
                      ask=merged["ask"],
                      outbound_filter=self.outbound_filter or other.outbound_filter,
                      source=other.source)
        return live, added, removed, skipped

    def _named(self, names, tool):
        tool = str(tool or "")
        for n in names:
            if n == tool or (n.endswith("*") and tool.startswith(n[:-1])):
                return n
        return None

    def handoff_blockers(self):
        """[(key, entry)] for each deny_tools or ask entry that names
        HANDOFF_TOOL (exactly or as a prefix*); ask counts, because it is
        enforced as a deny. Empty when the handoff stays allowed."""
        return [(key, entry) for key, names in (("deny_tools", self.deny_tools),
                                                ("ask", self.ask))
                for entry in names if self._named((entry,), HANDOFF_TOOL)]

    def decide(self, tool_name, tool_input):
        """`("allow", "")`, `("deny", reason)` or `("ask", reason)`:
        deny_tools first, then a string `command` in the tool's input
        (any tool but the cousin's own `mcp__cousin__*`, whose `command`
        is a registry verb) against deny_bash_patterns, then ask.
        Anything not named is allowed. `tool_name` is normalised to a
        string first: a hook payload missing `tool_name` must not raise
        out of a gate and so turn into an allow."""
        tool_name = str(tool_name or "")
        hit = self._named(self.deny_tools, tool_name)
        if hit:
            return "deny", "%s: deny_tools lists %s" % (FILE, hit)
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, str) and not tool_name.startswith(OWN_TOOL_PREFIX):
            for rx, reason in FRAMEWORK_BASH_DENY:
                if rx.search(command):
                    return "deny", reason
            for rx in self.deny_bash_patterns:
                if rx.search(command):
                    return "deny", "%s: deny_bash_patterns %r matches" % (FILE, rx.pattern)
        hit = self._named(self.ask, tool_name)
        if hit:
            return "ask", "%s: ask lists %s" % (FILE, hit)
        return "allow", ""

    def describe(self):
        """One line for the stream at start."""
        if self.source == "none":
            return "no policy.toml: every tool allowed"
        return "%s: %d denied tools, %d bash patterns, %d ask, outbound_filter=%s" % (
            self.source, len(self.deny_tools), len(self.deny_bash_patterns), len(self.ask),
            self.outbound_filter)
