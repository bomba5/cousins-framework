"""Confined reads of a cousin home: every path resolves inside the
home, `.secrets/` is invisible at any depth and through any link, a
link that leaves the home is listed but never followed, and binary
files are never handed back as text."""
import os
import pathlib
import tempfile
import unittest

from cousin_lib import home_files
from cousin_lib.home_files import PathRefused


class HomeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name)
        self.home = self.base / "cousins" / "wren"
        (self.home / "notes").mkdir(parents=True)
        (self.home / ".secrets").mkdir()
        (self.home / ".secrets" / "token").write_text("s3cret")
        (self.home / "notes" / "a.md").write_text("# A\n\nbody\n")
        (self.home / ".hidden.txt").write_text("dot")
        (self.base / "outside.txt").write_text("outside")


class Resolve(HomeCase):
    def test_a_plain_relative_path_resolves_inside(self):
        self.assertEqual(home_files.resolve_in(self.home, "notes/a.md"),
                         (self.home / "notes" / "a.md").resolve())

    def test_empty_means_the_home_itself(self):
        self.assertEqual(home_files.resolve_in(self.home, ""),
                         self.home.resolve())

    def test_dotdot_absolute_and_nul_are_refused(self):
        for bad in ("../outside.txt", "notes/../../outside.txt",
                    "/etc/passwd", "notes/\x00a.md", "notes/./../.."):
            with self.assertRaises(PathRefused, msg=bad):
                home_files.resolve_in(self.home, bad)

    def test_secrets_is_refused_at_any_depth(self):
        (self.home / "notes" / ".secrets").mkdir()
        for bad in (".secrets", ".secrets/token", "notes/.secrets"):
            with self.assertRaises(PathRefused, msg=bad):
                home_files.resolve_in(self.home, bad)

    def test_a_link_out_of_the_home_is_refused(self):
        os.symlink(self.base / "outside.txt", self.home / "out.txt")
        with self.assertRaises(PathRefused):
            home_files.resolve_in(self.home, "out.txt")

    def test_a_link_into_secrets_is_refused(self):
        os.symlink(self.home / ".secrets", self.home / "innocent")
        with self.assertRaises(PathRefused):
            home_files.resolve_in(self.home, "innocent/token")

    def test_a_link_inside_the_home_is_followed(self):
        os.symlink(self.home / "notes", self.home / "n")
        self.assertEqual(home_files.resolve_in(self.home, "n/a.md"),
                         (self.home / "notes" / "a.md").resolve())


class ListDir(HomeCase):
    def test_secrets_is_never_listed_and_dotfiles_only_on_request(self):
        names = [e["name"] for e in
                 home_files.list_dir(self.home, "")["entries"]]
        self.assertEqual(names, ["notes"])
        names = [e["name"] for e in home_files.list_dir(
            self.home, "", show_hidden=True)["entries"]]
        self.assertEqual(names, ["notes", ".hidden.txt"])

    def test_entries_carry_type_size_and_mtime(self):
        row = home_files.list_dir(self.home, "notes")["entries"][0]
        self.assertEqual(row["name"], "a.md")
        self.assertEqual(row["type"], "file")
        self.assertEqual(row["path"], "notes/a.md")
        self.assertEqual(row["size"], len("# A\n\nbody\n"))
        self.assertIsInstance(row["mtime"], float)

    def test_a_link_out_is_listed_as_outside_and_not_followed(self):
        os.symlink(self.base, self.home / "escape")
        rows = {e["name"]: e for e in
                home_files.list_dir(self.home, "")["entries"]}
        self.assertEqual(rows["escape"]["type"], "link")
        self.assertTrue(rows["escape"]["outside"])
        with self.assertRaises(PathRefused):
            home_files.list_dir(self.home, "escape")

    def test_listing_a_file_is_refused(self):
        with self.assertRaises(PathRefused):
            home_files.list_dir(self.home, "notes/a.md")


class ReadFile(HomeCase):
    def test_text_pages_with_line_numbers(self):
        (self.home / "big.log").write_text(
            "".join("line %d\n" % i for i in range(1, 101)))
        page = home_files.read_text_page(self.home, "big.log", start=11,
                                         count=5)
        self.assertEqual(page["kind"], "text")
        self.assertEqual(page["start"], 11)
        self.assertEqual(page["lines"], ["line %d" % i
                                         for i in range(11, 16)])
        self.assertEqual(page["total_lines"], 100)
        self.assertTrue(page["more"])

    def test_the_last_page_says_no_more(self):
        (self.home / "s.txt").write_text("a\nb\n")
        page = home_files.read_text_page(self.home, "s.txt")
        self.assertEqual(page["lines"], ["a", "b"])
        self.assertFalse(page["more"])

    def test_markdown_is_marked_and_whole(self):
        page = home_files.read_text_page(self.home, "notes/a.md")
        self.assertEqual(page["kind"], "markdown")
        self.assertEqual(page["text"], "# A\n\nbody\n")

    def test_binary_is_never_text(self):
        (self.home / "blob.bin").write_bytes(b"\x00\x01\x02garbage")
        page = home_files.read_text_page(self.home, "blob.bin")
        self.assertEqual(page["kind"], "binary")
        self.assertNotIn("lines", page)
        self.assertNotIn("text", page)

    def test_images_are_marked_as_images(self):
        (self.home / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
        self.assertEqual(
            home_files.read_text_page(self.home, "p.png")["kind"], "image")

    def test_a_directory_is_refused(self):
        with self.assertRaises(PathRefused):
            home_files.read_text_page(self.home, "notes")


if __name__ == "__main__":
    unittest.main()
