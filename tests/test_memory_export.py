"""Memory export and import move a cousin's memory byte for byte.

An entry's id is a sha1 over its own stored timestamp, topic and content,
and obsolete marks, `derived_from` hops and dreaming journals name entries
by it. The round trip below is the contract: every raw file arrives
byte-identical, every id computes the same, and `why` and `validity` give
the same answers in both homes. The refusals are the other half: a bundle
that does not match its manifest, a home that already has raw memory
without --merge, an archive of the same name with other bytes.
"""
import contextlib
import gzip
import hashlib
import io
import json
import pathlib
import tarfile
import tempfile
from datetime import datetime, timedelta

from cousin_lib import distill, memory, memory_export, raw_fold
from tests._hermetic import HermeticCase


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = memory.memory_main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _tree(home, sub="memory/raw"):
    """{relative path: bytes} of every file under home/sub."""
    base = pathlib.Path(home)
    return {p.relative_to(base).as_posix(): p.read_bytes()
            for p in sorted((base / sub).rglob("*")) if p.is_file()}


def _retar(src, dst, mutate):
    """Copy a bundle, letting `mutate` change its {name: bytes}."""
    with tarfile.open(src, "r:gz") as tar:
        blobs = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    mutate(blobs)
    with tarfile.open(dst, "w:gz") as tar:
        for name, data in blobs.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class ExportCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.bundle = self.root / "wren-memory.tar.gz"

    def home(self, slug):
        home = self.root / "cousins" / slug
        for sub in ("data", "memory"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text('[cousin]\nslug = "%s"\n' % slug)
        return home

    def write_day(self, home, day, lines, *, newline_at_end=True):
        """Raw lines as given (already serialized), so the test owns
        every byte of the day file."""
        memory.ensure_layout(home)
        text = "\n".join(lines) + ("\n" if newline_at_end else "")
        with open(memory.raw_dir(home) / ("%s.jsonl" % day), "a") as fh:
            fh.write(text)

    def ids(self, home, topic):
        return [r["id"] for r in memory.validity(home) if r["topic"] == topic]

    def source(self):
        """Wren's home: two old days folded into a monthly archive (one entry
        derived from the other), hot entries derived from archived ones, an
        entry-level obsolete mark, a decision, a line whose bytes no
        serializer would reproduce, a last line with no newline, a dream
        journal naming an entry, a knowledge file, distilled views, and the
        indexes an export leaves out."""
        home = self.home("wren")
        dawn = {"timestamp": "2020-01-05T06:00:00+00:00", "topic": "kestrel",
                "content": "seen on the roof at dawn", "truth_level": "L2_TOOL",
                "source": "remember"}
        self.write_day(home, "2020-01-05", [json.dumps(dawn)])
        dusk = {"timestamp": "2020-01-06T18:00:00+00:00", "topic": "kestrel",
                "content": "seen on the roof at dusk", "truth_level": "L3_COUSIN_CONCLUSION",
                "source": "remember", "derived_from": [memory.entry_id(dawn)]}
        self.write_day(home, "2020-01-06", [json.dumps(dusk)])
        raw_fold.fold_raw(home, keep_days=30)
        self.assertTrue((memory.raw_dir(home) / "archive" / "2020-01.jsonl.gz").exists())
        a, b = memory.entry_id(dawn), memory.entry_id(dusk)
        memory.remember(home, "kestrel-nest", "nests on the roof", derived_from=[a, b])
        memory.remember(home, "roof", "the roof leaks over the porch")
        memory.remember(home, "roof", "the roof was fixed by Sam")
        leak = self.ids(home, "roof")[0]
        memory.mark_obsolete(home, "roof", "Sam fixed it", entry=leak)
        memory.decide(home, "porch", "keep the porch light on", "Priya comes home late",
                      derived_from=[b])
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        odd = ('{"timestamp": "%sT09:00:00+00:00",  "topic":"toki" , "content": "café'
               ' at the corner",   "truth_level": "L2_TOOL"}' % yesterday)
        self.write_day(home, yesterday, [odd, '{"topic": "toki", "content": "no newline"'
                                              ', "timestamp": "%sT10:00:00+00:00"}' % yesterday],
                       newline_at_end=False)
        (home / "memory" / "upkeep.md").write_text("# Upkeep\n\nDescale every 200 shots.\n")
        distill.distill(home)
        nest = self.ids(home, "kestrel-nest")[0]
        journal = home / "data" / "dreams" / "journal"
        journal.mkdir(parents=True)
        (journal / "20261003T070531-c8b94d.jsonl").write_text(
            json.dumps({"op": "remember", "entry": nest, "derived_from": [a, b]}) + "\n")
        (home / "data" / "dreams" / "2026-10-03.jsonl").write_text(
            json.dumps({"pass": "20261003T070531-c8b94d", "changes": [nest]}) + "\n")
        (home / "memory" / ".dream-ledger.json").write_text('{"cursor": 7}\n')
        (home / "memory" / "fts_index.db").write_bytes(b"SQLite format 3\x00")
        (home / "memory" / ".recall-log.jsonl").write_text('{"q": "roof"}\n')
        (home / "data" / ".decisions-backfilled").write_text("backfilled 1\n")
        return home

    def export(self, home, out=None):
        code, stdout, err = _run("--home", str(home), "export", "--out",
                                 str(out or self.bundle))
        self.assertEqual(code, 0, err)
        return stdout

    def manifest(self, bundle=None):
        with tarfile.open(bundle or self.bundle, "r:gz") as tar:
            return json.loads(tar.extractfile("MANIFEST.json").read())


class TestRoundTrip(ExportCase):
    def test_an_export_imported_into_an_empty_home_is_the_same_memory(self):
        wren = self.source()
        self.export(wren)
        testa = self.home("testa")
        code, out, err = _run("import", str(self.bundle), "--home", str(testa), "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("distilled views regenerated", out)

        # every raw file, archives included, byte-identical
        self.assertEqual(_tree(wren), _tree(testa))
        # and every file the manifest lists
        for row in self.manifest()["files"]:
            self.assertEqual((wren / row["path"]).read_bytes(),
                             (testa / row["path"]).read_bytes(), row["path"])
        # every id computes the same
        self.assertEqual([memory.entry_id(e) for e in memory._all_raw(wren)],
                         [memory.entry_id(e) for e in memory._all_raw(testa)])
        # marks and derivations resolve the same way
        self.assertEqual(memory.validity(wren), memory.validity(testa))
        for eid in [r["id"] for r in memory.validity(wren)]:
            self.assertEqual(memory.why(wren, eid), memory.why(testa, eid))
        nest = self.ids(testa, "kestrel-nest")[0]
        hops = memory.why(testa, nest)["derived_from"]
        self.assertEqual(len(hops), 2)
        self.assertFalse(any(h.get("missing") for h in hops))   # resolved into the archive
        leak = self.ids(testa, "roof")[0]
        self.assertIsNotNone(memory.why(testa, leak)["retired_by"])
        # the dream journal names an entry the new home has
        line = (testa / "data/dreams/journal/20261003T070531-c8b94d.jsonl").read_text()
        self.assertIn(json.loads(line)["entry"], {r["id"] for r in memory.validity(testa)})
        # the distiller, run on the same raw, writes the same views
        self.assertEqual(_tree(wren, "memory/distilled"), _tree(testa, "memory/distilled"))
        # what is rebuilt did not move
        for rel in ("memory/fts_index.db", "memory/.recall-log.jsonl",
                    "data/.decisions-backfilled"):
            self.assertFalse((testa / rel).exists(), rel)

    def test_the_manifest_lists_every_file_with_its_sum_and_what_stayed_behind(self):
        wren = self.source()
        self.export(wren)
        manifest = self.manifest()
        self.assertEqual(manifest["format"], memory_export.FORMAT)
        self.assertEqual(manifest["source"], "wren")
        self.assertTrue(manifest["framework_version"])
        self.assertTrue(datetime.fromisoformat(manifest["created_at"]))
        paths = {row["path"]: row for row in manifest["files"]}
        for rel in ("memory/raw/archive/2020-01.jsonl.gz", "memory/raw/2020-01-digest.jsonl",
                    "memory/upkeep.md", "memory/distilled/decisions.md",
                    "memory/.dream-ledger.json", "data/dreams/2026-10-03.jsonl",
                    "data/dreams/journal/20261003T070531-c8b94d.jsonl",
                    "data/decisions.jsonl"):
            self.assertIn(rel, paths)
            data = (wren / rel).read_bytes()
            self.assertEqual(paths[rel]["size"], len(data))
            self.assertEqual(paths[rel]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(paths["memory/raw/archive/2020-01.jsonl.gz"]["kind"], "archive")
        self.assertEqual(paths["memory/raw/2020-01-digest.jsonl"]["kind"], "lines")
        excluded = {e["path"]: e["why"] for e in manifest["excluded"]}
        for rel in ("memory/fts_index.db", "memory/.recall-log.jsonl", "memory/.last-distill",
                    "data/.decisions-backfilled"):
            self.assertIn(rel, excluded)
            self.assertTrue(excluded[rel])
        self.assertFalse(set(excluded) & set(paths))

    def test_the_archive_travels_as_its_own_compressed_bytes(self):
        wren = self.source()
        self.export(wren)
        with tarfile.open(self.bundle, "r:gz") as tar:
            inner = tar.extractfile("memory/raw/archive/2020-01.jsonl.gz").read()
        self.assertEqual(inner, (wren / "memory/raw/archive/2020-01.jsonl.gz").read_bytes())
        self.assertIn(b"seen on the roof at dawn", gzip.decompress(inner))

    def test_an_export_never_overwrites(self):
        wren = self.source()
        self.bundle.write_text("Kestrel's notes")
        code, _out, err = _run("--home", str(wren), "export", "--out", str(self.bundle))
        self.assertEqual(code, 2)
        self.assertEqual(self.bundle.read_text(), "Kestrel's notes")

    def test_without_yes_an_import_writes_nothing(self):
        self.export(self.source())
        testa = self.home("testa")
        code, out, err = _run("import", str(self.bundle), "--home", str(testa))
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)
        self.assertEqual(_tree(testa, "memory"), {})


class TestRefusals(ExportCase):
    def test_one_byte_changed_in_an_archive_is_refused_by_the_sum(self):
        self.export(self.source())
        tampered = self.root / "tampered.tar.gz"

        def flip(blobs):
            data = bytearray(blobs["memory/raw/archive/2020-01.jsonl.gz"])
            data[len(data) // 2] ^= 0x01
            blobs["memory/raw/archive/2020-01.jsonl.gz"] = bytes(data)
        _retar(self.bundle, tampered, flip)
        testa = self.home("testa")
        code, _out, err = _run("import", str(tampered), "--home", str(testa), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("memory/raw/archive/2020-01.jsonl.gz: sha256", err)
        self.assertEqual(_tree(testa, "memory"), {})            # nothing written

    def test_a_path_that_is_not_memory_is_refused_even_with_a_matching_sum(self):
        self.export(self.source())
        crafted = self.root / "crafted.tar.gz"

        def smuggle(blobs):
            body = b"# Mallory was here\n"
            manifest = json.loads(blobs["MANIFEST.json"])
            for rel in ("CLAUDE.md", "memory/../CLAUDE.md"):
                manifest["files"].append({"path": rel, "kind": "file", "size": len(body),
                                          "sha256": hashlib.sha256(body).hexdigest()})
                blobs[rel] = body
            blobs["MANIFEST.json"] = json.dumps(manifest).encode()
        _retar(self.bundle, crafted, smuggle)
        testa = self.home("testa")
        code, _out, err = _run("import", str(crafted), "--home", str(testa), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("not a memory path", err)
        self.assertFalse((testa / "CLAUDE.md").exists())

    def test_a_home_with_raw_memory_is_refused_without_merge(self):
        self.export(self.source())
        testa = self.home("testa")
        memory.remember(testa, "upkeep", "Toki descales on Sundays")
        before = _tree(testa)
        code, _out, err = _run("import", str(self.bundle), "--home", str(testa), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("--merge", err)
        self.assertEqual(_tree(testa), before)

    def test_an_archive_of_the_same_name_with_other_bytes_is_refused_not_merged(self):
        self.export(self.source())
        testa = self.home("testa")
        self.write_day(testa, "2020-01-09", [json.dumps(
            {"timestamp": "2020-01-09T08:00:00+00:00", "topic": "kestrel",
             "content": "Testa saw it too"})])
        raw_fold.fold_raw(testa, keep_days=30)
        before = _tree(testa)
        code, _out, err = _run("import", str(self.bundle), "--home", str(testa),
                               "--merge", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("archives are never merged", err)
        self.assertEqual(_tree(testa), before)


class TestMerge(ExportCase):
    def test_merge_appends_new_lines_byte_for_byte_and_twice_adds_nothing(self):
        wren = self.source()
        self.export(wren)
        testa = self.home("testa")
        memory.remember(testa, "upkeep", "Toki descales on Sundays")
        own = _tree(testa)
        code, out, err = _run("import", str(self.bundle), "--home", str(testa),
                              "--merge", "--yes")
        self.assertEqual(code, 0, err)
        after = _tree(testa)
        # the home's own lines are untouched, at the head of their files
        for rel, data in own.items():
            self.assertTrue(after[rel].startswith(data), rel)
        # every line of Wren's raw is in Testa's raw, as its bytes
        held = set()
        for rel, data in after.items():
            raw = gzip.decompress(data) if rel.endswith(".gz") else data
            held.update(raw.split(b"\n"))
        for rel, data in _tree(wren).items():
            raw = gzip.decompress(data) if rel.endswith(".gz") else data
            for line in raw.split(b"\n"):
                self.assertIn(line, held, (rel, line))
        wren_ids = {memory.entry_id(e) for e in memory._all_raw(wren)}
        self.assertLessEqual(wren_ids, {memory.entry_id(e) for e in memory._all_raw(testa)})

        code, out, err = _run("import", str(self.bundle), "--home", str(testa),
                              "--merge", "--yes", "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual({r["action"] for r in report["rows"]} - {"skip", "kept"}, set())
        self.assertEqual(_tree(testa), after)                   # nothing added

    def test_a_line_differing_only_in_whitespace_is_a_different_line(self):
        """The importer compares bytes, never parsed JSON. Deciding that two
        different byte strings hold "the same entry" would need a canonical
        form, which is a re-serialization, the one thing a mover must not
        do; and a judgment that drops a line can lose bytes some reader
        depends on. So the twin lands as its own line, appended unchanged.
        Its id is the same (it hashes the parsed timestamp, topic and
        content), so marks and derivations naming it still resolve."""
        entry = {"timestamp": "2026-09-30T08:00:00+00:00", "topic": "kestrel",
                 "content": "nests on the roof", "truth_level": "L2_TOOL"}
        spaced = json.dumps(entry)
        tight = json.dumps(entry, separators=(",", ":"))
        self.assertNotEqual(spaced, tight)
        self.assertEqual(json.loads(spaced), json.loads(tight))
        wren = self.home("wren")
        self.write_day(wren, "2026-09-30", [tight])
        self.export(wren)
        testa = self.home("testa")
        self.write_day(testa, "2026-09-30", [spaced])
        code, _out, err = _run("import", str(self.bundle), "--home", str(testa),
                               "--merge", "--yes")
        self.assertEqual(code, 0, err)
        day = (testa / "memory/raw/2026-09-30.jsonl").read_bytes()
        self.assertEqual(day, (spaced + "\n" + tight + "\n").encode())
        self.assertEqual({memory.entry_id(json.loads(l)) for l in day.splitlines()},
                         {memory.entry_id(entry)})

    def test_a_target_line_with_no_newline_is_not_joined_by_the_append(self):
        wren = self.home("wren")
        new = json.dumps({"timestamp": "2026-09-30T09:00:00+00:00", "topic": "porch",
                          "content": "Priya fixed the light"})
        self.write_day(wren, "2026-09-30", [new])
        self.export(wren)
        testa = self.home("testa")
        old = json.dumps({"timestamp": "2026-09-30T08:00:00+00:00", "topic": "porch",
                          "content": "the light flickers"})
        self.write_day(testa, "2026-09-30", [old], newline_at_end=False)
        code, _out, err = _run("import", str(self.bundle), "--home", str(testa),
                               "--merge", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual((testa / "memory/raw/2026-09-30.jsonl").read_bytes(),
                         (old + "\n" + new + "\n").encode())

    def test_a_whole_file_that_differs_stays_as_the_home_has_it(self):
        wren = self.source()
        self.export(wren)
        testa = self.home("testa")
        (testa / "memory" / "upkeep.md").write_text("# Upkeep\n\nTesta's own notes.\n")
        code, out, err = _run("import", str(self.bundle), "--home", str(testa), "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("kept", out)
        self.assertEqual((testa / "memory" / "upkeep.md").read_text(),
                         "# Upkeep\n\nTesta's own notes.\n")


if __name__ == "__main__":
    import unittest
    unittest.main()
