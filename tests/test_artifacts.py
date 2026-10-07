"""#250: build outputs as rows (path, sha256, size, job, commit), and a
check that the file is still the recorded one."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
from unittest import mock

from cousin_lib import artifacts, jobs
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
        jobs.register_job(kind="shell", title="build", spawned_by="wren")   # job #1
        for _ in range(6):
            jobs.register_job(kind="shell", title="other", spawned_by="wren")
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
        job = jobs.register_job(kind="shell", title="build", spawned_by="wren")
        a = artifacts.add(self.image, created_by="wren", job_id=job)
        other = self.out / "other.bin"
        other.write_bytes(b"x")
        artifacts.add(other, created_by="sam")
        self.assertEqual([r["id"] for r in artifacts.list_rows(job_id=job)], [a["id"]])
        self.assertEqual(len(artifacts.list_rows(path=str(self.out) + "/")), 2)
        odd = self.root / "buildXv1"
        odd.mkdir()
        (odd / "a.bin").write_bytes(b"y")
        artifacts.add(odd / "a.bin", created_by="sam")
        self.assertEqual(artifacts.list_rows(path=str(self.root / "build_v1") + "/"), [])
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(len(artifacts.list_rows(path="~/build/")), 2)
        rc, out = self.main(["list", "--mine", "--verify", "--json"])
        rows = json.loads(out)
        self.assertEqual([(r["created_by"], r["state"]) for r in rows], [("wren", "ok")])

    def test_a_directory_or_missing_path_is_refused(self):
        self.assertEqual(self.main(["add", str(self.out)])[0], 2)
        self.assertEqual(self.main(["add", str(self.out / "nope")])[0], 2)

    def test_an_unknown_job_is_refused(self):
        self.assertEqual(self.main(["add", str(self.image), "--job", "99"])[0], 2)

    def test_a_file_still_being_written_is_refused(self):
        real = artifacts.sha256_of

        def grow(path):
            digest = real(path)
            with open(path, "ab") as fh:
                fh.write(b"more")
            return digest
        with mock.patch.object(artifacts, "sha256_of", grow):
            with self.assertRaisesRegex(ValueError, "still changing"):
                artifacts.add(self.image, created_by="wren")

    def test_an_unreadable_file_says_so(self):
        row = artifacts.add(self.image, created_by="wren")
        with mock.patch.object(artifacts, "sha256_of", side_effect=PermissionError("denied")):
            self.assertEqual(artifacts.verify(row), "unreadable")

    def test_quick_check_stats_without_hashing(self):
        row = artifacts.add(self.image, created_by="wren")
        with mock.patch.object(artifacts, "sha256_of", side_effect=AssertionError("hashed")):
            self.assertEqual(artifacts.verify(row, quick=True), "unchanged")
            os.utime(self.image, (1, 1))
            self.assertEqual(artifacts.verify(row, quick=True), "touched")
            self.image.write_bytes(b"firmware v22")
            self.assertEqual(artifacts.verify(row, quick=True), "changed")

    def test_rm_drops_your_own_row_only(self):
        mine = artifacts.add(self.image, created_by="wren")
        theirs = artifacts.add(self.image, created_by="sam")
        self.assertEqual(self.main(["rm", str(theirs["id"])])[0], 2)
        self.assertEqual(self.main(["rm", str(mine["id"])])[0], 0)
        self.assertIsNone(artifacts.get(mine["id"]))
        self.assertEqual(self.main(["rm", str(mine["id"])])[0], 1)
        self.assertTrue(artifacts.remove(theirs["id"]))     # the operator, outside a cousin


class TestRemote(ArtifactCase):
    SHA = "ab" * 32

    def test_a_remote_row_needs_its_measurements(self):
        self.assertEqual(self.main(["add", "/srv/out/img.bin", "--host", "buildbox"])[0], 2)
        self.assertEqual(self.main(["add", "out/img.bin", "--host", "buildbox", "--sha256", self.SHA,
                                    "--size", "5"])[0], 2)
        self.assertEqual(self.main(["add", "/srv/img.bin", "--host=-oProxyCommand=x",
                                    "--sha256", self.SHA, "--size", "5"])[0], 2)
        self.assertEqual(self.main(["add", str(self.image), "--sha256", self.SHA])[0], 2)

    def test_remote_verify_runs_only_when_asked(self):
        row = artifacts.add("/srv/out/it's.bin", created_by="wren", host="buildbox",
                            sha256=self.SHA, size=5)
        self.assertEqual(artifacts.verify(row), "unverified")
        done = mock.Mock(returncode=0, stdout=self.SHA + "  /srv/out/it's.bin\n", stderr="")
        with mock.patch.object(artifacts.subprocess, "run", return_value=done) as run:
            self.assertEqual(artifacts.verify(row, remote=True), "ok")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["buildbox", "LC_ALL=C sha256sum -- '/srv/out/it'\"'\"'s.bin'"])
        gone = mock.Mock(returncode=1, stdout="", stderr="sha256sum: x: No such file or directory")
        with mock.patch.object(artifacts.subprocess, "run", return_value=gone):
            self.assertEqual(artifacts.verify(row, remote=True), "missing")
        with mock.patch.object(artifacts.subprocess, "run", side_effect=OSError("no ssh")):
            self.assertEqual(artifacts.verify(row, remote=True), "unreachable")


class TestPrivate(ArtifactCase):
    def test_the_shared_row_keeps_only_the_label(self):
        self.assertEqual(self.main(["add", str(self.image), "--private"])[0], 2)
        self.assertEqual(self.main(["add", str(self.image), "--private", "--label", "A",
                                    "--note", "employer tree"])[0], 2)
        self.assertEqual(self.main(["add", str(self.image), "--private", "--label", "A",
                                    "--commit", "abc123"])[0], 2)
        rc, out = self.main(["add", str(self.image), "--private", "--label", "board A image",
                             "--json"])
        self.assertEqual(rc, 0)
        row = artifacts.get(json.loads(out)["id"])
        self.assertEqual((row["path"], row["host"], row["label"], row["private"]),
                         (None, None, "board A image", 1))
        self.assertNotIn(str(self.out), json.dumps(row))
        home = self.root / "cousins" / "wren"
        self.assertIn(str(self.image), (home / "data" / "artifacts-private.json").read_text())
        self.assertEqual(artifacts.list_rows(path=str(self.image)), [])

    def test_only_the_owner_verifies_it(self):
        home = self.root / "cousins" / "wren"
        row = artifacts.add(self.image, created_by="wren", private=True, label="A", home=home)
        self.assertEqual(artifacts.verify(row), "private")
        self.assertEqual(artifacts.verify(row, me="sam", home=self.root / "cousins" / "sam"),
                         "private")
        self.assertEqual(artifacts.verify(row, me="wren", home=home), "ok")
        self.assertEqual(self.main(["verify", str(row["id"])])[0], 0)
        self.assertEqual(self.main(["rm", str(row["id"])])[0], 0)
        self.assertEqual(json.loads((home / "data" / "artifacts-private.json").read_text()), {})

    def test_an_operator_rm_is_pruned_at_the_owners_next_write(self):
        home = self.root / "cousins" / "wren"
        a = artifacts.add(self.image, created_by="wren", private=True, label="A", home=home)
        artifacts.remove(a["id"])                   # the operator: no reach into the home
        b = artifacts.add(self.image, created_by="wren", private=True, label="B", home=home)
        paths = json.loads((home / "data" / "artifacts-private.json").read_text())
        self.assertEqual(list(paths), [str(b["id"])])
        self.assertEqual(artifacts.verify(a, me="wren", home=home), "unknown")
