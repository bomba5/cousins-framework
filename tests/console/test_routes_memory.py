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


class TestWhyRoute(ConsoleCase):
    """#247: the console reads a claim's provenance chain."""

    def test_the_chain_both_ways_and_the_errors(self):
        from cousin_lib import memory
        home = self.cousin("wren")
        memory.remember(home, "measure", "the NAS snapshot runs at 02:00")
        a = memory.validity(home)[0]["id"]
        memory.remember(home, "fact", "verify after 02:30", derived_from=[a])
        b = [r for r in memory.validity(home) if r["topic"] == "fact"][0]["id"]
        memory.remember(home, "plan", "check at 02:45", derived_from=[b])
        c = [r for r in memory.validity(home) if r["topic"] == "plan"][0]["id"]
        self.serve()
        status, body = self.get("/api/memory/wren/why?id=%s" % c)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["derived_from"][0]["id"], b)
        self.assertEqual(body["derived_from"][0]["built_from"][0]["id"], a)
        status, body = self.get("/api/memory/wren/why?id=%s&depth=1" % c)
        self.assertTrue(body["derived_from"][0]["more"])
        self.assertEqual(self.get("/api/memory/wren/why?id=0123456789ab")[0], 404)
        self.assertEqual(self.get("/api/memory/wren/why")[0], 400)
        self.assertEqual(self.get("/api/memory/wren/why?id=%s&depth=x" % c)[0], 400)
