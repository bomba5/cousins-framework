"""Glossary from code (cousin_lib/glossary.py): the word splitter, code and
docs counted apart, the files the walk skips, the synonym groups and their
dominant member, the derived glossary, and the CLI."""
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from cousin_lib import glossary as g
from cousin_lib import plain_language

PY = '''"""The reply mode module."""
import os

def reply_mode(chat_thread, replyTarget):
    # a reply goes to the operator
    return "conversation reply"
'''


class TestWords(unittest.TestCase):
    def test_identifiers_split_and_fold(self):
        self.assertEqual(g.words("reply_mode replyMode reply-mode HTTPServer"),
                         ["reply", "mode", "reply", "mode", "reply", "mode", "http", "server"])
        self.assertEqual(g.words("entries caches boxes status process threads don't v2"),
                         ["entry", "cache", "box", "status", "process", "thread", "don", "v"])

    def test_code_and_docs_regions(self):
        kinds = g.regions(pathlib.Path("m.py"), PY)
        code = " ".join(t for k, t in kinds if k == "code")
        docs = " ".join(t for k, t in kinds if k == "docs")
        self.assertIn("def reply_mode", code)
        self.assertNotIn("operator", code)
        self.assertIn("# a reply goes to the operator", docs)
        self.assertIn('"conversation reply"', docs)
        self.assertEqual(g.regions(pathlib.Path("a.md"), "x"), [("docs", "x")])
        slash = g.regions(pathlib.Path("a.js"), "let a = 1; // note\n/* more */ b")
        self.assertEqual([k for k, t in slash if t.strip()], ["code", "docs", "docs", "code"])


class Tree(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "repo"
        self.root.mkdir()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
        return path


class TestFiles(Tree):
    def test_the_walk_skips_what_it_should(self):
        self.write("app/mod.py", PY)
        self.write("README.md", "# Readme\n")
        self.write(".git/config", "x")
        self.write("node_modules/x/index.js", "x")
        self.write("build/out.py", "x")
        self.write("pkg.egg-info/PKG-INFO", "x")
        self.write("env/pyvenv.cfg", "home = /usr")
        self.write("env/lib/site.py", "x")
        self.write("big.txt", "word " * 300_000)
        self.write("blob.bin", b"\x00\x01binary")
        (self.root / "link.md").symlink_to(self.root / "README.md")
        rels, skipped, truncated = g.files(self.root)
        self.assertEqual(sorted(str(r) for r in rels), ["README.md", "app/mod.py", "blob.bin"])
        self.assertEqual((skipped, truncated), (2, False))
        corpus = g.scan(self.root)
        self.assertEqual((corpus.files, corpus.skipped), (2, 3))

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_gitignore_is_honoured_where_git_works(self):
        self.write("kept.md", "kept")
        self.write("ignored.md", "ignored")
        self.write("node_modules/tracked.js", "x")
        self.write(".gitignore", "ignored.md\n")
        self.write("env/pyvenv.cfg", "home = /usr")
        self.write("env/lib/site.py", "x")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        rels, _, _ = g.files(self.root)
        self.assertEqual(sorted(str(r) for r in rels), [".gitignore", "kept.md"])

    def test_a_single_file_scan_takes_the_same_rules(self):
        big = self.write("big.md", "word " * 300_000)
        self.assertEqual(g.files(big), ([], 1, False))
        small = self.write("small.md", "word")
        self.assertEqual(g.files(small)[0], [pathlib.Path("small.md")])
        self.assertEqual(g.scan(small).docs["word"], 1)
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        self.assertEqual(g.files(fifo), ([], 1, False))
        self.assertEqual(g.files(self.root)[0], [pathlib.Path("small.md")])

    def test_a_glossary_must_be_a_small_regular_file(self):
        import threading
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        got = []

        def load():
            try:
                plain_language.load_glossary(fifo)
                got.append("read")
            except OSError as err:
                got.append(err)
        reader = threading.Thread(target=load, daemon=True)
        reader.start()
        reader.join(5)
        if reader.is_alive():
            with open(fifo, "w"):
                pass        # let the blocked read end
            reader.join(5)
            self.fail("load_glossary blocked on a FIFO")
        self.assertIsInstance(got[0], OSError)
        big = self.write("g.md", "x" * (plain_language.GLOSSARY_MAX + 1))
        with self.assertRaises(OSError):
            plain_language.load_glossary(big)

    def test_a_secrets_directory_is_never_read(self):
        self.write(".secrets/token", "sekret value")
        self.write("a.md", "fine")
        self.assertEqual([str(r) for r in g.files(self.root)[0]], ["a.md"])

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_runs_no_fsmonitor_hook_of_the_scanned_repo(self):
        self.write("a.md", "x")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "a.md"], check=True)
        marker = self.root.parent / "ran"
        hook = self.write("../hook.sh", "#!/bin/sh\ntouch %s\n" % marker)
        hook.chmod(0o755)
        subprocess.run(["git", "-C", str(self.root), "config", "core.fsmonitor", str(hook)],
                       check=True)
        self.assertEqual([str(r) for r in g.files(self.root)[0]], ["a.md"])
        self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_a_directory_git_ignores_is_walked(self):
        self.write(".gitignore", "sub/\n")
        self.write("sub/a.md", "x")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.assertEqual([str(r) for r in g.files(self.root / "sub")[0]], ["a.md"])


class TestTermsAndGroups(Tree):
    def setUp(self):
        super().setUp()
        self.write("mod.py", PY)
        self.write("docs/a.md", "A conversation is a chat line. Threads, a thread.\n")
        self.write("lib/x.js", "const chatLine = conversationOf(chat_line, conversationId); // thread\n")

    def test_terms_count_code_and_docs_apart_and_mark_the_glossary(self):
        gloss = {"thread": {"meaning": "", "avoid": ["conversation"]}}
        rows = g.terms(self.root, top=50, glossary=gloss)
        by = {r["term"]: r for r in rows}
        self.assertEqual((by["reply"]["code"], by["reply"]["docs"]), (2, 3))
        self.assertEqual((by["thread"]["code"], by["thread"]["docs"], by["thread"]["glossary"]),
                         (1, 3, "term"))
        self.assertEqual((by["conversation"]["code"], by["conversation"]["glossary"]), (2, "avoid"))
        for gone in ("the", "a", "os", "def", "return", "is"):
            self.assertNotIn(gone, by)
        self.assertEqual(len(g.terms(self.root, top=2)), 2)
        self.assertEqual([r["term"] for r in rows][:1], ["reply"])

    def test_groups_name_the_dominant_member_and_flag_the_entry(self):
        groups = g.synonym_groups({"thread": {"meaning": "a chat", "avoid": ["conversation"]}},
                                  ["chat line, thread"])
        rows = g.check_groups(self.root, groups)
        first, second = rows
        self.assertEqual(first["members"], [{"word": "thread", "code": 1, "docs": 3},
                                            {"word": "conversation", "code": 2, "docs": 2}])
        self.assertEqual((first["dominant"], first["flagged"]), ("conversation", True))
        self.assertEqual(first["message"],
                         'code says "conversation" 2 times, the glossary prefers "thread" (1)')
        # a phrase matches chat_line, chatLine and the prose "chat line"
        self.assertEqual(second["members"][0], {"word": "chat line", "code": 2, "docs": 1})
        self.assertEqual((second["dominant"], second["flagged"]), ("chat line", False))

    def test_docs_break_a_tie_and_the_preferred_term_wins_a_full_one(self):
        self.write("t.md", "alpha beta beta\n")
        self.write("t.py", "alpha = beta = 1\n")
        rows = g.check_groups(self.root, g.synonym_groups(None, ["alpha,beta", "gamma,delta"]))
        self.assertEqual([(r["dominant"], r["flagged"]) for r in rows],
                         [("beta", True), ("gamma", False)])

    def test_a_term_with_no_words_is_never_compared(self):
        rows = g.check_groups(self.root, g.synonym_groups(
            {"--": {"meaning": "", "avoid": ["thread"]}}))
        self.assertEqual((rows[0]["dominant"], rows[0]["flagged"], rows[0]["message"]),
                         ("--", False, "the term has no words to count"))
        self.assertEqual(rows[0]["members"][0], {"word": "--", "code": 0, "docs": 0})

    def test_derive_writes_the_table_format_the_check_reads(self):
        groups = g.synonym_groups({"thread": {"meaning": "a chat | line", "avoid": ["conversation"]},
                                   "turn": {"meaning": "", "avoid": []}})
        text = g.derive_text(g.check_groups(self.root, groups))
        self.assertIn("| Term | Meaning | Do not use | Uses |", text)
        self.assertEqual(plain_language.parse_glossary(text), {
            "conversation": {"meaning": "a chat | line", "avoid": ["thread"]},
            "turn": {"meaning": "TODO", "avoid": []}})
        self.assertIn("| conversation | a chat \\| line | thread | 2 code, 2 docs |", text)


class TestCli(Tree):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = g.glossary_main(list(argv))
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_terms_groups_and_derive(self):
        self.write("mod.py", PY)
        gloss = self.write("../g.md", "| Term | Meaning | Do not use |\n|---|---|---|\n"
                                      "| conversation | a chat | thread |\n")
        rc, out, _ = self.run_cli("terms", str(self.root), "--top", "3", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)), 3)
        rc, out, _ = self.run_cli("groups", str(self.root), "--glossary", str(gloss))
        self.assertEqual(rc, 1)
        self.assertIn('FLAG conversation: conversation 0/1, thread 1/0; code says "thread" 1 times', out)
        self.assertEqual(self.run_cli("groups", str(self.root), "--group", "reply,answer")[0], 0)
        self.assertEqual(self.run_cli("groups", str(self.root))[0], 2)
        self.assertEqual(self.run_cli("terms", str(self.root / "nope"))[0], 2)
        self.assertEqual(self.run_cli("terms", str(self.root), "--glossary", "/nonexistent")[0], 2)
        out_file = self.root.parent / "derived.md"
        rc, out, _ = self.run_cli("derive", str(self.root), "--glossary", str(gloss),
                                  "--out", str(out_file), "--json")
        self.assertEqual((rc, json.loads(out)["entries"]), (0, 1))
        self.assertIn("| thread | a chat | conversation | 1 code, 0 docs |", out_file.read_text())
        out_file.write_text("mine")
        self.assertEqual(self.run_cli("derive", str(self.root), "--glossary", str(gloss),
                                      "--out", str(out_file))[0], 2)
        self.assertEqual(out_file.read_text(), "mine")
        self.assertEqual(self.run_cli("derive", str(self.root), "--glossary", str(gloss),
                                      "--out", str(out_file), "--force")[0], 0)
        self.assertNotEqual(out_file.read_text(), "mine")


if __name__ == "__main__":
    unittest.main()
