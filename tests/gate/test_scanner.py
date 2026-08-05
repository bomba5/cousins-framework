"""Scanner behavior tests.

All planted terms are fictional (zorblatt, quibbleton, ...): the test corpus
for a name-removal tool must not itself carry a name. Strings that would
trip the scanner's generic classes (addresses, secret shapes) are assembled
at runtime rather than written as literals, so the gate scanning its own
repository stays clean.
"""
import base64
import pathlib
import tempfile
import unittest

from cousin_lib.gate.scanner import Scanner


def _b64url(payload):
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


class TestNameMatching(unittest.TestCase):
    def test_denylisted_name_in_text_is_a_hit(self):
        s = Scanner(name_terms=["zorblatt"])
        hits = s.scan_text("deploy zorblatt tomorrow\n", filename="a.txt")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].term, "zorblatt")

    def test_substring_inside_a_word_is_not_a_hit(self):
        # A short term must never match inside longer words ("rea" inside
        # React/textarea/create): boundaries are mandatory.
        s = Scanner(name_terms=["rea"])
        hits = s.scan_text(
            "const el = React.createElement('textarea')\n", filename="a.jsx"
        )
        self.assertEqual(hits, [])

    def test_matching_is_case_insensitive(self):
        s = Scanner(name_terms=["zorblatt"])
        hits = s.scan_text("Ask Zorblatt; ZORBLATT knows.\n", filename="a.md")
        self.assertEqual(len(hits), 2)


class TestHitAuditability(unittest.TestCase):
    def test_hit_reports_file_line_column_and_context(self):
        s = Scanner(name_terms=["zorblatt"])
        text = "line one\nsay hi to zorblatt today\nline three\n"
        (hit,) = s.scan_text(text, filename="dir/a.txt")
        self.assertEqual(hit.file, "dir/a.txt")
        self.assertEqual(hit.line, 2)
        self.assertEqual(hit.col, 11)
        self.assertIn("zorblatt", hit.context)
        self.assertEqual(hit.context, "say hi to zorblatt today")


class TestPythonPositionClassification(unittest.TestCase):
    """Position decides disposition: a hit in a comment is mechanical, a
    hit in executable code is structural."""

    def _scan(self, source):
        s = Scanner(name_terms=["zorblatt"])
        return s.scan_text(source, filename="mod.py")

    def test_hit_in_comment_is_comment_position(self):
        (hit,) = self._scan("x = 1  # reviewed by zorblatt\n")
        self.assertEqual(hit.position, "comment")

    def test_hit_in_docstring_is_docstring_position(self):
        source = '"""Module notes: zorblatt wrote the first draft."""\nx = 1\n'
        (hit,) = self._scan(source)
        self.assertEqual(hit.position, "docstring")

    def test_hit_in_string_literal_in_code_is_code_string_position(self):
        source = 'if user == "zorblatt":\n    grant()\n'
        (hit,) = self._scan(source)
        self.assertEqual(hit.position, "code-string")


class TestGenericClasses(unittest.TestCase):
    """Non-name contamination classes are built in: they need no denylist.
    Address fixtures are assembled at runtime so this file carries no
    address-shaped literal of its own."""

    def test_private_lan_address_literal_is_a_hit(self):
        s = Scanner()
        addr = ".".join(["192", "168", "5", "43"])
        (hit,) = s.scan_text("URL = 'http://%s:9'\n" % addr, filename="c.py")
        self.assertEqual(hit.kind, "address")
        self.assertEqual(hit.term, addr)

    def test_cgnat_address_literal_is_a_hit(self):
        s = Scanner()
        addr = ".".join(["100", "64", "0", "1"])
        (hit,) = s.scan_text("peer = '%s'\n" % addr, filename="c.py")
        self.assertEqual(hit.kind, "address")

    def test_public_address_is_not_a_hit(self):
        s = Scanner()
        addr = ".".join(["8", "8", "8", "8"])
        hits = s.scan_text("dns = '%s'\n" % addr, filename="c.py")
        self.assertEqual(hits, [])

    def test_absolute_home_path_is_a_hit(self):
        s = Scanner()
        path = "/".join(["", "home", "zuser", "f.txt"])
        (hit,) = s.scan_text("P = '%s'\n" % path, filename="c.py")
        self.assertEqual(hit.kind, "home-path")

    def test_home_relative_paths_are_not_hits(self):
        s = Scanner()
        hits = s.scan_text("P = os.path.join(HOME, 'f.txt')\n", filename="c.py")
        self.assertEqual(hits, [])

    def test_jwt_shaped_string_is_a_secret_hit(self):
        s = Scanner()
        token = ".".join(
            [_b64url('{"alg":"none","typ":"JWT"}'), _b64url('{"sub":"x"}'), "x9" * 8]
        )
        (hit,) = s.scan_text("AUTH = '%s'\n" % token, filename="c.py")
        self.assertEqual(hit.kind, "secret")

    def test_ordinary_dotted_words_are_not_secret_hits(self):
        s = Scanner()
        hits = s.scan_text("v = 'alpha.beta.gamma'\n", filename="c.py")
        self.assertEqual(hits, [])


class TestTreeScanning(unittest.TestCase):
    def _tree(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content)
        return root

    def test_scan_tree_reports_relative_paths_and_skips_git_dir(self):
        root = self._tree(
            {
                "src/mod.py": "who = 'zorblatt'\n",
                ".git/config": "zorblatt everywhere\n",
            }
        )
        s = Scanner(name_terms=["zorblatt"])
        hits = s.scan_tree(root)
        self.assertEqual([h.file for h in hits], ["src/mod.py"])

    def test_unallowlisted_binary_is_an_opaque_carrier_hit(self):
        root = self._tree({"data/state.db": b"\x00\x01binaryblob"})
        s = Scanner()
        (hit,) = s.scan_tree(root)
        self.assertEqual(hit.kind, "binary")
        self.assertEqual(hit.file, "data/state.db")

    def test_allowlisted_binary_extension_is_not_a_hit(self):
        root = self._tree({"ui/icon.png": b"\x89PNG\x00fake"})
        s = Scanner()
        self.assertEqual(s.scan_tree(root), [])


if __name__ == "__main__":
    unittest.main()
