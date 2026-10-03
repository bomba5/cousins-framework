"""The rules inventory: which rules are refused in code and which are prose.

docs/reference/rules-inventory.md is the specification and the data: one
row per numbered rule of templates/law.md and per kind: rule file under
templates/shared/, each ENFORCED (naming the tests that prove the
refusal) or PROSE (with a note). This test keeps the table honest both
ways: a rule without a row or a row without a rule fails, a named test
that does not exist fails, and a test marked `# enforces: <rule>` for a
rule the table calls prose is a contradiction.
"""
import ast
import io
import pathlib
import re
import tempfile
import tokenize
import unittest

from cousin_lib import boot

REPO = pathlib.Path(__file__).resolve().parents[1]
INVENTORY = REPO / "docs" / "reference" / "rules-inventory.md"
LAW = REPO / "templates" / "law.md"
SHARED = REPO / "templates" / "shared"

HEADER = ["rule", "first words", "status", "tests", "note"]
STATUSES = ("ENFORCED", "PROSE")
_LAW_RULE = re.compile(r"^(\d+[a-z]?)\.\s+(\S.*)$")
_TEST_REF = re.compile(r"^tests/[\w/]+\.py::\w+::test\w*$")
_MARKER = re.compile(r"^#\s*enforces:\s*(.+?)\s*$")


# -- the rules ------------------------------------------------------------

def law_rules(text):
    """{'law <n>': text} for each numbered rule at the start of a line.
    A rule runs to the next blank line; indented lists inside it are its
    text, never rules of their own."""
    rules, current = {}, None
    for line in text.splitlines():
        m = _LAW_RULE.match(line)
        if m:
            current = "law " + m.group(1)
            rules[current] = [m.group(2)]
        elif current and line.strip():
            rules[current].append(line.strip())
        else:
            current = None
    return {k: " ".join(v) for k, v in rules.items()}


def house_rules(shared_dir):
    """{'house <stem>': description} for each kind: rule file directly in
    shared_dir (the examples subdirectory is not shipped active)."""
    rules = {}
    for path in sorted(pathlib.Path(shared_dir).glob("*.md")):
        fields, _ = boot._frontmatter(path.read_text())
        if fields.get("kind") == "rule":
            rules["house " + path.stem] = fields.get("description", "")
    return rules


def shipped_rules():
    rules = law_rules(LAW.read_text())
    rules.update(house_rules(SHARED))
    return rules


# -- the table ------------------------------------------------------------

def parse_inventory(text):
    """(rows, problems). A row is a dict of the five columns plus its
    line number; the table is the one whose header is HEADER."""
    rows, problems, in_table = [], [], False
    for number, line in enumerate(text.splitlines(), 1):
        if not line.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells == HEADER:
            in_table = True
            continue
        if not in_table or set("".join(cells)) <= set("-: "):
            continue
        if len(cells) != len(HEADER):
            problems.append("line %d: %d cells, expected %d" % (number, len(cells), len(HEADER)))
            continue
        row = dict(zip(HEADER, cells))
        row["line"] = number
        rows.append(row)
    if not in_table and not rows:
        problems.append("no table with the header %s" % " | ".join(HEADER))
    return rows, problems


def cell_refs(cell):
    """(refs, leftover): the backticked test references in a cell, and
    whatever else the cell holds besides them and their separators."""
    refs = re.findall(r"`([^`]*)`", cell)
    leftover = re.sub(r"`[^`]*`", "", cell).replace(",", "").strip()
    return refs, leftover


# -- the tests ------------------------------------------------------------

def _methods(path):
    """{(class, method)} of the test file, read without importing it."""
    tree = ast.parse(path.read_text())
    return {(c.name, f.name) for c in tree.body if isinstance(c, ast.ClassDef)
            for f in c.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}


def resolve(root, ref):
    """None when the reference names an existing test method, else why not."""
    if not _TEST_REF.match(ref):
        return "%r is not tests/<file>.py::<Class>::<test_method>" % ref
    rel, cls, method = ref.split("::")
    path = pathlib.Path(root) / rel
    if not path.is_file():
        return "%s does not exist" % rel
    if (cls, method) not in _methods(path):
        return "%s has no %s.%s" % (rel, cls, method)
    return None


def markers(root):
    """[(rule id, test ref or None, where)] for each `# enforces:` comment
    under <root>/tests. Comments are read with tokenize, so the words
    inside a string literal are never a marker; the ref is the test
    method the comment sits in, None when it sits in none."""
    found = []
    for path in sorted((pathlib.Path(root) / "tests").rglob("*.py")):
        source = path.read_text()
        if "enforces:" not in source:
            continue
        rel = path.relative_to(root).as_posix()
        spans = [(f.lineno, f.end_lineno, "%s::%s::%s" % (rel, c.name, f.name))
                 for c in ast.parse(source).body if isinstance(c, ast.ClassDef)
                 for f in c.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                continue
            m = _MARKER.match(tok.string)
            if not m:
                continue
            line = tok.start[0]
            ref = next((r for lo, hi, r in spans if lo <= line <= hi), None)
            for rule in m.group(1).split(","):
                found.append((" ".join(rule.split()), ref, "%s:%d" % (rel, line)))
    return found


# -- the audit ------------------------------------------------------------

def audit(inventory_text, rules, root):
    """Every disagreement between the table, the rules and the tests under
    root, one line each. Empty means the inventory is honest."""
    rows, problems = parse_inventory(inventory_text)
    seen = {}
    for row in rows:
        rule, where = row["rule"], "line %d (%s)" % (row["line"], row["rule"])
        if rule in seen:
            problems.append("%s: duplicate row, first at line %d" % (where, seen[rule]))
            continue
        seen[rule] = row["line"]
        if rule not in rules:
            problems.append("%s: no such rule any more; remove or rename the row" % where)
        else:
            words, source = row["first words"].split(), rules[rule].split()
            if len(words) < 3 or source[:len(words)] != words:
                problems.append("%s: first words %r do not open the rule %r"
                                % (where, row["first words"], " ".join(source[:8])))
        status = row["status"]
        if status not in STATUSES:
            problems.append("%s: status %r is not one of %s" % (where, status, ", ".join(STATUSES)))
        refs, leftover = cell_refs(row["tests"])
        if leftover:
            problems.append("%s: tests cell holds %r outside backticks" % (where, leftover))
        for ref in refs:
            why = resolve(root, ref)
            if why:
                problems.append("%s: %s" % (where, why))
        if status == "ENFORCED" and not refs:
            problems.append("%s: ENFORCED names no test" % where)
        if status == "PROSE" and not row["note"]:
            problems.append("%s: PROSE without a note" % where)
    for rule in rules:
        if rule not in seen:
            problems.append("%s: missing from the inventory; add a row" % rule)
    by_rule = {r["rule"]: r for r in rows}
    for rule, ref, where in markers(root):
        row = by_rule.get(rule)
        if ref is None:
            problems.append("%s: an enforces marker outside a test method" % where)
        elif row is None:
            problems.append("%s: enforces %r, which has no row" % (where, rule))
        elif row["status"] != "ENFORCED":
            problems.append("%s: enforces %r, which the inventory marks %s"
                            % (where, rule, row["status"]))
        elif ref not in cell_refs(row["tests"])[0]:
            problems.append("%s: enforces %r, but its row does not name %s" % (where, rule, ref))
    return problems


# -- the checks -----------------------------------------------------------

class TestTheShippedInventory(unittest.TestCase):
    def test_the_inventory_agrees_with_the_rules_and_the_tests(self):
        problems = audit(INVENTORY.read_text(), shipped_rules(), REPO)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_every_law_rule_and_every_house_rule_is_read(self):
        # A parser that read nothing would make the audit pass on an
        # empty table; pin what it reads from what ships.
        rules = shipped_rules()
        for rule in ("law 1", "law 3a", "law 14", "house reference_first-principles"):
            self.assertIn(rule, rules)
        self.assertEqual(sum(r.startswith("law ") for r in rules), 15)
        self.assertEqual(sum(r.startswith("house ") for r in rules),
                         len(list(SHARED.glob("*.md"))))

    def test_the_page_is_plain_ascii(self):
        self.assertTrue(INVENTORY.read_text().isascii())


LAW_FIXTURE = """# Law

1. Alpha rule says one thing.
   - an indented list item
   2. not a rule, indented

2. Bravo rule says another.

2a. Bravo sub rule follows.
"""

HEAD = "| rule | first words | status | tests | note |\n|---|---|---|---|---|\n"
GOOD_REF = "tests/test_fixture.py::TestThing::test_refuses"
TEST_FIXTURE = '''import unittest


class TestThing(unittest.TestCase):
    def test_refuses(self):
        # enforces: law 1
        self.assertTrue(True)

    def test_other(self):
        text = "# enforces: law 2"
        self.assertTrue(text)
'''


class FixtureCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_fixture.py").write_text(TEST_FIXTURE)
        self.rules = law_rules(LAW_FIXTURE)

    def table(self, *rows):
        return HEAD + "".join("| %s |\n" % " | ".join(r) for r in rows)

    def good_rows(self):
        return [("law 1", "Alpha rule says", "ENFORCED", "`%s`" % GOOD_REF, ""),
                ("law 2", "Bravo rule says", "PROSE", "", "none: a habit."),
                ("law 2a", "Bravo sub rule", "PROSE", "", "none: a habit.")]

    def audit(self, *rows):
        return audit(self.table(*rows), self.rules, self.root)


class TestTheParsers(FixtureCase):
    def test_law_rules_are_numbered_lines_and_indented_lists_are_their_text(self):
        self.assertEqual(sorted(self.rules), ["law 1", "law 2", "law 2a"])
        self.assertIn("an indented list item", self.rules["law 1"])

    def test_house_rules_are_the_kind_rule_files_only(self):
        shared = self.root / "shared"
        (shared / "examples").mkdir(parents=True)
        (shared / "reference_a.md").write_text(
            "---\nname: reference_a\ndescription: Keep it tidy\nkind: rule\n---\nbody\n")
        (shared / "reference_b.md").write_text(
            "---\nname: reference_b\ndescription: Just an index line\n---\nbody\n")
        (shared / "examples" / "reference_c.md").write_text(
            "---\nname: reference_c\ndescription: Not active\nkind: rule\n---\nbody\n")
        self.assertEqual(house_rules(shared), {"house reference_a": "Keep it tidy"})

    def test_a_marker_in_a_string_is_not_a_marker(self):
        self.assertEqual(markers(self.root),
                         [("law 1", GOOD_REF, "tests/test_fixture.py:6")])


class TestTheAuditFailsBothWays(FixtureCase):
    def test_a_complete_and_honest_table_passes(self):
        self.assertEqual(self.audit(*self.good_rows()), [])

    def test_a_row_naming_a_missing_test_method_fails(self):
        rows = self.good_rows()
        rows[1] = ("law 2", "Bravo rule says", "PROSE",
                   "`tests/test_fixture.py::TestThing::test_gone`", "partly: x.")
        problems = self.audit(*rows)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("has no TestThing.test_gone", problems[0])

    def test_a_row_naming_a_missing_test_file_fails(self):
        rows = self.good_rows()
        rows[0] = ("law 1", "Alpha rule says", "ENFORCED",
                   "`%s`, `tests/test_nowhere.py::TestX::test_y`" % GOOD_REF, "")
        problems = self.audit(*rows)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tests/test_nowhere.py does not exist", problems[0])

    def test_a_malformed_reference_fails(self):
        rows = self.good_rows()
        rows[0] = ("law 1", "Alpha rule says", "ENFORCED", "`%s`, test_refuses" % GOOD_REF, "")
        problems = self.audit(*rows)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("outside backticks", problems[0])

    def test_a_new_law_rule_absent_from_the_table_fails(self):
        self.rules = law_rules(LAW_FIXTURE + "\n3. Charlie rule is new.\n")
        problems = self.audit(*self.good_rows())
        self.assertEqual(problems, ["law 3: missing from the inventory; add a row"])

    def test_a_removed_rule_still_in_the_table_fails(self):
        self.rules = law_rules(LAW_FIXTURE.replace("2a. Bravo sub rule follows.\n", ""))
        problems = self.audit(*self.good_rows())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("(law 2a): no such rule any more", problems[0])

    def test_a_renumbered_rule_fails_on_its_first_words(self):
        self.rules = law_rules(LAW_FIXTURE.replace("1. Alpha", "1. Delta"))
        problems = self.audit(*self.good_rows())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("do not open the rule", problems[0])

    def test_enforced_without_a_test_and_prose_without_a_note_fail(self):
        rows = self.good_rows()
        rows[1] = ("law 2", "Bravo rule says", "ENFORCED", "", "")
        rows[2] = ("law 2a", "Bravo sub rule", "PROSE", "", "")
        problems = self.audit(*rows)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("ENFORCED names no test", problems[0])
        self.assertIn("PROSE without a note", problems[1])

    def test_an_unknown_status_and_a_duplicate_row_fail(self):
        rows = self.good_rows()
        rows[2] = ("law 2a", "Bravo sub rule", "MAYBE", "", "x")
        problems = self.audit(*(rows + [rows[1]]))
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("is not one of ENFORCED, PROSE", problems[0])
        self.assertIn("duplicate row", problems[1])


class TestTheMarkers(FixtureCase):
    def test_a_marker_on_a_prose_rule_is_a_contradiction(self):
        rows = self.good_rows()
        rows[0] = ("law 1", "Alpha rule says", "PROSE", "", "none: x.")
        problems = self.audit(*rows)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("enforces 'law 1', which the inventory marks PROSE", problems[0])

    def test_a_marked_test_the_enforced_row_does_not_name_fails(self):
        (self.root / "tests" / "test_more.py").write_text(
            "import unittest\n\n\nclass TestMore(unittest.TestCase):\n"
            "    def test_also(self):  # enforces: law 1\n        pass\n")
        problems = self.audit(*self.good_rows())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("does not name tests/test_more.py::TestMore::test_also", problems[0])

    def test_a_marker_for_an_unknown_rule_or_outside_a_test_fails(self):
        (self.root / "tests" / "test_more.py").write_text(
            "# enforces: law 1\nimport unittest\n\n\nclass TestMore(unittest.TestCase):\n"
            "    def test_also(self):\n        # enforces: law 99\n        pass\n")
        problems = self.audit(*self.good_rows())
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("tests/test_more.py:1: an enforces marker outside a test method", problems[0])
        self.assertIn("enforces 'law 99', which has no row", problems[1])


if __name__ == "__main__":
    unittest.main()
