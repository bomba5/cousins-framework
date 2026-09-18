"""The cousin file explorer over HTTP: behind the console login, one
directory level per call, `.secrets/` never listed, read or
downloaded, links out of the home listed but not followed, binary
files never returned as text."""
import os
import unittest

from cousin_lib.console import auth
from tests.console._harness import ConsoleCase


class FilesCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.home = self.cousin("wren")
        (self.home / "notes").mkdir()
        (self.home / "notes" / "long.md").write_text("# Long\n\nbody\n")
        (self.home / ".secrets").mkdir()
        (self.home / ".secrets" / "key").write_text("s3cret")


class FileExplorer(FilesCase):
    def test_tree_hides_secrets_and_dotfiles_behind_a_toggle(self):
        (self.home / ".dot").write_text("d")
        self.serve()
        status, body = self.get("/api/cousins/wren/files")
        self.assertEqual(status, 200)
        names = [e["name"] for e in body["entries"]]
        self.assertIn("notes", names)
        self.assertNotIn(".secrets", names)
        self.assertNotIn(".dot", names)
        _, body = self.get("/api/cousins/wren/files?hidden=1")
        names = [e["name"] for e in body["entries"]]
        self.assertIn(".dot", names)
        self.assertNotIn(".secrets", names)
        self.assertEqual(self.get("/api/cousins/wren/files?path=.secrets")[0],
                         404)

    def test_read_pages_text_and_refuses_binary(self):
        (self.home / "big.log").write_text(
            "".join("l%d\n" % i for i in range(5000)))
        (self.home / "blob.bin").write_bytes(b"\x00\x01\x02")
        self.serve()
        _, body = self.get("/api/cousins/wren/files/read?path=big.log"
                           "&start=4001&count=500")
        self.assertEqual(body["lines"][0], "l4000")
        self.assertEqual(body["total_lines"], 5000)
        self.assertTrue(body["more"])
        _, body = self.get("/api/cousins/wren/files/read?path=blob.bin")
        self.assertEqual(body["kind"], "binary")
        self.assertNotIn("text", body)

    def test_download_streams_bytes_as_an_attachment(self):
        (self.home / "blob.bin").write_bytes(b"\x00\x01\x02")
        (self.home / "p.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
        self.serve()
        status, hdrs, body = self.get(
            "/api/cousins/wren/files/download?path=blob.bin", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x00\x01\x02")
        self.assertIn("attachment", hdrs.get("Content-Disposition", ""))
        self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")
        status, hdrs, body = self.get(
            "/api/cousins/wren/files/download?path=p.png", raw=True)
        self.assertEqual(hdrs.get("Content-Type"), "image/png")
        self.assertIn("inline", hdrs.get("Content-Disposition", ""))
        status, _, body = self.get(
            "/api/cousins/wren/files/download?path=.secrets/key", raw=True)
        self.assertEqual(status, 404)
        self.assertNotIn(b"s3cret", body)

    def test_a_link_out_of_the_home_is_not_followed(self):
        os.symlink(self.root / "config", self.home / "escape")
        self.serve()
        _, body = self.get("/api/cousins/wren/files")
        row = [e for e in body["entries"] if e["name"] == "escape"][0]
        self.assertTrue(row["outside"])
        self.assertEqual(self.get("/api/cousins/wren/files?path=escape")[0],
                         403)


class BehindTheLogin(FilesCase):
    def test_every_route_is_401_without_a_session(self):
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.serve()
        for path in ("/api/cousins/wren/files",
                     "/api/cousins/wren/files/read?path=notes/long.md",
                     "/api/cousins/wren/files/download?path=notes/long.md"):
            self.assertEqual(self.get(path)[0], 401, path)


if __name__ == "__main__":
    unittest.main()
