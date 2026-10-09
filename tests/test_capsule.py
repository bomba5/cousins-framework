"""Reasoning capsules, retired in 3.50.0 (meeting 11 F): the memory tool's
decide records the same; `cousin-reason capsule` refuses and names it, the
boot packet no longer reads capsules, and the old ones stay listable
read-only for one release."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from cousin_lib import boot, capsule
from tests._hermetic import HermeticCase


class CapsuleCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        capsule.capsules_path(self.home).write_text("".join(json.dumps(e) + "\n" for e in (
            {"id": "c1", "conclusion": "older", "evidence": ["a"], "confidence": "low"},
            {"id": "c2", "conclusion": "keep backups 30 days", "evidence": ["audits"],
             "confidence": "high", "topic": "retention"})))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = capsule.reason_main(["--home", str(self.home), *argv])
        return rc, out.getvalue(), err.getvalue()


class TestRetired(CapsuleCase):
    def test_writing_a_capsule_refuses_and_names_decide(self):
        rc, _out, err = self.run_cli("capsule", "--conclusion", "x", "--evidence", "y")
        self.assertEqual(rc, 2)
        self.assertIn("decide", err)
        self.assertEqual(len(capsule.list_capsules(self.home)), 2)   # nothing written

    def test_the_old_ones_are_listed_newest_first(self):
        self.assertEqual([c["id"] for c in capsule.list_capsules(self.home)], ["c2", "c1"])
        rc, out, _err = self.run_cli("list", "--n", "1")
        self.assertEqual(rc, 0)
        self.assertIn("keep backups 30 days", out)
        self.assertNotIn("older", out)

    def test_the_boot_packet_no_longer_reads_them(self):
        self.assertNotIn("keep backups 30 days", boot._memories(self.home, 20000))


if __name__ == "__main__":
    unittest.main()
