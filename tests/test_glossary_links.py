"""Every page links the first use of each glossary
term to its entry in docs/glossary.md.

A use is the term as a word (a plural counts) in prose: not in a code
block, an inline code span, a heading or another page's link. The first
such use on a page must be a link to `glossary.md#<term>`."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "docs" / "glossary.md"
# the glossary's terms, longest first so "shared tier" is never read as "tier"
TERMS = ("shared tier", "supervisor", "distilled", "rollover", "cousin", "worker",
         "thread", "runner", "inbox", "stream", "lane", "turn", "fold", "flip")
# Ordinary-English uses of a term, and other senses the glossary does not
# define (the auth "login lane", an OS thread, the console's SSE stream):
# never a use. Matched case-insensitively on the raw line.
IGNORE = {
    "turn": (r"\bturn(?:s|ed)?\s+(?:on|off|it|them|that|something|into)\b",
             r"\bturns?\s+(?:a|an|the)\s+[\w-]+\s+(?:on|off)\b",
             r"\bin turn\b", r"\bturns\s+`",
             r"\bturns(?:\s+(?:the|a)\s+\w+)?\s+(?:online|offline)\b", r"\band turns\s*$"),
    "fold": (r"\bfolds?\s+(?:old|only|daily|the agent|into one)\b", r"\braw fold\b"),
    "stream": (r"server-sent-events stream", r"\blive event stream\b",
               r"\bstreams into\b", r"\bevent streams alike\b"),
    "thread": (r"\bthreads of work\b", r"\bactive[ -]threads\b", r"\brequest's thread\b",
               r"\bper thread in flight\b", r"\bkeep the thread\b",
               r"\bbackground thread\b", r"\bincluding threads\b"),
    "lane": (r"\blogin lane\b", r"\btwo lanes\b", r"\bpick one lane\b", r"\bkey lane\b"),
    "worker": (r"\bits worker ended\b",),
}
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
CODE = re.compile(r"`[^`]*`")


def pages():
    out = [ROOT / "README.md"]
    out += sorted(p for p in (ROOT / "docs").glob("*.md") if p != GLOSSARY)
    out += sorted((ROOT / "docs" / "reference").glob("*.md"))
    return out


def anchor(term):
    return term.replace(" ", "-")


def glossary_href(page):
    if page.parent == ROOT:
        return "docs/glossary.md"
    depth = len(page.relative_to(ROOT / "docs").parts) - 1
    return "../" * depth + "glossary.md"


def _word(term):
    return re.compile(r"(?<![\w`/.#-])(%s)(?:s|es)?(?![\w-])" % re.escape(term), re.I)


def prose_lines(text):
    """(index, line) of the lines that are prose: outside fences, outside a
    generated region (`<!-- x:begin ... -->` to `<!-- x:end -->`, which its
    generator rewrites), not headings."""
    fence = generated = False
    for i, line in enumerate(text.split("\n")):
        stripped = line.lstrip()
        if stripped.startswith("<!--") and ":begin" in stripped:
            generated = True
        if generated:
            generated = not (stripped.startswith("<!--") and ":end" in stripped)
            continue
        if stripped.startswith("```"):
            fence = not fence
            continue
        if fence or stripped.startswith("#"):
            continue
        yield i, line


def first_use(text, term):
    """(line index, start, end, glossary_link) of the term's first use, or None.
    glossary_link is True when that use is the text of a link to the glossary."""
    word = _word(term)
    for i, line in prose_lines(text):
        masked = line
        for pattern in IGNORE.get(term, ()):
            masked = re.sub(pattern, lambda m: " " * len(m.group(0)), masked, flags=re.I)
        masked = CODE.sub(lambda m: " " * len(m.group(0)), masked)
        spans = []
        for m in LINK.finditer(masked):
            to_glossary = "glossary.md#" in m.group(2)
            spans.append((m.start(), m.end(), m.start(1), m.end(1), to_glossary))
        for m in word.finditer(masked):
            inside = next((s for s in spans if s[0] <= m.start() < s[1]), None)
            if inside is None:
                return i, m.start(), m.end(), False
            if inside[4] and inside[2] <= m.start() < inside[3]:
                return i, m.start(), m.end(), True
            # another page's link, or a URL: not a use
    return None


class TestGlossaryLinks(unittest.TestCase):
    def test_every_term_has_an_entry(self):
        heads = set(re.findall(r"(?m)^### (.+)$", GLOSSARY.read_text()))
        self.assertEqual([t for t in TERMS if t not in heads], [])

    def test_the_first_use_of_each_term_links_the_glossary(self):
        missing = []
        for page in pages():
            text = page.read_text()
            for term in TERMS:
                found = first_use(text, term)
                if found is not None and not found[3]:
                    line = text.split("\n")[found[0]]
                    missing.append("%s:%d %s: %s" % (page.relative_to(ROOT), found[0] + 1,
                                                     term, line.strip()[:80]))
        self.assertEqual(missing, [], "link the first use to glossary.md#<term>")

    def test_glossary_links_name_an_entry(self):
        heads = {anchor(h) for h in re.findall(r"(?m)^### (.+)$", GLOSSARY.read_text())}
        bad = []
        for page in pages():
            for m in re.finditer(r"glossary\.md#([\w-]+)", page.read_text()):
                if m.group(1) not in heads:
                    bad.append("%s: #%s" % (page.relative_to(ROOT), m.group(1)))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
