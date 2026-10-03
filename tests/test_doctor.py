"""cousin-doctor (cousin_lib/doctor.py): the homes check, over temporary
roots only. Modes are set on the temporary homes by hand; nothing else
is touched."""
import contextlib
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from cousin_lib import doctor
from tests._hermetic import HermeticCase


class _RootCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.root = self.tmp / "root"
        (self.root / "config").mkdir(parents=True)

    def home(self, slug, mode):
        home = self.root / "cousins" / slug
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text('[cousin]\nslug = "%s"\n' % slug)
        os.chmod(home, mode)
        return home

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = doctor.doctor_main(list(argv) + ["--root", str(self.root)])
        return rc, out.getvalue(), err.getvalue()


class TestHomes(_RootCase):
    def test_open_homes_are_listed_each_with_its_chmod_line(self):
        wren = self.home("wren", 0o755)
        sam = self.home("sam", 0o750)
        self.home("kit", 0o700)
        result = doctor.check_homes(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"], "2 of 3 cousin homes open to group or other")
        self.assertEqual(result["items"], [{"home": str(sam), "mode": "0750"},
                                           {"home": str(wren), "mode": "0755"}])
        self.assertEqual(result["fixes"], ["chmod 700 %s    # now 0750" % sam,
                                           "chmod 700 %s    # now 0755" % wren])

    def test_any_group_or_other_bit_counts(self):
        for mode in (0o710, 0o701, 0o720, 0o704, 0o740):
            with self.subTest(mode="%o" % mode):
                home = self.home("m%o" % mode, mode)
                result = doctor.check_homes(self.root)
                self.assertIn({"home": str(home), "mode": "%04o" % mode}, result["items"])

    def test_all_closed_is_ok_and_says_what_it_does_not_cover(self):
        self.home("wren", 0o700)
        self.home("sam", 0o700)
        result = doctor.check_homes(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "2 cousin homes, none open to group or other")
        self.assertEqual(result["fixes"], [])
        self.assertIn("not one cousin to another", " ".join(result["notes"]))

    def test_no_homes_is_ok(self):
        result = doctor.check_homes(self.root)
        self.assertTrue(result["ok"])
        self.assertIn("no cousin homes", result["summary"])

    def test_a_directory_without_cousin_toml_is_not_a_home(self):
        stray = self.root / "cousins" / "stray"
        stray.mkdir(parents=True)
        os.chmod(stray, 0o755)
        self.assertEqual(doctor.cousin_homes(self.root), [])
        self.assertTrue(doctor.check_homes(self.root)["ok"])

    def test_a_path_with_a_space_is_quoted_for_the_shell(self):
        self.root = self.tmp / "my root"
        (self.root / "config").mkdir(parents=True)
        home = self.home("wren", 0o755)
        self.assertEqual(doctor.check_homes(self.root)["fixes"],
                         ["chmod 700 '%s'    # now 0755" % home])

    def test_the_check_changes_no_mode(self):
        home = self.home("wren", 0o755)
        doctor.check_homes(self.root)
        self.main()
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o755)


class TestClosedAbove(_RootCase):
    def test_a_closed_directory_above_is_named_in_a_note(self):
        os.chmod(self.tmp, 0o755)
        outer = self.tmp / "outer"
        outer.mkdir(mode=0o700)
        os.chmod(outer, 0o700)
        self.root = outer / "root"
        (self.root / "config").mkdir(parents=True)
        self.home("wren", 0o755)
        self.assertEqual(doctor.closed_above(self.root / "cousins"), outer)
        notes = " ".join(doctor.check_homes(self.root)["notes"])
        self.assertIn("%s (mode 0700) already keeps other users out" % outer, notes)
        self.assertIn("changes little today", notes)

    def test_group_search_alone_is_not_closed(self):
        os.chmod(self.tmp, 0o710)
        self.assertNotEqual(doctor.closed_above(self.root / "cousins"), self.tmp)

    def test_no_note_without_a_closed_directory_above(self):
        self.home("wren", 0o755)
        with mock.patch.object(doctor, "closed_above", return_value=None):
            notes = doctor.check_homes(self.root)["notes"]
        self.assertEqual(notes, [doctor.SCOPE])


class TestMain(_RootCase):
    def test_a_bad_home_prints_the_fix_and_exits_1(self):
        wren = self.home("wren", 0o755)
        with mock.patch.object(doctor, "closed_above", return_value=None):
            rc, out, _ = self.main()
        self.assertEqual(rc, 1)
        lines = out.splitlines()
        self.assertEqual(lines[0], "WARN  homes: 1 of 1 cousin home open to group or other")
        self.assertIn("        chmod 700 %s    # now 0755" % wren, lines)
        self.assertIn("      " + doctor.SCOPE, lines)

    def test_good_homes_exit_0(self):
        self.home("wren", 0o700)
        rc, out, _ = self.main("homes")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("OK    homes: 1 cousin home, none open to group or other\n"))
        self.assertNotIn("chmod", out)

    def test_json(self):
        self.home("wren", 0o750)
        rc, out, _ = self.main("--json")
        self.assertEqual(rc, 1)
        [result] = json.loads(out)
        self.assertEqual(result["check"], "homes")
        self.assertFalse(result["ok"])

    def test_an_unknown_check_is_usage(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as caught:
            doctor.doctor_main(["nosuch", "--root", str(self.root)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("no check nosuch", err.getvalue())

    def test_no_root_is_exit_2(self):
        with contextlib.chdir(self.tmp), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = doctor.doctor_main([])
        self.assertEqual(rc, 2)
        self.assertIn("no framework root", err.getvalue())


if __name__ == "__main__":
    unittest.main()
