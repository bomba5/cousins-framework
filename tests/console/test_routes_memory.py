"""The memory tree: shared/*.md and each cousin's memory/*.md with
size, age and preview; read-only."""
import unittest

from tests.console._harness import ConsoleCase


class TestMemoryTree(ConsoleCase):
    def test_tree_shape(self):
        home = self.cousin("wren")
        (home / "memory" / "b.md").write_text("beta " * 200)
        (home / "memory" / "a.md").write_text("alpha")
        (home / "memory" / "skip.txt").write_text("no")
        self.cousin("toki")
        (self.root / "cousins" / "toki" / "memory").rmdir()
        (self.root / "shared").mkdir()
        (self.root / "shared" / "project_x.md").write_text("shared body")
        self.serve()
        status, body = self.get("/api/memory")
        self.assertEqual(status, 200)
        tree = body["tree"]
        self.assertEqual(set(tree), {"shared/", "wren/", "toki/"})
        self.assertEqual(list(tree["wren/"]), ["a.md", "b.md"])
        self.assertEqual(tree["wren/"]["a.md"]["size"], 5)
        self.assertEqual(tree["wren/"]["a.md"]["preview"], "alpha")
        self.assertEqual(len(tree["wren/"]["b.md"]["preview"]), 400)
        self.assertLessEqual(tree["wren/"]["a.md"]["updated"], 5)
        self.assertEqual(tree["toki/"], {})
        self.assertEqual(tree["shared/"]["project_x.md"]["preview"],
                         "shared body")

    def test_the_first_fifty_files_only(self):
        home = self.cousin("wren")
        for i in range(60):
            (home / "memory" / ("f%02d.md" % i)).write_text("x")
        self.serve()
        _, body = self.get("/api/memory")
        self.assertEqual(len(body["tree"]["wren/"]), 50)


if __name__ == "__main__":
    unittest.main()
