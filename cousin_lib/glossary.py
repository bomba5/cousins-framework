"""Glossary from code (docs/plain-language.md, "A glossary from the code").

Three questions about the words a repository uses:

- terms: which words does it use most, in its code (identifiers, split at
  snake_case, camelCase and kebab-case) and in its prose (comments, strings,
  Markdown and text files)? The domain words that need a glossary entry are
  near the top.
- groups: for each glossary entry, the preferred term and its "do not use"
  words form a synonym group. Which member do the code and the docs really
  use? An entry whose preferred term is not the one the code uses most is
  flagged.
- derive: a glossary in the table format plain_language.parse_glossary reads
  (Term, Meaning, Do not use, Uses), the dominant member of each group as the
  term.

Every word is lower case, and a plural counts as its singular (a heuristic:
"entries" is "entry", "caches" is "cache"). Words under 3 letters and
stopwords are left out of the term list. The walk skips .git, virtual
environments, node_modules, build output, binary files and files over 1 MB,
and honours .gitignore when `git ls-files` works there. Nothing here writes
but `derive --out`."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from cousin_lib.plain_language import load_glossary

MAX_BYTES = 1_000_000
MAX_FILES = 20_000
SKIP_DIRS = frozenset((".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
                       ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build",
                       "dist", "target", "out", ".eggs", "htmlcov", "coverage", ".next",
                       "site-packages", ".secrets"))
SKIP_FILES = frozenset(("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
                        "Cargo.lock", "uv.lock"))
DOC_EXTS = frozenset((".md", ".markdown", ".rst", ".txt", ".adoc", ""))
HASH_EXTS = frozenset((".py", ".sh", ".bash", ".zsh", ".toml", ".yaml", ".yml", ".rb", ".pl",
                       ".r", ".conf", ".cfg", ".ini", ".mk", ".nix", ".tf"))
SLASH_EXTS = frozenset((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".c", ".h", ".cc", ".cpp",
                        ".hpp", ".java", ".go", ".rs", ".swift", ".kt", ".scala", ".css",
                        ".scss", ".php", ".cs", ".dart", ".zig"))
HASH_NAMES = frozenset(("Makefile", "Dockerfile", "Containerfile", "Justfile"))

STOPWORDS = frozenset("""
about above after again against all also and any are aren because been before being below
between both but can cannot could couldn did didn does doesn doing don down during each else
etc few for from further get got had hasn has have haven having her here hers herself him
himself his how into isn its itself just let like more most must mustn myself nor not now off
once one only other ought our ours ourselves out over own same shan she should shouldn some
such than that the their theirs them themselves then there these they this those through too
under until upon very via was wasn were weren what when where which while who whom why will
with won would wouldn yet you your yours yourself yourselves
def return self import from class function const var true false none null nil undefined
else elif try except finally raise pass lambda async await yield with assert global
nonlocal break continue new this super public private protected static void int str
bool float dict list tuple set len range print type isinstance object args kwargs
string number boolean println printf include define endif ifdef ifndef struct enum
impl pub mut use mod crate fn func package interface extends implements export default
require module exports echo then done esac fi elif local readonly http https www com org
""".split())


def fold(word):
    """A lower-case word, a plural as its singular (a heuristic)."""
    word = word.lower()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "shes", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


_APOSTROPHE = re.compile("(?<=[A-Za-z])['" + chr(0x2019) + "][A-Za-z]+")
_RUN = re.compile(r"[A-Za-z0-9]+")
_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")


def words(text):
    """The folded words of `text`, in order: identifiers split at _, -,
    and case changes (camelCase, HTTPServer), digits dropped, a word's
    apostrophe suffix ("don't", "cousin's") dropped."""
    out = []
    for run in _RUN.findall(_APOSTROPHE.sub("", text)):
        out += [fold(p) for p in _PART.findall(run)]
    return out


_STRINGS = (r'"""[\s\S]*?"""', r"'''[\s\S]*?'''", r'"(?:\\.|[^"\\\n])*"',
            r"'(?:\\.|[^'\\\n])*'", r"`(?:\\.|[^`\\])*`")
_STYLES = {
    "hash": re.compile("|".join(_STRINGS + (r"#[^\n]*",))),
    "slash": re.compile("|".join(_STRINGS + (r"//[^\n]*", r"/\*[\s\S]*?\*/"))),
    "markup": re.compile("|".join(_STRINGS[2:4] + (r"<!--[\s\S]*?-->",))),
    "plain": re.compile("|".join(_STRINGS)),
}


def _style(path):
    ext = path.suffix.lower()
    if path.name in HASH_NAMES:
        return "hash"
    if ext in DOC_EXTS:
        return "docs"
    if ext in HASH_EXTS:
        return "hash"
    if ext in SLASH_EXTS:
        return "slash"
    if ext in (".html", ".htm", ".xml", ".svg", ".vue"):
        return "markup"
    return "plain"


def regions(path, text):
    """[(kind, text)] of a file: "docs" for Markdown and text files, and for
    a code file's comments and strings; "code" for the rest of a code file."""
    style = _style(path)
    if style == "docs":
        return [("docs", text)]
    out, at = [], 0
    for m in _STYLES[style].finditer(text):
        out.append(("code", text[at:m.start()]))
        out.append(("docs", m.group(0)))
        at = m.end()
    out.append(("code", text[at:]))
    return out


def _venv(path):
    return (path / "pyvenv.cfg").is_file()


def _skipped_dir(parts):
    return any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts)


def _listed(root):
    """The files `git ls-files` lists under `root` (tracked and untracked,
    .gitignore honoured), relative to it; None when git does not work there."""
    # The scanned repository's own config must not run anything here: no
    # fsmonitor hook, no system config. A repository git refuses (not
    # owned, safe.directory) answers non-zero and gets the plain walk.
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root), "ls-files",
                              "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    listed = [Path(p) for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
    # an empty answer is a directory git ignores (or an empty one): walk it
    return listed or None


def _walked(root):
    for top, dirs, names in os.walk(root):
        here = Path(top)
        # SKIP_DIRS spares the descent; files() filters every path either way
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            yield (here / name).relative_to(root)


def _readable(path):
    """A regular file (not a link, a FIFO or a device) of at most MAX_BYTES."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES


def files(root):
    """The text files under `root` the scan reads: (relative paths, skipped
    count, truncated). A file given as `root` is the one file, under the
    same rules as any other."""
    root = Path(root)
    if not root.is_dir():
        return ([Path(root.name)], 0, False) if _readable(root) else ([], 1, False)
    listed = _listed(root)
    candidates = listed if listed is not None else _walked(root)
    out, skipped = [], 0
    venvs = {}
    for rel in candidates:
        if _skipped_dir(rel.parts[:-1]) or rel.name in SKIP_FILES:
            continue
        parent = rel.parent
        if parent not in venvs:
            venvs[parent] = any(_venv(root / Path(*parent.parts[:i + 1]))
                                for i in range(len(parent.parts)))
        if venvs[parent]:
            continue
        if not _readable(root / rel):
            skipped += 1
            continue
        out.append(rel)
        if len(out) >= MAX_FILES:
            return out, skipped, True
    return out, skipped, False


def read_text(path):
    """The file's text, or None for a binary file (a NUL byte, not UTF-8)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass
class Corpus:
    code: Counter = field(default_factory=Counter)
    docs: Counter = field(default_factory=Counter)
    files: int = 0
    skipped: int = 0
    truncated: bool = False


def _phrase(text):
    return tuple(words(text))


def scan(root, phrases=()):
    """Count every word under `root` as code or docs, and each phrase of
    more than one word (a tuple of folded words) as a run of words."""
    root = Path(root)
    base = root if root.is_dir() else root.parent
    corpus = Corpus()
    rels, corpus.skipped, corpus.truncated = files(root)
    long = sorted({p for p in phrases if len(p) > 1})
    for rel in rels:
        text = read_text(base / rel)
        if text is None:
            corpus.skipped += 1
            continue
        corpus.files += 1
        for kind, chunk in regions(rel, text):
            seq = words(chunk)
            counts = corpus.code if kind == "code" else corpus.docs
            counts.update(seq)
            for p in long:
                n = len(p)
                hits = sum(1 for i in range(len(seq) - n + 1) if tuple(seq[i:i + n]) == p)
                if hits:
                    counts[" ".join(p)] += hits
    return corpus


def _marks(glossary):
    marks = {}
    for term, entry in (glossary or {}).items():
        for word in entry.get("avoid") or ():
            marks.setdefault(" ".join(_phrase(word)), "avoid")
        marks[" ".join(_phrase(term))] = "term"
    return marks


def terms(root, *, top=50, glossary=None, corpus=None):
    """The `top` most used terms: [{term, code, docs, total, glossary}],
    `glossary` "term" for an entry's term, "avoid" for a do-not-use word."""
    corpus = corpus or scan(root)
    marks = _marks(glossary)
    totals = Counter()
    for counts in (corpus.code, corpus.docs):
        for word, n in counts.items():
            if " " not in word and len(word) >= 3 and word.isalpha() and word not in STOPWORDS:
                totals[word] += n
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return [{"term": w, "code": corpus.code[w], "docs": corpus.docs[w], "total": n,
             "glossary": marks.get(w, "")} for w, n in ranked]


def parse_groups(values):
    """[(preferred, [others])] from "a,b,c" strings: the first is preferred."""
    out = []
    for value in values or ():
        members = [m.strip() for m in str(value).split(",") if m.strip()]
        if members:
            out.append((members[0], members[1:]))
    return out


def synonym_groups(glossary=None, groups=()):
    """[(preferred, [others], meaning)] of a glossary's entries, then the
    groups given as "a,b,c" strings."""
    out = [(term, list(entry.get("avoid") or ()), entry.get("meaning") or "")
           for term, entry in (glossary or {}).items()]
    out += [(term, others, "") for term, others in parse_groups(groups)]
    return out


def check_groups(root, groups, corpus=None):
    """For each (preferred, others, meaning): each member's uses in code
    and docs, the dominant one (most code uses, docs break a tie, the
    preferred term wins a full tie), and whether the entry is flagged
    (the dominant member is not the preferred term)."""
    phrases = {_phrase(m) for term, others, _ in groups for m in [term] + others}
    corpus = corpus or scan(root, phrases)
    out = []
    for term, others, meaning in groups:
        members = []
        seen = set()
        for word in [term] + others:
            key = " ".join(_phrase(word))
            if word is not term and (not key or key in seen):
                continue
            seen.add(key)
            members.append({"word": word, "code": corpus.code[key] if key else 0,
                            "docs": corpus.docs[key] if key else 0})
        if not _phrase(term):
            # nothing to count in the term itself: no dominant member, no flag
            out.append({"term": term, "meaning": meaning, "members": members,
                        "dominant": term, "flagged": False,
                        "message": "the term has no words to count"})
            continue
        best = max(members, key=lambda m: (m["code"], m["docs"], m is members[0]))
        row = {"term": term, "meaning": meaning, "members": members, "dominant": best["word"],
               "flagged": best is not members[0], "message": ""}
        if row["flagged"]:
            row["message"] = ("code says \"%s\" %d times, the glossary prefers \"%s\" (%d)"
                              % (best["word"], best["code"], term, members[0]["code"]))
        out.append(row)
    return out


def _cell(text):
    return " ".join(str(text).split()).replace("|", "\\|")


def derive_text(rows):
    """The glossary in the table format, from check_groups rows."""
    lines = ["# Glossary", "",
             "Derived by `cousin-glossary derive`: each term is the word the code uses most.",
             "", "| Term | Meaning | Do not use | Uses |", "|---|---|---|---|"]
    for row in rows:
        best = next(m for m in row["members"] if m["word"] == row["dominant"])
        avoid = [m["word"] for m in row["members"] if m is not best]
        lines.append("| %s | %s | %s | %s |" % (
            _cell(best["word"]), _cell(row["meaning"] or "TODO"),
            _cell(", ".join(avoid)) if avoid else "-",
            "%d code, %d docs" % (best["code"], best["docs"])))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI

def _usage(message):
    print("cousin-glossary: %s" % message, file=sys.stderr)
    return 2


def glossary_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-glossary",
        description="the words a repository uses, in its code and its docs, against a glossary")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("terms", help="the most used terms, code and docs counted apart")
    p.add_argument("path")
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--glossary", metavar="FILE", help="mark the terms it has")
    p.add_argument("--json", action="store_true")
    for name, text in (("groups", "each synonym group's uses, and the entries the code"
                                  " contradicts"),
                       ("derive", "write a glossary whose terms are the words the code uses")):
        p = sub.add_parser(name, help=text)
        p.add_argument("path")
        p.add_argument("--glossary", metavar="FILE", help="the entries and their do-not-use words")
        p.add_argument("--group", action="append", default=[], metavar="A,B,C",
                       help="a synonym group, the preferred word first (repeatable)")
        p.add_argument("--json", action="store_true")
        if name == "derive":
            p.add_argument("--out", required=True, metavar="FILE")
            p.add_argument("--force", action="store_true", help="overwrite --out")
    args = parser.parse_args(argv)
    root = Path(args.path)
    if not root.exists():
        return _usage("%s: no such file or directory" % args.path)
    glossary = None
    if args.glossary:
        try:
            glossary = load_glossary(args.glossary)
        except (OSError, UnicodeDecodeError) as err:
            return _usage("%s: %s" % (args.glossary, getattr(err, "strerror", None) or err))
    if args.cmd == "terms":
        if args.top < 1:
            return _usage("--top must be 1 or more")
        rows = terms(root, top=args.top, glossary=glossary)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                mark = " (%s)" % ("in the glossary" if r["glossary"] == "term"
                                  else "a do-not-use word") if r["glossary"] else ""
                print("%-24s %6d code %6d docs%s" % (r["term"], r["code"], r["docs"], mark))
        return 0
    groups = synonym_groups(glossary, args.group)
    if not groups:
        return _usage("%s needs --glossary or --group" % args.cmd)
    if args.cmd == "derive" and Path(args.out).exists() and not args.force:
        return _usage("%s exists; --force overwrites it" % args.out)
    rows = check_groups(root, groups)
    if args.cmd == "groups":
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                uses = ", ".join("%s %d/%d" % (m["word"], m["code"], m["docs"])
                                 for m in r["members"])
                print("%s %s: %s%s" % ("FLAG" if r["flagged"] else "ok  ", r["term"], uses,
                                       ("; " + r["message"]) if r["flagged"] else ""))
        return 1 if any(r["flagged"] for r in rows) else 0
    from cousin_lib import atomic
    text = derive_text(rows)
    atomic.write_text(args.out, text)
    if args.json:
        print(json.dumps({"out": args.out, "entries": len(rows)}))
    else:
        print("wrote %d entries to %s" % (len(rows), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(glossary_main())
