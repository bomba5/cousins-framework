"""Removing a memory is a move, never a destruction: a raw entry, a
decision or a memory/note file goes into <home>/memory/.trash/<id>/
with its original path (and, for a JSONL line, the line itself), an
audit line is written, and restore puts it back where it was."""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory_trash
from cousin_lib.home_files import PathRefused


def _line(entry):
    return json.dumps(entry)


class TrashCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name)
        self.home = self.base / "cousins" / "wren"
        for sub in ("memory/raw", "memory/distilled", "notes", "data",
                    "legacy", ".secrets"):
            (self.home / sub).mkdir(parents=True)
        self.raw = self.home / "memory" / "raw" / "2026-09-01.jsonl"
        self.entries = [
            {"topic": "alpha", "content": "first",
             "truth_level": "L3_COUSIN_CONCLUSION",
             "timestamp": "2026-09-01T10:00:00+00:00"},
            {"topic": "beta", "content": "second",
             "truth_level": "L0_OPERATOR",
             "timestamp": "2026-09-01T11:00:00+00:00"},
            {"topic": "gamma", "content": "third",
             "truth_level": "L5_OBSOLETE",
             "timestamp": "2026-09-01T12:00:00+00:00"},
        ]
        self.raw.write_text("".join(_line(e) + "\n" for e in self.entries))
        (self.home / "notes" / "n.md").write_text("# note\nkeep me\n")
        (self.home / "memory" / "fact.md").write_text("# fact\n")
        (self.home / "legacy" / "old.tar.gz").write_bytes(b"\x1f\x8bold")
        (self.home / ".secrets" / "k").write_text("x")
        (self.home / "memory" / "distilled" / "glossary.md").write_text("g")

    def lines(self):
        return self.raw.read_text().splitlines()


class TrashLine(TrashCase):
    def test_a_raw_entry_moves_to_the_trash_with_its_line(self):
        sha = memory_trash.line_sha(self.lines()[1])
        batch = memory_trash.trash_lines(
            self.home, [("memory/raw/2026-09-01.jsonl", 2, sha)], by="ana")
        self.assertEqual([json.loads(l)["topic"] for l in self.lines()],
                         ["alpha", "gamma"])
        item = batch["items"][0]
        self.assertEqual(item["path"], "memory/raw/2026-09-01.jsonl")
        self.assertEqual(item["line_no"], 2)
        self.assertEqual(json.loads(item["line"])["topic"], "beta")
        manifest = (self.home / "memory" / ".trash" / batch["id"]
                    / "manifest.json")
        self.assertTrue(manifest.is_file())
        audit = (self.home / "memory" / ".trash" / "audit.jsonl") \
            .read_text().splitlines()
        self.assertEqual(json.loads(audit[-1])["action"], "trash")
        self.assertEqual(json.loads(audit[-1])["by"], "ana")

    def test_restore_puts_the_line_back_where_it_was(self):
        sha = memory_trash.line_sha(self.lines()[1])
        before = self.raw.read_text()
        batch = memory_trash.trash_lines(
            self.home, [("memory/raw/2026-09-01.jsonl", 2, sha)])
        memory_trash.restore(self.home, batch["id"])
        self.assertEqual(self.raw.read_text(), before)
        self.assertFalse((self.home / "memory" / ".trash"
                          / batch["id"]).exists())
        self.assertEqual(memory_trash.list_trash(self.home), [])

    def test_a_shifted_line_is_found_by_its_hash(self):
        sha = memory_trash.line_sha(self.lines()[2])
        # someone removed line 1 since the explorer read the file
        self.raw.write_text("\n".join(self.lines()[1:]) + "\n")
        memory_trash.trash_lines(
            self.home, [("memory/raw/2026-09-01.jsonl", 3, sha)])
        self.assertEqual([json.loads(l)["topic"] for l in self.lines()],
                         ["beta"])

    def test_a_stale_hash_is_a_conflict_and_changes_nothing(self):
        before = self.raw.read_text()
        with self.assertRaises(PathRefused) as ctx:
            memory_trash.trash_lines(
                self.home, [("memory/raw/2026-09-01.jsonl", 1, "0" * 12)])
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.raw.read_text(), before)

    def test_decision_lines_are_allowed_other_jsonl_are_not(self):
        dec = self.home / "data" / "decisions.jsonl"
        dec.write_text(_line({"topic": "t", "decision": "d",
                              "reasoning": "r"}) + "\n")
        sha = memory_trash.line_sha(dec.read_text().splitlines()[0])
        memory_trash.trash_lines(self.home,
                                 [("data/decisions.jsonl", 1, sha)])
        self.assertEqual(dec.read_text(), "")
        other = self.home / "data" / "other.jsonl"
        other.write_text("{}\n")
        for rel in ("data/other.jsonl", "memory/raw/archive/x.jsonl",
                    "../x.jsonl", ".secrets/k"):
            with self.assertRaises(PathRefused, msg=rel):
                memory_trash.trash_lines(self.home, [(rel, 1, None)])

    def test_one_batch_holds_several_lines_and_restores_them_all(self):
        lines = self.lines()
        batch = memory_trash.trash_lines(self.home, [
            ("memory/raw/2026-09-01.jsonl", 1, memory_trash.line_sha(lines[0])),
            ("memory/raw/2026-09-01.jsonl", 3, memory_trash.line_sha(lines[2])),
        ])
        self.assertEqual(len(self.lines()), 1)
        memory_trash.restore(self.home, batch["id"])
        self.assertEqual(self.lines(), lines)

    def test_restoring_a_line_that_is_back_already_is_a_conflict(self):
        lines = self.lines()
        batch = memory_trash.trash_lines(self.home, [
            ("memory/raw/2026-09-01.jsonl", 1, memory_trash.line_sha(lines[0]))])
        self.raw.write_text("\n".join(lines) + "\n")
        with self.assertRaises(PathRefused) as ctx:
            memory_trash.restore(self.home, batch["id"])
        self.assertEqual(ctx.exception.status, 409)


class TrashFile(TrashCase):
    def test_a_note_moves_and_restores_byte_identical(self):
        batch = memory_trash.trash_file(self.home, "notes/n.md", by="ana")
        self.assertFalse((self.home / "notes" / "n.md").exists())
        kept = (self.home / "memory" / ".trash" / batch["id"] / "files"
                / "notes" / "n.md")
        self.assertEqual(kept.read_text(), "# note\nkeep me\n")
        listed = memory_trash.list_trash(self.home)
        self.assertEqual(listed[0]["items"][0]["path"], "notes/n.md")
        memory_trash.restore(self.home, batch["id"])
        self.assertEqual((self.home / "notes" / "n.md").read_text(),
                         "# note\nkeep me\n")

    def test_restore_never_overwrites(self):
        batch = memory_trash.trash_file(self.home, "memory/fact.md")
        (self.home / "memory" / "fact.md").write_text("new")
        with self.assertRaises(PathRefused) as ctx:
            memory_trash.restore(self.home, batch["id"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual((self.home / "memory" / "fact.md").read_text(),
                         "new")

    def test_refused_targets(self):
        os.symlink(self.home / "notes" / "n.md",
                   self.home / "notes" / "link.md")
        for rel in ("memory/distilled/glossary.md", ".secrets/k",
                    "memory/raw/2026-09-01.jsonl", "cousin.toml",
                    "notes/../cousin.toml", "memory/.trash/audit.jsonl",
                    "notes", "notes/link.md", "memory/missing.md",
                    "legacy/old.tar.gz"):
            with self.assertRaises(PathRefused, msg=rel):
                memory_trash.trash_file(self.home, rel)
        self.assertTrue((self.home / "notes" / "n.md").exists())

    def test_a_linked_directory_cannot_reach_a_refused_place(self):
        os.symlink(self.home / "memory" / "distilled",
                   self.home / "notes" / "d")
        with self.assertRaises(PathRefused):
            memory_trash.trash_file(self.home, "notes/d/glossary.md")
        os.symlink(self.home / "memory", self.home / "notes" / "m")
        with self.assertRaises(PathRefused):
            memory_trash.trash_lines(self.home, [
                ("memory/raw/../../notes/m/raw/2026-09-01.jsonl", 1, None)])
        self.assertTrue((self.home / "memory" / "distilled"
                         / "glossary.md").exists())

    def test_legacy_only_when_explicitly_selected_and_still_to_trash(self):
        batch = memory_trash.trash_file(self.home, "legacy/old.tar.gz",
                                        allow_legacy=True)
        self.assertFalse((self.home / "legacy" / "old.tar.gz").exists())
        memory_trash.restore(self.home, batch["id"])
        self.assertEqual((self.home / "legacy" / "old.tar.gz").read_bytes(),
                         b"\x1f\x8bold")

    def test_unknown_trash_id_is_404(self):
        for bad in ("nope", "../x", ""):
            with self.assertRaises(PathRefused) as ctx:
                memory_trash.restore(self.home, bad)
            self.assertEqual(ctx.exception.status, 404)


class DependentsSeeTheChange(TrashCase):
    def test_distill_regenerates_without_a_trashed_raw_entry(self):
        from cousin_lib import distill
        distill.distill(self.home)
        text = "".join(p.read_text() for p in
                       (self.home / "memory" / "distilled").glob("*.md"))
        self.assertIn("second", text)
        sha = memory_trash.line_sha(self.lines()[1])
        batch = memory_trash.trash_lines(
            self.home, [("memory/raw/2026-09-01.jsonl", 2, sha)])
        report = memory_trash.after_change(self.home, batch)
        self.assertTrue(report["distilled"])
        text = "".join(p.read_text() for p in
                       (self.home / "memory" / "distilled").glob("*.md"))
        self.assertNotIn("second", text)

    def test_search_no_longer_finds_a_trashed_file(self):
        from cousin_lib import memory_search
        (self.home / "notes" / "n.md").write_text("# zebracorn notes\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FRAMEWORK_ROOT", None)
            hits, _ = memory_search.search("zebracorn", home=self.home)
            self.assertTrue(hits)
            memory_trash.trash_file(self.home, "notes/n.md")
            hits, _ = memory_search.search("zebracorn", home=self.home)
            self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()


class TrashCli(TrashCase):
    def _main(self, argv):
        import contextlib
        import io
        from cousin_lib.memory import memory_main
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(self.home)}), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = memory_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_list_and_restore_by_cli(self):
        rc, out, _ = self._main(["trash"])
        self.assertEqual((rc, out.strip()), (0, "trash is empty"))
        batch = memory_trash.trash_file(self.home, "notes/n.md", by="ana")
        rc, out, _ = self._main(["trash", "list"])
        self.assertEqual(rc, 0)
        self.assertIn(batch["id"], out)
        self.assertIn("notes/n.md", out)
        rc, out, _ = self._main(["trash", "restore", batch["id"]])
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.home / "notes" / "n.md").is_file())
        rc, _, err = self._main(["trash", "restore", batch["id"]])
        self.assertEqual(rc, 1)
        self.assertIn("unknown trash id", err)
