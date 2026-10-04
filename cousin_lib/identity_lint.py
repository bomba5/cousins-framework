"""cousin-doctor identity: a cousin's identity text against the facts the
framework owns.

Identity text drifts. A cousin moves to a runner lane, changes account or
gains a peer, and the lines it wrote about itself stay as they were; it
then acts on whichever line it read last. This check reads the identity
files and reports each line that contradicts a fact the framework holds,
with the fact and where it comes from. It only reads.

The identity files:

- CLAUDE.md, the authored part only: the part runner.prompt puts in the
  system prompt (the title line, the Identity and Voice sections, and
  everything below the template marker), less any paragraph that is
  word for word the current template's. The template's own framework
  sections are framework text, not identity, and are never read here.
- self-portrait.md (self_portrait.committed_path).
- STATUS.md, its live open-loops section only (status_sections): the
  active state the next generation starts from, not the history kept
  under other headings.

The rules, each with its source of truth:

- lane: `[agent] runner` in cousin.toml (delivery's runner kind) and the
  cousin's MCP registry (runner.tools.resolve_registry). On a lane that
  runs a model, a `cousin-*` CLI that a served tool replaces is the
  terminal lane's habit (runner.contract says so at every boot):
  `cousin-reply` is the reply tool, and every other mapping is read from
  the registry itself (each enabled tool command's CLI and its literal
  subcommand: `cousin-chat send` is `send`, `cousin-memory decide` is
  `memory`'s decide). A CLI whose tool the registry does not serve is the
  legitimate fallback and is not reported.
- billing: the cousin's account (accounts.for_cousin: cousin.toml
  `[agent] account` or `api_key_file`, config/accounts.toml). A short
  list of phrases that claim a subscription, no key, or a key, each
  checked against the account's kind.
- peer: a line saying a registered cousin cannot reach or message this
  one, when that cousin's registry serves `send`, both are in each
  other's peer list (chat.list_peers) and this cousin is on a runner lane
  that takes delivery.
- tool: an `mcp__cousin__<name>` the cousin's registry does not serve
  (runner.tools.tool_definitions).

Precision over recall: a line whose clause negates or dates the phrase
(not, never, no longer, used to, was, replied, logged ...), names a
fallback or a condition, or makes claims both ways, is not reported; nor
is a line about a command's syntax (its --help, arguments, flags, usage,
a CLI's shape), which describes the CLI rather than tells the cousin to run
it; "verify" or "shape" used for its own sake is no such line. A line that
names commands to state a distinction (two of them with only, vs, not,
but ...; "reaches you via"; a command given to the operator or to peers)
is reported with a fix that keeps the distinction in tool terms, each
command under the audience the line gives it.

Each finding carries the matched phrase (`match`), and its shown line is
windowed around it, so the phrase is visible on a long line. The summary
counts findings per rule and, where one claim repeats, the distinct claims
(same rule, same matched phrase, same cousin)."""
import re
from pathlib import Path

from cousin_lib import delivery, mcp_server, self_portrait, status_sections, template_sync
from cousin_lib.runner import contract, prompt

# The runner kinds that run a model, to which the framework contract's
# "Tools, not the terminal CLIs" applies (the fake runner runs none).
MODEL_LANES = tuple(k for k in delivery.RUNNER_KINDS if k != "fake")

RULES = ("lane", "billing", "peer", "tool")

# What an account kind is billed as. claude-token is a subscription's
# OAuth token; the opencode lane never carries subscription traffic
# (accounts.py refuses it), and its own provider keys are its business.
SUBSCRIPTION_KINDS = ("claude-login", "claude-token")
KEY_KIND = "anthropic-key"
OPENCODE_KIND = "opencode"

_MAX_SHOWN = 200

# A clause that negates, dates or conditions a phrase does not assert it.
_HEDGE = re.compile(
    r"\b(?:not|never|no longer|no more|don't|do not|doesn't|does not|instead of|rather than"
    r"|used to|formerly|previously|was|were|old|legacy|retired|avoid|stopped"
    r"|replied|logged|sent|ran|wrote|posted|filed)\b", re.I)
_FALLBACK = re.compile(
    r"\bfall(?:s|ing)?[ -]?back\b|\bfallback\b"
    r"|\b(?:fails?|failed|errors?|missing|unavailable|down|broken)\b", re.I)
# A line about how a command is called describes the CLI, it is no habit.
# "shape" is an ordinary word, so it counts only as the shape of a command
# ("a CLI's shape", "the shape of `cousin-chat send`"); "verify" never
# counts alone: a line that verifies a command's syntax names --help, its
# arguments, its flags or its shape, and one that verifies anything else
# may still tell the cousin to run a command.
_USAGE = re.compile(
    r"--help\b|\b(?:arguments?|argv|flags?|syntax|usage)\b"
    r"|\b(?:cli|command|call|invocation)(?:'s|\u2019s)?\s+shapes?\b"
    r"|\bshapes?\s+of\s+(?:(?:a|an|the|its|each|every)\s+)?(?:`?cousin-[\w-]+|cli|command|call)",
    re.I)
# Two commands set against each other, or the channel something arrives on:
# the line states a distinction the tools state too.
_CONTRAST = re.compile(r"\b(?:only|vs|versus|not|instead|while|whereas|but)\b", re.I)
_REACH_VIA = re.compile(r"\breach(?:es)?\s+(?:[\w'-]+\s+){1,2}?(?:via|through)\b", re.I)
# A command assigned to an audience: "`X` = operator", "X for peers only".
_AUDIENCE = re.compile(r"(?:\bfor\s+(?:the\s+|my\s+|your\s+)?|=\s*|\bonly\b.{0,40}?)"
                       r"\b(?:operators?|peers?)\b|\b(?:operators?|peers?)\s+only\b", re.I)
_AUDIENCE_WORD = re.compile(r"\b(operator|peer)s?\b", re.I)
_CONDITION = re.compile(r"\b(?:when|while|if|unless|until|during|without|before|after)\b", re.I)
_BOUNDARY = re.compile(r"[.;!?](?:\s|$)")

_DET = r"(?:(?:a|an|the|my|our|its|his|her|their|your|\w+'s)\s+){0,2}"
_ON = r"\b(?:on|via|through|under|billed to|paid by|paid for by)\s+" + _DET
# (pattern, what the line claims, the account kinds it contradicts, side)
BILLING_CLAIMS = (
    (re.compile(_ON + r"(?:(?:personal|private|claude|max|pro|own)\s+)*subscription\b", re.I),
     "it runs on a subscription", (KEY_KIND, OPENCODE_KIND), "subscription"),
    (re.compile(r"\bsubscription[ -](?:login|billing)\b", re.I),
     "it runs on a subscription", (KEY_KIND, OPENCODE_KIND), "subscription"),
    (re.compile(r"\bno\s+(?:(?:company|business|work|anthropic|api)\s+)+key\b", re.I),
     "it uses no API key", (KEY_KIND,), "subscription"),
    (re.compile(_ON + r"(?:(?:company|business|work|anthropic|api|paid|metered)\s+)+(?:api\s+)?key\b",
                re.I),
     "it runs on an API key", SUBSCRIPTION_KINDS, "key"),
    (re.compile(r"\bapi[ -]key billing\b|\bbilled per token\b", re.I),
     "it is billed to an API key", SUBSCRIPTION_KINDS, "key"),
)

_NEG = (r"(?:cannot|can't|can\u2019t|can not|could not|couldn't|couldn\u2019t"
        r"|is unable to|was unable to|has no way to|had no way to)")
_VERB = r"(?:reach|message|contact|write to|send to|send messages to|talk to|ping|dm)"
_TOOL_REF = re.compile(r"\bmcp__%s__([A-Za-z0-9_]+)" % re.escape(contract.SERVER_NAME))
_CLI_TOKEN = re.compile(r"[A-Za-z0-9][\w-]*")


# ------------------------------------------------------------ the identity text

def _blank_comments(text):
    """The text with each HTML comment removed and its newlines kept, so
    line numbers stay those of the file."""
    return prompt._COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _runs(numbered):
    """Paragraphs (runs of non-blank lines) of [(lineno, line)]."""
    run = []
    for lineno, line in numbered:
        if line.strip():
            run.append((lineno, line))
        elif run:
            yield run
            run = []
    if run:
        yield run


def _authored(runs, template):
    out = []
    for run in runs:
        if prompt._norm(" ".join(line for _n, line in run)) not in template:
            out.extend(run)
    return out


def claude_lines(text, template):
    """[(lineno, line)] of the authored part of a CLAUDE.md: what
    prompt._claude_identity keeps (the title line, the Identity and Voice
    sections above the marker, everything below it), less the paragraphs
    in `template` (normalised template paragraphs)."""
    lines = _blank_comments(text).split("\n")
    numbered = list(enumerate(lines, 1))
    out = []
    if lines and prompt._TITLE.match(lines[0].strip()):
        out.append((1, lines[0]))
    marker = next((i for i, line in enumerate(lines) if template_sync.MARKER in line), None)
    head = numbered if marker is None else numbered[:marker]
    title, body = None, []
    for lineno, line in head + [(None, "## ")]:
        if line.startswith("## "):
            if title in prompt._KEEP_HEADINGS:
                out.extend(_authored(_runs(body), template))
            title, body = line.strip(), []
        elif title is not None:
            body.append((lineno, line))
    if marker is not None:
        out.extend(_authored(_runs(numbered[marker + 1:]), template))
    return out


def _plain_lines(text):
    return [(n, line) for n, line in enumerate(_blank_comments(text).split("\n"), 1)
            if line.strip()]


def status_lines(text):
    """[(lineno, line)] of STATUS.md's live open-loops body
    (status_sections, the part the next generation starts from); the
    history the cousin keeps under other headings is not read."""
    span = status_sections._live_body_span(text)
    if span is None:
        return []
    first = text.count("\n", 0, span[0]) + 1
    body = _blank_comments(text[span[0]:span[1]])
    return [(first + i, line) for i, line in enumerate(body.split("\n")) if line.strip()]


def _read(path):
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return None


def identity_files(home, root):
    """[(path, [(lineno, line)])] for the identity files that exist."""
    home = Path(home)
    out = []
    text = _read(home / "CLAUDE.md")
    if text is not None:
        out.append((home / "CLAUDE.md",
                    claude_lines(text, prompt._template_paragraphs(root, home))))
    text = _read(self_portrait.committed_path(home))
    if text is not None:
        out.append((Path(self_portrait.committed_path(home)), _plain_lines(text)))
    text = _read(home / "STATUS.md")
    if text is not None:
        out.append((home / "STATUS.md", status_lines(text)))
    return out


# ------------------------------------------------------------ the facts

class Facts:
    """What the framework holds for one cousin, each read from its source.
    A fact that cannot be read is None, and its rules are skipped with a
    note saying why."""

    def __init__(self, home, root):
        from cousin_lib.config import CousinConfig
        self.home, self.root = Path(home), Path(root)
        self.slug, self.notes = self.home.name, []
        try:
            cfg = CousinConfig.load(self.home)
            self.slug, self.name, self.peer_visible = cfg.slug, cfg.name, cfg.peer_visible
        except Exception as err:  # noqa: BLE001 - a broken cousin.toml costs its own checks
            self.name, self.peer_visible = self.slug, None
            self.notes.append("%s: cousin.toml does not load (%s); only the tool rule"
                              " is checked" % (self.slug, err))
        self.lane = delivery._runner_kind(self.home)
        self._registry = self._account = False

    @property
    def registry(self):
        """(registry, its source) or None."""
        if self._registry is False:
            from cousin_lib.runner import tools
            try:
                registry, notice = tools.resolve_registry(self.home, self.root)
                path = mcp_server.default_registry_path(
                    {"FRAMEWORK_ROOT": str(self.root), "COUSIN_HOME": str(self.home)})
                source = "the shipped default registry" if notice else _rel(path, self.root)
                self._registry = (registry, source)
            except Exception as err:  # noqa: BLE001
                self._registry = None
                self.notes.append("%s: the MCP registry does not load (%s); the lane, peer"
                                  " and tool rules are skipped" % (self.slug, err))
        return self._registry

    @property
    def served(self):
        """The names the cousin MCP server serves (registry tools, reply, handoff)."""
        from cousin_lib.runner import tools
        reg = self.registry
        return None if reg is None else {d["name"] for d in tools.tool_definitions(reg[0])}

    def tool_name(self, name):
        """The tool's name on this cousin's lane."""
        if self.lane == "opencode":
            from cousin_lib.runner import opencode
            return opencode.tool_name(name)
        return contract.sdk_tool_name(name)

    @property
    def account(self):
        if self._account is False:
            from cousin_lib import accounts
            try:
                self._account = accounts.for_cousin(self.home, self.root)
            except Exception as err:  # noqa: BLE001
                self._account = None
                self.notes.append("%s: the account does not load (%s); the billing rule is"
                                  " skipped" % (self.slug, err))
        return self._account


def _rel(path, root):
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def _literal_prefix(argv):
    out = []
    for element in argv:
        if mcp_server._placeholder(element) is not None or element.startswith("-"):
            break
        out.append(element)
    return tuple(out)


def terminal_clis(registry):
    """{cli: [(subcommand words, tool, command or None)]}: each `cousin-*`
    CLI a served tool replaces, read from the registry's enabled tools.
    A command-kind tool needs a literal subcommand (a bare CLI may do more
    than the tool); `cousin-reply` is the reply tool, served on every
    lane (runner.tools.RUNNER_TOOLS), whatever the registry says."""
    out = {}
    for name, tool in registry["tools"].items():
        if not tool.get("enabled", True):
            continue
        is_send = tool.get("kind") == "send"
        for cmd_name, cmd in tool["commands"].items():
            cli = Path(cmd["command"][0]).name if cmd.get("command") else ""
            words = _literal_prefix(cmd.get("argv") or [])
            if not cli.startswith("cousin-") or cli == "cousin-reply" or (not is_send and not words):
                continue
            out.setdefault(cli, []).append((words, name, None if is_send else cmd_name))
    out["cousin-reply"] = [((), "reply", None)]
    return out


# ------------------------------------------------------------ the rules

def _clause_before(line, start):
    """The text of `line` from the last sentence boundary before `start`."""
    cut = 0
    for m in _BOUNDARY.finditer(line, 0, start):
        cut = m.end()
    return line[cut:start]


def _clause_after(line, end):
    m = _BOUNDARY.search(line, end)
    return line[end:m.start() if m else len(line)]


def _hedged(line, start):
    # This reads the physical line, while the skip tests read the logical
    # line (_logical_lines). Feeding it the logical unit, or the unit's
    # text back to the last sentence punctuation before the command, looks
    # like the consistent change and is wrong: a wrapped line's earlier
    # physical lines usually hold another clause ("X is not done: any
    # reply goes to cousin-reply"), cut off by a colon or a dash rather
    # than a full stop, and a hedge there does not hedge the command. On
    # the live identity files every finding the wider scope would silence
    # was of that kind. The cost of the narrow scope is a hedge split from
    # its command by the wrap ("never answer through" / "cousin-reply"),
    # which is reported; rewrap that line.
    return bool(_HEDGE.search(_clause_before(line, start)))


def _snippet(line, span):
    """The stripped line, cut to _MAX_SHOWN around `span` (the match) with
    an ellipsis on each side cut, so the match is always shown."""
    text = line.strip()
    if len(text) <= _MAX_SHOWN:
        return text
    lead = len(line) - len(line.lstrip())
    start = min(max(span[0] - lead, 0), len(text))
    end = min(max(span[1] - lead, start), len(text))
    if end - start > _MAX_SHOWN - 6:
        lo, hi = start, end                   # the match alone is longer than the room
    else:
        lo = max(0, start - (_MAX_SHOWN - (end - start)) // 2)
        hi = min(len(text), lo + _MAX_SHOWN)
        lo = max(0, hi - _MAX_SHOWN)
        lo += 3 if lo > 0 else 0
        hi -= 3 if hi < len(text) else 0
    return ("..." if lo > 0 else "") + text[lo:hi] + ("..." if hi < len(text) else "")


def _finding(facts, path, lineno, line, span, rule, fact, fix):
    return {"cousin": facts.slug, "file": _rel(path, facts.root), "line": lineno,
            "text": _snippet(line, span), "match": line[span[0]:span[1]], "rule": rule,
            "fact": fact, "fix": fix}


def _claim_key(item):
    """What makes two findings one claim: cousin, rule, the matched phrase
    with case, punctuation and spacing normalised."""
    return (item["cousin"], item["rule"], re.sub(r"[\W_]+", " ", item["match"].lower()).strip())


# A line that starts a new unit of text: a bullet, a numbered item or a heading.
_NEW_ITEM = re.compile(r"\s*(?:[-*+]\s|\d+[.)]\s|#)")


def _logical_lines(numbered):
    """{line number: the text of the logical line it belongs to}. A line
    that ends without sentence punctuation is continued by the next when
    that one follows it directly, is indented, and starts no new item, as a
    wrapped bullet is. The skip tests read the whole unit, so a wrapped
    note about a command's usage is skipped on every line it spans."""
    units, group, prev = {}, [], None

    def flush():
        text = " ".join(line.strip() for _n, line in group)
        for n, _line in group:
            units[n] = text
        group.clear()

    for n, line in numbered:
        continues = (group and prev is not None and n == prev[0] + 1
                     and not prev[1].rstrip().endswith((".", "!", "?", ":"))
                     and line[:1].isspace() and not _NEW_ITEM.match(line))
        if group and not continues:
            flush()
        group.append((n, line))
        prev = (n, line)
    if group:
        flush()
    return units


def _audiences(line, mentions):
    """{mention start: [audience]}: each audience the line gives a command
    to (_AUDIENCE: "for the operator", "= peer messages", "peers only"),
    as "operator" or "peers", given to the mention nearest the audience
    word in its sentence."""
    out = {}
    for m in _AUDIENCE.finditer(line):
        word = _AUDIENCE_WORD.search(line, m.start(), m.end())
        if word is None:
            continue
        label = "operator" if word.group(1).lower() == "operator" else "peers"
        near = []
        for start, end, *_rest in mentions:
            lo, hi = (end, word.start()) if end <= word.start() else (word.end(), start)
            if not _BOUNDARY.search(line[lo:hi]):
                near.append((abs(hi - lo), start))
        if near:
            labels = out.setdefault(min(near)[1], [])
            if label not in labels:
                labels.append(label)
    return out


def _distinction_fix(line, mentions):
    """The fix for a line that states a distinction between commands: each
    command in tool terms, under the audience the line gives it, if any."""
    audiences = _audiences(line, mentions)
    parts = [("%s: %s" % (" and ".join(audiences[start]), use)) if start in audiences
             else "`%s` -> %s" % (said, use)
             for start, _end, said, _tool, use, _hedged in mentions]
    return "rewrite the distinction in tool terms: %s" % ("; " if audiences else ", ").join(parts)


def lane_findings(facts, path, numbered):
    if facts.lane not in MODEL_LANES or facts.registry is None:
        return []
    clis = terminal_clis(facts.registry[0])
    pattern = re.compile(r"(?<![\w-])(%s)(?![\w-])"
                         % "|".join(re.escape(c) for c in sorted(clis, key=len, reverse=True)))
    out = []
    units = _logical_lines(numbered)
    for lineno, line in numbered:
        unit = units.get(lineno, line)
        if _FALLBACK.search(unit) or _USAGE.search(unit):
            continue
        mentions = {}                         # every command the line names, hedged or not
        for m in pattern.finditer(line):
            if line[m.end():m.end() + 1] == "." and line[m.end() + 1:m.end() + 2].isalnum():
                continue                      # a file name (cousin-reply.py), not a command
            tokens = list(_CLI_TOKEN.finditer(line, m.end(), m.end() + 80))[:4]
            words = tuple(t.group(0).lower() for t in tokens)
            best = None
            for want, tool, command in clis[m.group(1)]:
                if words[:len(want)] == want and (best is None or len(want) > len(best[0])):
                    best = (want, tool, command)
            if best is None:
                continue
            want, tool, command = best
            said = " ".join((m.group(1),) + want)
            hedged = _hedged(line, m.start())
            if said in mentions and (hedged or not mentions[said][5]):
                continue                      # said already, unhedged the first time
            named = facts.tool_name(tool)
            use = ("the `%s` tool's `%s` command" % (named, command) if command
                   else "the `%s` tool" % named)
            end = tokens[len(want) - 1].end() if want else m.end()
            mentions[said] = (m.start(), end, said, tool, use, hedged)
        mentions = sorted(mentions.values())
        if not mentions:
            continue
        contrast = ((len(mentions) > 1 and _CONTRAST.search(line)) or _REACH_VIA.search(line)
                    or _AUDIENCE.search(line))
        for start, end, said, tool, use, hedged in mentions:
            if hedged:
                continue
            where = ("every runner lane serves it" if tool == "reply"
                     else "from %s" % facts.registry[1])
            fix = (_distinction_fix(line, mentions) if contrast
                   else "use %s instead of `%s`" % (use, said))
            out.append(_finding(
                facts, path, lineno, line, (start, end), "lane",
                "%s runs on the %s runner (cousin.toml [agent] runner), where `%s` is the"
                " terminal lane's habit: %s replaces it (%s)" % (
                    facts.slug, facts.lane, said, use, where),
                fix))
    return out


def _billing_of(account):
    if account.kind in SUBSCRIPTION_KINDS:
        return "a subscription login"
    if account.kind == KEY_KIND:
        return "an API key"
    return "the opencode lane, which never carries subscription traffic"


def billing_findings(facts, path, numbered):
    account = facts.account
    if account is None:
        return []
    out = []
    for lineno, line in numbered:
        hits = []
        for pattern, claim, contradicts, side in BILLING_CLAIMS:
            for m in pattern.finditer(line):
                if not _hedged(line, m.start()):
                    hits.append((m, claim, contradicts, side))
        if len({h[3] for h in hits}) > 1:
            continue                          # claims both ways: a change or a comparison
        for m, claim, contradicts, _side in hits[:1]:
            if account.kind not in contradicts:
                continue
            source = ("cousin.toml [agent] api_key_file" if account.implicit and account.kind == KEY_KIND
                      else "the host login (no [agent] account)" if account.implicit
                      else "cousin.toml [agent] account, config/accounts.toml")
            out.append(_finding(
                facts, path, lineno, line, m.span(), "billing",
                "the line says %s (\"%s\"); %s runs on account %r, kind %s: %s (%s)" % (
                    claim, m.group(0), facts.slug, account.name, account.kind,
                    _billing_of(account), source),
                "reword the line to say it runs on %s, or remove it" % _billing_of(account)))
    return out


def _can_message(sender, target, fw):
    """True when `sender` can message `target`, False when it cannot,
    None when a fact is missing."""
    from cousin_lib import chat
    served = sender.served
    if served is None or target.peer_visible is None:
        return None
    if "send" not in served or target.lane not in delivery.RUNNER_KINDS:
        return False
    try:
        return target.slug in {c.slug for c in chat.list_peers(fw, sender.slug)}
    except Exception:  # noqa: BLE001 - a broken cousin.toml elsewhere: unknown
        return None


def peer_findings(facts, path, numbered, others, fw):
    if facts.peer_visible is None or not others:
        return []
    me = sorted({re.escape(facts.slug), re.escape(facts.name)}, key=len, reverse=True)
    obj = r"(?:me|us|myself|%s)\b" % "|".join(me)
    out = []
    for other in others:
        names = sorted({re.escape(other.slug), re.escape(other.name)}, key=len, reverse=True)
        # the words between the name and the "cannot" are group 1: a hedge
        # there ("Wren never said it cannot ...") is no claim
        pattern = re.compile(r"\b(?:%s)\b(?:'s)?((?:\s+[\w'\u2019-]+){0,3}?)\s+%s\s+(?:\w+\s+)?%s\s+%s"
                             % ("|".join(names), _NEG, _VERB, obj), re.I)
        for lineno, line in numbered:
            m = pattern.search(line)
            if m is None or _hedged(line, m.start()) or _HEDGE.search(m.group(1)) \
                    or _CONDITION.search(_clause_after(line, m.end())):
                continue
            if _can_message(other, facts, fw) is not True:
                continue
            out.append(_finding(
                facts, path, lineno, line, m.span(), "peer",
                "%s can message %s: %s's registry (%s) serves `send`, both are in each other's"
                " peer list (cousin.toml [cousin] peer_visible) and %s takes delivery on the %s"
                " runner" % (other.slug, facts.slug, other.slug, other.registry[1], facts.slug,
                             facts.lane),
                "remove the line, or say what actually stops %s" % other.slug))
    return out


def tool_findings(facts, path, numbered):
    served = facts.served
    if served is None:
        return []
    out = []
    for lineno, line in numbered:
        for m in _TOOL_REF.finditer(line):
            if m.group(1) in served or _hedged(line, m.start()):
                continue
            out.append(_finding(
                facts, path, lineno, line, m.span(), "tool",
                "%s's cousin server serves no `%s`: its tools are %s (%s)" % (
                    facts.slug, m.group(1), ", ".join(sorted(served)), facts.registry[1]),
                "name a tool it serves, or remove the line"))
    return out


# ------------------------------------------------------------ the check

def lint_home(facts, others, fw):
    """Every finding for one cousin, in file and line order."""
    out = []
    for path, numbered in identity_files(facts.home, facts.root):
        found = (lane_findings(facts, path, numbered) + billing_findings(facts, path, numbered)
                 + peer_findings(facts, path, numbered, others, fw)
                 + tool_findings(facts, path, numbered))
        out += sorted(found, key=lambda f: (f["line"], RULES.index(f["rule"])))
    return out


def check_identity(root, homes, cousin=None):
    """The doctor result for `homes` (doctor.cousin_homes), or for the one
    named `cousin`. Peers are read from every home either way."""
    from cousin_lib.config import FrameworkConfig
    fw = FrameworkConfig(root)
    every = [Facts(home, fw.root) for home in homes]
    chosen = [f for f in every if cousin is None or f.home.name == cousin]
    items, errors = [], []
    for facts in chosen:
        others = [o for o in every if o is not facts and o.peer_visible is not None]
        try:
            items += lint_home(facts, others, fw)
        except OSError as err:
            errors.append("%s: an identity file cannot be read: %s" % (facts.slug, err))
    notes = [n for f in chosen for n in f.notes]
    cousins = sorted({i["cousin"] for i in items})
    n = len(chosen)
    if not chosen:
        summary = "no cousin homes under %s" % (Path(fw.root) / "cousins")
    elif items:
        counts = []
        for r in RULES:
            mine = [i for i in items if i["rule"] == r]
            if mine:
                distinct = len({_claim_key(i) for i in mine})
                counts.append("%s %d%s" % (r, len(mine), "" if distinct == len(mine)
                                           else " (%d distinct)" % distinct))
        counts = ", ".join(counts)
        summary = "%d line%s contradict%s the framework in %d of %d cousin%s (%s)" % (
            len(items), "" if len(items) == 1 else "s", "s" if len(items) == 1 else "",
            len(cousins), n, "" if n == 1 else "s", counts)
    else:
        summary = "%d cousin%s, no identity line contradicts the framework" % (
            n, "" if n == 1 else "s")
    if errors:
        summary += "; %d could not be read" % len(errors)
    return {"check": "identity", "ok": not items and not errors, "summary": summary,
            "items": items, "fixes": [], "errors": errors, "notes": notes}


def render_items(items):
    """The findings as the lines cousin-doctor prints under the summary."""
    lines = []
    for i in items:
        lines.append("%s:%d [%s] %s" % (i["file"], i["line"], i["rule"], i["text"]))
        lines.append("  fact: %s" % i["fact"])
        lines.append("  fix:  %s" % i["fix"])
    return lines
