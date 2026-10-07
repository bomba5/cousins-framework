"""#250: build outputs as rows (path, sha256, size, job, commit), and a
check that the file is still the recorded one."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
from unittest import mock

from cousin_lib import artifacts
from tests._hermetic import HermeticCase


class ArtifactCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        home = self.root / "cousins" / "wren"
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root), "COUSIN_HOME": str(home)})
        p.start()
        self.addCleanup(p.stop)
        self.out = self.root / "build"
        self.out.mkdir()
        self.image = self.out / "image.bin"
        self.image.write_bytes(b"firmware v1")

    def main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = artifacts.artifact_main(argv)
        return rc, out.getvalue()


class TestArtifacts(ArtifactCase):
    def test_add_records_the_checksum_size_job_and_commit(self):
        rc, out = self.main(["add", str(self.image), "--job", "7", "--commit", "abc123",
                             "--note", "rev A image", "--json"])
        self.assertEqual(rc, 0)
        row = json.loads(out)
        self.assertEqual(row["sha256"], artifacts.sha256_of(self.image))
        self.assertEqual((row["size"], row["job_id"], row["git_commit"], row["created_by"]),
                         (11, 7, "abc123", "wren"))

    def test_verify_says_ok_changed_or_missing(self):
        row = artifacts.add(self.image, created_by="wren")
        self.assertEqual(artifacts.verify(row), "ok")
        self.assertEqual(self.main(["verify", str(row["id"])])[0], 0)
        self.image.write_bytes(b"firmware v2")
        self.assertEqual(artifacts.verify(row), "changed")
        self.assertEqual(self.main(["verify", str(row["id"])])[0], 1)
        self.image.unlink()
        self.assertEqual(artifacts.verify(row), "missing")

    def test_list_filters_and_verifies(self):
        a = artifacts.add(self.image, created_by="wren", job_id=3)
        other = self.out / "other.bin"
        other.write_bytes(b"x")
        artifacts.add(other, created_by="sam")
        self.assertEqual([r["id"] for r in artifacts.list_rows(job_id=3)], [a["id"]])
        self.assertEqual(len(artifacts.list_rows(path=str(self.out) + "/")), 2)
        rc, out = self.main(["list", "--mine", "--verify", "--json"])
        rows = json.loads(out)
        self.assertEqual([(r["created_by"], r["state"]) for r in rows], [("wren", "ok")])

    def test_a_directory_or_missing_path_is_refused(self):
        self.assertEqual(self.main(["add", str(self.out)])[0], 2)
        self.assertEqual(self.main(["add", str(self.out / "nope")])[0], 2)
