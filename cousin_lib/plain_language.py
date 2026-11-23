"""Plain-language check (docs/plain-language.md).

`check(text)` reads Markdown or plain text and returns findings for five
rules: an em dash, a sentence over its word limit, a banned word, a word a
glossary entry says not to use, and a passive verb (a heuristic, reported
as a note that never fails). Fenced code, inline code spans, URLs and table
separator rows are not checked; table cells are checked as text.

The install-wide config is <root>/config/plain-language.toml (absent: the
defaults). A cousin's cousin.toml `[plain_language] reply` says what its
reply and send tools do with the text: nothing ("off"), add the findings
to the tool's result ("note"), or fix what is fixable first ("fix"). The
check never blocks a reply.

The glossary reader takes two formats, so the glossary from code
(cousin_lib/glossary.py) and the framework's own docs/glossary.md read the
same way: a Markdown table with the columns Term, Meaning and Do not use,
or `### term` headings, each with a paragraph and an optional
`Do not use: a, b` line."""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

RULES = ("em-dash", "sentence-length", "banned-word", "glossary", "passive")
NOTE_RULES = ("passive",)
REPLY_MODES = ("off", "note", "fix")
CONFIG_NAME = "plain-language.toml"
DEFAULT_LIMITS = {"step": 20, "description": 25}
NOTE_MAX = 10
OUTBOUND_MAX = 64_000     # characters the reply hook checks; a longer text goes unchecked
GLOSSARY_MAX = 1_000_000  # bytes of a glossary file

# A list item that starts with one of these is a step (the `step` limit).
# A heuristic: English has more verbs than this, and some of these are nouns.
IMPERATIVE = frozenset("""
add allow answer apply ask avoid build call change check choose clear click
close commit compare confirm copy create delete describe do don't drop edit
enable disable enter export find fill fix follow give go install keep leave
let list load log look make mark merge move name note open paste pick press
print pull push put read rebuild record reload remember remove rename repeat
replace report reset restart restore review run save say see select send set
show sign skip start stop switch take tell test try turn type update upgrade
use verify wait watch write
""".split())

# Words ending in -ed or -en that are not participles. Also a heuristic.
_NOT_PARTICIPLES = frozenset("""
been even often open then when token kitchen garden listen happen need indeed
speed red bed feed seed hundred between green screen children
""".split())
_ABBREVIATIONS = frozenset(("e.g", "i.e", "etc", "vs", "cf", "approx", "no", "nr"))


class ConfigError(ValueError):
    """config/plain-language.toml (or a config mapping) that does not check."""


@dataclass(frozen=True)
class Finding:
    line: int            # 1-based
    col: int             # 1-based, in characters
    rule: str
    excerpt: str
    message: str
    fix: str | None = None   # replacement text for `length` characters at line:col
    length: int = 0
    severity: str = "error"  # "note" for a rule that never fails (passive)

    def as_dict(self):
        return asdict(self)


# ---------------------------------------------------------------- config

def _key(rule):
    return rule.replace("-", "_")


def default_config():
    return {"limits": dict(DEFAULT_LIMITS), "rules": {_key(r): True for r in RULES},
            "banned": [], "glossary": "", "reply_default": "off"}


def check_config(data):
    """The config `data` (a parsed TOML table or a JSON object) normalized
    over the defaults, or ConfigError naming the first bad key."""
    if not isinstance(data, dict):
        raise ConfigError("the config must be a table")
    out = default_config()
    unknown = sorted(set(data) - set(out))
    if unknown:
        raise ConfigError("unknown key %s" % ", ".join(unknown))
    limits = data.get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError("[limits] must be a table")
    for name, value in limits.items():
        if name not in DEFAULT_LIMITS:
            raise ConfigError("unknown key limits.%s (step, description)" % name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
            raise ConfigError("limits.%s must be a whole number from 1 to 1000" % name)
        out["limits"][name] = value
    rules = data.get("rules", {})
    if not isinstance(rules, dict):
        raise ConfigError("[rules] must be a table")
    for name, value in rules.items():
        if name not in out["rules"]:
            raise ConfigError("unknown rule rules.%s (%s)"
                              % (name, ", ".join(out["rules"])))
        if not isinstance(value, bool):
            raise ConfigError("rules.%s must be true or false" % name)
        out["rules"][name] = value
    banned = data.get("banned", [])
    if not isinstance(banned, list):
        raise ConfigError("banned must be a list of [[banned]] tables")
    for i, row in enumerate(banned, 1):
        if not isinstance(row, dict) or set(row) - {"word", "use"}:
            raise ConfigError("banned entry %d: only word and use" % i)
        word, use = row.get("word"), row.get("use", "")
        if not isinstance(word, str) or not word.strip():
            raise ConfigError("banned entry %d: word must be a non-empty string" % i)
        if not isinstance(use, str):
            raise ConfigError("banned entry %d: use must be a string" % i)
        out["banned"].append({"word": word.strip(), "use": use.strip()})
    glossary = data.get("glossary", "")
    if not isinstance(glossary, str):
        raise ConfigError("glossary must be a path string")
    out["glossary"] = glossary.strip()
    mode = data.get("reply_default", "off")
    if mode not in REPLY_MODES:
        raise ConfigError("reply_default must be one of %s" % ", ".join(REPLY_MODES))
    out["reply_default"] = mode
    return out


def config_path(root):
    return Path(root) / "config" / CONFIG_NAME


def load_config(root=None):
    """The install's config, checked; the defaults with no root or no file."""
    if root is None:
        return default_config()
    path = config_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_config()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ConfigError("%s does not parse: %s" % (path.name, err))
    return check_config(data)


def _toml_str(value):
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def render_config(cfg):
    """TOML text for a checked config: what the console writes."""
    cfg = check_config(cfg)
    lines = ["# Plain-language check: see docs/plain-language.md.",
             "reply_default = %s" % _toml_str(cfg["reply_default"])]
    if cfg["glossary"]:
        lines.append("glossary = %s" % _toml_str(cfg["glossary"]))
    lines += ["", "[limits]"]
    lines += ["%s = %d" % (k, v) for k, v in cfg["limits"].items()]
    lines += ["", "[rules]"]
    lines += ["%s = %s" % (k, "true" if v else "false") for k, v in cfg["rules"].items()]
    for row in cfg["banned"]:
        lines += ["", "[[banned]]", "word = %s" % _toml_str(row["word"])]
        if row["use"]:
            lines.append("use = %s" % _toml_str(row["use"]))
    return "\n".join(lines) + "\n"


def write_config(root, cfg):
    """Check `cfg`, render it, read the text back, then replace the file in
    one step. Returns the normalized config; ConfigError writes nothing."""
    from cousin_lib import atomic
    cfg = check_config(cfg)
    text = render_config(cfg)
    if check_config(tomllib.loads(text)) != cfg:
        raise ConfigError("the config did not read back as written")
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, text)
    return cfg


# -------------------------------------------------------------- glossary

_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_FENCE_LEAD = re.compile(r"^\s*(?:>\s?)*\s*")


def _fence(state, line):
    """(state, whether `line` is fence or code): `state` is None outside a
    fence, else the opener's (char, length). Quote markers and indentation
    (a fence in a list item or a quote) are stripped before the test; a
    fence closes only on the opener's char, at least as long, alone."""
    m = _FENCE.match(line[_FENCE_LEAD.match(line).end():])
    if state is None:
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            return (m.group(1)[0], len(m.group(1))), True
        return None, False
    if m and m.group(1)[0] == state[0] and len(m.group(1)) >= state[1] \
            and not m.group(2).strip():
        return None, True
    return state, True
_SEP_CELL = re.compile(r"^\s*:?-{1,}:?\s*$")


def _split_cells(line):
    """[(offset, cell text)] of a table row, outer pipes dropped; an
    escaped pipe (\\|) stays in its cell."""
    cells, start = [], 0
    for m in re.finditer(r"(?<!\\)\|", line):
        cells.append((start, line[start:m.start()]))
        start = m.end()
    cells.append((start, line[start:]))
    if cells and not cells[0][1].strip():
        cells = cells[1:]
    if cells and not cells[-1][1].strip():
        cells = cells[:-1]
    return cells


def _is_separator(line):
    if "-" not in line or "|" not in line:
        return False
    cells = _split_cells(line)
    return bool(cells) and all(_SEP_CELL.match(c) for _, c in cells)


def _avoid(cell):
    words = [w.strip().strip("`") for w in cell.split(",")]
    return [w for w in words if w and w != "-"]


def _cell(text):
    return text.strip().replace("\\|", "|").strip("`").strip()


def parse_glossary(text):
    """{term: {"meaning": str, "avoid": [words]}} from a glossary text in
    either format (both, when a file holds both: a table row wins)."""
    lines = text.splitlines()
    table, headed = {}, {}
    fence = None
    i = 0
    while i < len(lines):
        line = lines[i]
        fence, code = _fence(fence, line)
        if code:
            pass
        elif ("|" in line and i + 1 < len(lines)
              and _is_separator(lines[i + 1])):
            header = [c.strip().lower() for _, c in _split_cells(line)]
            if "term" in header and "do not use" in header:
                t, a = header.index("term"), header.index("do not use")
                m = header.index("meaning") if "meaning" in header else None
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    cells = [c for _, c in _split_cells(lines[i])]
                    term = _cell(cells[t]) if t < len(cells) else ""
                    if term and term != "-":
                        meaning = _cell(cells[m]) if m is not None and m < len(cells) else ""
                        table[term] = {"meaning": meaning,
                                       "avoid": _avoid(cells[a]) if a < len(cells) else []}
                    i += 1
                continue
        i += 1
    term, para = None, []

    def flush():
        if term is None:
            return
        avoid, kept = [], []
        for row in para:
            if row.strip().lower().startswith("do not use:"):
                avoid += _avoid(row.strip()[len("do not use:"):])
            else:
                kept.append(row.strip())
        headed[term] = {"meaning": " ".join(" ".join(kept).split()), "avoid": avoid}

    fence = None
    for line in lines:
        fence, code = _fence(fence, line)
        if code:
            continue
        if line.startswith("### "):
            flush()
            term, para = _cell(line[4:]), []
        elif line.startswith("#"):
            flush()
            term, para = None, []
        elif term is not None:
            para.append(line)
    flush()
    out = {k: v for k, v in headed.items() if k not in table}
    out.update(table)
    return out


def load_glossary(path):
    """parse_glossary of the file at `path`: OSError when it is not a
    regular file (never a FIFO, which would block), is over GLOSSARY_MAX
    bytes, or cannot be read."""
    import os
    import stat
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("%s: not a regular file" % path)
    if info.st_size > GLOSSARY_MAX:
        raise OSError("%s: over %d bytes" % (path, GLOSSARY_MAX))
    return parse_glossary(Path(path).read_text(encoding="utf-8"))


def configured_glossary(root, cfg, override=None):
    """The glossary the check uses: `override` (a path), else the config's
    `glossary` (relative to the root), else None. ConfigError when the file
    cannot be read."""
    raw = override or cfg.get("glossary") or ""
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() and root is not None and not override:
        path = Path(root) / path
    try:
        return load_glossary(path)
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigError("glossary %s: %s" % (raw, getattr(err, "strerror", None) or err))


# -------------------------------------------------------------- the text

_HEADING = re.compile(r"^ {0,3}#{1,6}(\s|$)")
_THEMATIC = re.compile(r"^ {0,3}([-*_])(\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|(\d{1,9})[.)])\s+(\[[ xX]\]\s+)?")
_QUOTE = re.compile(r"^ {0,3}(>\s?)+")
_CODE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
_LINK_TARGET = re.compile(r"\]\(<?[^)\s>]*>?(?:\s+\"[^\"]*\")?\)")
_URL = re.compile(r"(?:https?|ftp)://[^\s<>()\[\]`]*[^\s<>()\[\]`.,;:!?'\"]"
                  r"|www\.[^\s<>()\[\]`]*[^\s<>()\[\]`.,;:!?'\"]")
_WORD = re.compile(r"\w+(?:['’.-]\w+)*")
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]*_]*(?=\s|$)")
_DASH = re.compile(r"\u2014|(?<=[ \t])\u2013(?=[ \t])")
_PASSIVE = re.compile(r"\b(?:is|are|was|were|be|been|being)\s+(\w+)(?:\s+(\w+))?", re.I)


def _mask(line):
    """The line with code spans and URLs as one word each (underscores, so
    no rule matches inside) and link targets as "~" (no word, no space: a
    link counts as its text, and no rule reaches into the target); same
    length."""
    line = _CODE.sub(lambda m: "_" * len(m.group(0)), line)
    line = _LINK_TARGET.sub(lambda m: "]" + "~" * (len(m.group(0)) - 1), line)
    return _URL.sub(lambda m: "_" * len(m.group(0)), line)


class _Block:
    """One unit of text: a paragraph, a list item, a heading or a table
    cell, as segments (line index, column offset, text) joined by spaces."""

    def __init__(self, kind, line, col, text):
        self.kind = kind
        self.segments = []
        self.add(line, col, text)

    def add(self, line, col, text):
        self.segments.append((line, col, text))

    def joined(self):
        starts, parts, at = [], [], 0
        for line, col, text in self.segments:
            starts.append(at)
            parts.append(text)
            at += len(text) + 1
        return " ".join(parts), starts

    def where(self, starts, index):
        k = max(0, bisect.bisect_right(starts, index) - 1)
        line, col, text = self.segments[k]
        return line + 1, col + min(index - starts[k], len(text)) + 1


def _uncomment(line, inside):
    """(line, inside): each HTML comment span on the line blanked to
    spaces, and whether a comment runs on past its end."""
    out, at = [], 0
    while at < len(line):
        if inside:
            end = line.find("-->", at)
            stop = len(line) if end < 0 else end + 3
            out.append(" " * (stop - at))
            at, inside = stop, end < 0
        else:
            begin = line.find("<!--", at)
            if begin < 0:
                out.append(line[at:])
                break
            out.append(line[at:begin])
            at, inside = begin, True
    return "".join(out), inside


def _first_word(text):
    m = _WORD.search(text)
    return m.group(0).lower() if m else ""


def _blocks(text, front_matter=True):
    """The prose blocks of Markdown `text`: fenced and indented code (four
    spaces, or a tab, starting a block outside a list) are skipped, and so
    is a YAML front matter block at the top when `front_matter` (a file;
    a chat message has none, so one starting with `---` is all prose)."""
    lines = text.split("\n")
    start = 0
    if front_matter and lines and lines[0].strip() == "---":
        for k in range(1, len(lines)):
            if lines[k].strip() in ("---", "..."):
                start = k + 1
                break
    blocks, cur = [], None
    fence = None
    comment = table = in_list = False
    for n in range(start, len(lines)):
        raw = lines[n]
        fence, code = _fence(fence, raw)
        if code:
            cur = None
            continue
        if not raw.strip():
            cur, table = None, False
            continue
        indent = len(raw.expandtabs(4)) - len(raw.expandtabs(4).lstrip())
        if cur is None and not in_list and not comment and indent >= 4:
            continue        # an indented code block: it cannot interrupt a paragraph
        raw, comment = _uncomment(raw, comment)
        if not raw.strip():
            cur = None
            continue
        masked = _mask(raw)
        quote = _QUOTE.match(masked)
        if quote:
            masked = " " * quote.end() + masked[quote.end():]
            if not raw[:1].isspace():
                in_list = False     # a quote at the margin ends the list
        body = masked.strip()
        if _HEADING.match(masked):
            cur, in_list = None, False
            blocks.append(_Block("heading", n, 0, masked))
            continue
        if _THEMATIC.match(raw):
            cur, in_list = None, False
            continue
        nxt = lines[n + 1] if n + 1 < len(lines) else ""
        if "|" in body and (table or body.startswith("|") or _is_separator(nxt)):
            cur, table = None, True
            if not raw[:1].isspace():
                in_list = False     # so does a table at the margin
            if _is_separator(masked):
                continue
            for off, cell in _split_cells(masked):
                if cell.strip():
                    blocks.append(_Block("cell", n, off, cell))
            continue
        table = False
        item = _ITEM.match(masked)
        if item:
            rest = masked[item.end():]
            step = item.group(3) is not None or _first_word(rest) in IMPERATIVE
            cur, in_list = _Block("step" if step else "item", n, item.end(), rest), True
            blocks.append(cur)
            continue
        indent = len(masked) - len(masked.lstrip())
        if indent == 0 and cur is None:
            in_list = False     # a new unindented paragraph ends the list
        if cur is None:
            cur = _Block("para", n, indent, masked[indent:])
            blocks.append(cur)
        else:
            cur.add(n, indent, masked[indent:])
    return blocks


def _excerpt(text, limit=60):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _phrase(words):
    return re.compile(r"(?<![\w-])%s(?![\w-])" % r"\s+".join(re.escape(w) for w in words.split()),
                      re.I)


def _participle(word):
    """A word ending in -ed or -en that is not on the exceptions list."""
    word = (word or "").lower()
    return len(word) > 3 and word.endswith(("ed", "en")) and word not in _NOT_PARTICIPLES


def _abbreviation(text, lo, dot, after):
    """Whether the dot at `dot` ends an abbreviation (e.g, etc, ...) and
    not a sentence: the word before it is one, and the next word starts
    lower case or with a digit. Looks at a few characters only."""
    word = re.search(r"(\S+)$", text[lo:dot])
    if not word or word.group(1).lower().rstrip(".") not in _ABBREVIATIONS:
        return False
    nxt = re.match(r"\s*(\S)", text[after:after + 40])
    return bool(nxt) and (nxt.group(1).islower() or nxt.group(1).isdigit())


def _sentences(text):
    """[(start, end)] of the sentences in `text`."""
    out, start = [], 0
    for m in _SENTENCE_END.finditer(text):
        if m.group(0).startswith(".") and _abbreviation(text, max(start, m.start() - 8),
                                                        m.start(), m.end()):
            continue
        out.append((start, m.end()))
        start = m.end()
    if text[start:].strip():
        out.append((start, len(text)))
    return out


def _dashes(raw, line, col, seg):
    """The em-dash findings of one segment: a dash found in the masked
    segment, its span widened over the real spaces of the raw line. A dash
    that starts the line has no fix: " - " there would read as a list."""
    out = []
    for m in _DASH.finditer(seg):
        at = col + m.start()
        lo, hi = at, at + 1
        while lo > col and raw[lo - 1] in " \t":
            lo -= 1
        while hi < len(raw) and raw[hi] in " \t":
            hi += 1
        dash = "em dash" if m.group(0) == "\u2014" else "en dash used as a dash"
        if not seg[:m.start()].strip():
            out.append(Finding(line + 1, at + 1, "em-dash", raw[at:hi],
                               "%s at the start of a line: rewrite it (a hyphen there"
                               " reads as a list item)" % dash, severity="note"))
        else:
            out.append(Finding(line + 1, lo + 1, "em-dash", raw[lo:hi],
                               "%s: use a hyphen with spaces ( - ) or two sentences" % dash,
                               " - ", hi - lo))
    return out


def check(text, *, config=None, glossary=None, front_matter=True):
    """The findings in `text`, in line and column order. `config` is a
    checked config (load_config; None: the defaults), `glossary` a
    parse_glossary result (None: no glossary rule). `front_matter` False
    reads a leading `---` block as prose (a chat message, not a file)."""
    cfg = check_config(config) if config is not None else default_config()
    on = cfg["rules"]
    found = []
    terms = []
    if glossary and on["glossary"]:
        for term, entry in glossary.items():
            preferred = _phrase(term)
            for word in entry.get("avoid") or ():
                if word.lower() != term.lower():
                    terms.append((term, word, _phrase(word), preferred))
    banned = [(row, _phrase(row["word"])) for row in cfg["banned"]] if on["banned_word"] else []
    lines = text.split("\n")
    for block in _blocks(text, front_matter):
        joined, starts = block.joined()
        if on["em_dash"]:
            for line, col, seg in block.segments:
                found += _dashes(lines[line], line, col, seg)
        for row, rx in banned:
            for m in rx.finditer(joined):
                use = (": use \"%s\"" % row["use"]) if row["use"] else ""
                found.append(Finding(*block.where(starts, m.start()), "banned-word",
                                     m.group(0), "\"%s\" is on the banned list%s"
                                     % (m.group(0), use)))
        for term, word, rx, preferred in terms:
            inside = [(p.start(), p.end()) for p in preferred.finditer(joined)]
            for m in rx.finditer(joined):
                if any(s <= m.start() < e for s, e in inside):
                    continue
                found.append(Finding(*block.where(starts, m.start()), "glossary", m.group(0),
                                     "the glossary says \"%s\", not \"%s\"" % (term, m.group(0))))
        if on["passive"]:
            for m in _PASSIVE.finditer(joined):
                k = next((k for k in (1, 2) if _participle(m.group(k))), None)
                if k is None:
                    continue
                found.append(Finding(*block.where(starts, m.start()), "passive",
                                     joined[m.start():m.end(k)],
                                     "maybe passive: say who does it", severity="note"))
        if on["sentence_length"] and block.kind != "heading":
            kind = "step" if block.kind == "step" else "description"
            limit = cfg["limits"][kind]
            for s, e in _sentences(joined):
                count = len(_WORD.findall(joined[s:e]))
                if count > limit:
                    lead = s + len(joined[s:e]) - len(joined[s:e].lstrip())
                    found.append(Finding(*block.where(starts, lead), "sentence-length",
                                         _excerpt(joined[lead:e]),
                                         "%d words in one sentence; the %s limit is %d"
                                         % (count, kind, limit)))
    found.sort(key=lambda f: (f.line, f.col, RULES.index(f.rule)))
    return found


def apply_fixes(text, findings):
    """`text` with each finding's fix applied (those with none skipped)."""
    lines = text.split("\n")
    by_line = {}
    for f in findings:
        if f.fix is not None and f.length > 0:
            by_line.setdefault(f.line, []).append(f)
    for n, fixes in by_line.items():
        line = lines[n - 1]
        for f in sorted(fixes, key=lambda f: f.col, reverse=True):
            line = line[:f.col - 1] + f.fix + line[f.col - 1 + f.length:]
        lines[n - 1] = line
    return "\n".join(lines)


def describe(findings, *, fixed=0):
    """One block of text for a tool result: what was fixed, then each
    finding as `line:col rule message`, at most NOTE_MAX of them."""
    if not findings and not fixed:
        return ""
    head = "plain language:"
    if fixed:
        head += " fixed %d em dash%s;" % (fixed, "" if fixed == 1 else "es")
    if not findings:
        return head.rstrip(";")
    rows = ["%d:%d %s %s%s" % (f.line, f.col, f.rule, "(note) " if f.severity == "note" else "",
                               f.message) for f in findings[:NOTE_MAX]]
    if len(findings) > NOTE_MAX:
        rows.append("and %d more" % (len(findings) - NOTE_MAX))
    return "%s %d finding%s\n%s" % (head, len(findings), "" if len(findings) == 1 else "s",
                                    "\n".join(rows))


# ------------------------------------------------------- reply and send

def reply_mode(home, root=None):
    """The cousin's `[plain_language] reply`, else the install's
    `reply_default`, else "off". A bad value anywhere reads as the next."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    table = data.get("plain_language")
    mode = table.get("reply") if isinstance(table, dict) else None
    if mode in REPLY_MODES:
        return mode
    try:
        return load_config(root)["reply_default"]
    except ConfigError:
        return "off"


def outbound(home, root, text):
    """(text to send, note for the tool's result) under the cousin's reply
    mode: "off" the text as it is and no note; "note" the text as it is
    and its findings; "fix" the fixable findings fixed, the rest noted.
    Never raises: a check that fails sends the text unchanged."""
    try:
        mode = reply_mode(home, root)
        if mode == "off" or not text:
            return text, ""
        if len(text) > OUTBOUND_MAX:
            return text, ("plain language: not checked: the text is over %d characters"
                          % OUTBOUND_MAX)
        cfg = load_config(root)
        try:
            glossary, problem = configured_glossary(root, cfg), ""
        except ConfigError as err:
            glossary, problem = None, "\nplain language: no glossary check: %s" % err
        # a message has no front matter: a reply that opens with `---` is checked whole
        found = check(text, config=cfg, glossary=glossary, front_matter=False)
        fixable = [f for f in found if f.fix is not None] if mode == "fix" else []
        if fixable:
            text = apply_fixes(text, fixable)
            # the rest, where they are in the text that goes
            found = [f for f in check(text, config=cfg, glossary=glossary, front_matter=False)
                     if f.fix is None]
        return text, (describe(found, fixed=len(fixable)) + problem).strip()
    except Exception:  # noqa: BLE001 - the check never blocks a reply
        return text, ""


# ------------------------------------------------------------------ CLI

def _root(flag):
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    try:
        return FrameworkConfig.resolve(flag, cwd_fallback=True).root
    except MissingConfigError:
        return None


def lint_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-lint",
        description="check text for plain language: em dashes, long sentences, banned"
                    " words, glossary words and (a note only) passive verbs")
    parser.add_argument("files", nargs="*", metavar="FILE",
                        help="Markdown or text files; stdin when none")
    parser.add_argument("--glossary", metavar="PATH",
                        help="a glossary file (overrides the config's glossary)")
    parser.add_argument("--json", action="store_true", help="findings as a JSON list")
    parser.add_argument("--only", metavar="RULE,...",
                        help="run only these rules (%s)" % ", ".join(RULES))
    parser.add_argument("--fix", action="store_true",
                        help="print the text with the fixable findings fixed")
    parser.add_argument("--write", action="store_true",
                        help="with --fix: write the fixed text back to each file")
    parser.add_argument("--root", default=None, help="framework root (else FRAMEWORK_ROOT)")
    args = parser.parse_args(argv)

    def usage(message):
        print("cousin-lint: %s" % message, file=sys.stderr)
        return 2

    if args.write and not args.fix:
        return usage("--write goes with --fix")
    if args.write and not args.files:
        return usage("--write needs files, not stdin")
    root = _root(args.root)
    try:
        cfg = load_config(root)
        if args.only:
            names = [n.strip() for n in args.only.split(",") if n.strip()]
            bad = [n for n in names if n not in RULES]
            if bad or not names:
                return usage("unknown rule %s; known: %s"
                             % (", ".join(bad) or "(none)", ", ".join(RULES)))
            cfg["rules"] = {_key(r): r in names for r in RULES}
        glossary = configured_glossary(root, cfg, args.glossary)
    except ConfigError as err:
        return usage(err)
    inputs = []
    if not args.files:
        inputs.append(("<stdin>", sys.stdin.read()))
    for name in args.files:
        try:
            # newline="": a CRLF file is checked, fixed and written as CRLF
            with open(name, encoding="utf-8", newline="") as fh:
                inputs.append((name, fh.read()))
        except (OSError, UnicodeDecodeError) as err:
            return usage("%s: %s" % (name, getattr(err, "strerror", None) or err))
    failing, rows = False, []
    for name, text in inputs:
        found = check(text, config=cfg, glossary=glossary)
        if args.fix:
            fixed = apply_fixes(text, found)
            if args.write:
                if fixed != text:
                    from cousin_lib import atomic
                    atomic.write_text(name, fixed, newline="")
            else:
                sys.stdout.write(fixed)
            # the rest, where they are in the fixed text
            found = [f for f in check(fixed, config=cfg, glossary=glossary) if f.fix is None]
        failing = failing or any(f.severity != "note" for f in found)
        for f in found:
            rows.append(dict(path=name, **f.as_dict()))
            if not args.json:
                note = "(note) " if f.severity == "note" else ""
                print("%s:%d:%d %s %s%s" % (name, f.line, f.col, f.rule, note, f.message),
                      file=sys.stderr if args.fix and not args.write else sys.stdout)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False),
              file=sys.stderr if args.fix and not args.write else sys.stdout)
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(lint_main())
