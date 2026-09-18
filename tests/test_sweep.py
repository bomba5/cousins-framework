"""The fleet compaction sweep.

The sweep only enumerates, sequences and reports; every safety rule
lives in `cousin-memory compact` itself. Pinned here: one result per
cousin, a failure never aborts the rest, and the exit code carries
"any failed" so a service unit's journal shows the alert.
"""
import contextlib
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.sweep import sweep_compact, sweep_main


def _make_fleet(root, slugs):
    for slug in slugs:
        home = root / "cousins" / slug
        (home / "memory").mkdir(parents=True)
        (home / "data").mkdir()
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n'
            'role = "test"\n[chat]\nport = 8090\n' % (slug, slug.title()))
        (home / "MEMORY.md").write_text("# %s memory\n" % slug)


class SweepCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        _make_fleet(self.root, ["testa", "testb", "testc"])
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestSweepCompact(SweepCase):
    def test_one_result_per_cousin_in_registry_order(self):
        calls = []

        def run(home, slug, target):
            calls.append((slug, target))
            return 0, "ok"

        results = sweep_compact(self.root, target="index", run=run)
        self.assertEqual([r["slug"] for r in results],
                         ["testa", "testb", "testc"])
        self.assertEqual(calls, [("testa", "index"), ("testb", "index"),
                                 ("testc", "index")])
        self.assertTrue(all(r["ok"] for r in results))

    def test_a_failure_never_aborts_the_rest(self):
        def run(home, slug, target):
            if slug == "testb":
                raise RuntimeError("compactor exploded")
            return 0, "ok"

        results = sweep_compact(self.root, target="index", run=run)
        self.assertEqual(len(results), 3)
        failed = [r for r in results if not r["ok"]]
        self.assertEqual([r["slug"] for r in failed], ["testb"])
        self.assertIn("compactor exploded", failed[0]["output"])

    def test_nonzero_rc_marks_the_cousin_failed(self):
        def run(home, slug, target):
            return (1, "no MEMORY.md") if slug == "testc" else (0, "ok")

        results = sweep_compact(self.root, target="index", run=run)
        by_slug = {r["slug"]: r for r in results}
        self.assertFalse(by_slug["testc"]["ok"])
        self.assertEqual(by_slug["testc"]["rc"], 1)
        self.assertTrue(by_slug["testa"]["ok"])

    def test_target_both_runs_index_then_raw_per_cousin(self):
        calls = []

        def run(home, slug, target):
            calls.append((slug, target))
            return 0, "ok"

        sweep_compact(self.root, target="both", run=run)
        self.assertEqual(calls[:2], [("testa", "index"), ("testa", "raw")])
        self.assertEqual(len(calls), 6)

    def test_the_runner_gets_the_cousin_home(self):
        seen = {}

        def run(home, slug, target):
            seen[slug] = pathlib.Path(home)
            return 0, ""

        sweep_compact(self.root, target="raw", run=run)
        self.assertEqual(seen["testa"], self.root / "cousins" / "testa")

    def test_empty_fleet_is_an_empty_report(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(sweep_compact(empty, target="index",
                                           run=lambda *a: (0, "")), [])

    def test_real_runner_compacts_a_real_home(self):
        # The default runner drives cousin-memory compact out of process
        # with the cousin's home; the smallest home must come back ok.
        results = sweep_compact(self.root / "cousins" / "testa" / ".." / "..",
                                target="index")
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertTrue(r["ok"], r)
            self.assertIn('"ok": true', r["output"])


class TestSweepCli(SweepCase):
    def _main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sweep_main(argv)
        return rc, out.getvalue()

    def test_exit_zero_when_all_succeed(self):
        with mock.patch("cousin_lib.sweep._default_run",
                        lambda home, slug, target: (0, "ok")):
            rc, out = self._main(["compact", "--root", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn("3 run(s), 0 failed", out)

    def test_exit_one_when_any_failed_but_every_cousin_ran(self):
        calls = []

        def run(home, slug, target):
            calls.append(slug)
            return (2, "boom") if slug == "testa" else (0, "ok")

        with mock.patch("cousin_lib.sweep._default_run", run):
            rc, out = self._main(["compact", "--root", str(self.root),
                                  "--target", "both"])
        self.assertEqual(rc, 1)
        self.assertIn("[sweep] testa index: rc=2 boom", out)
        self.assertIn("[sweep] testa raw: rc=2 boom", out)
        self.assertIn("6 run(s), 2 failed", out)
        self.assertEqual(calls, ["testa", "testa", "testb", "testb",
                                 "testc", "testc"])

    def test_root_comes_from_the_environment_when_no_flag(self):
        with mock.patch("cousin_lib.sweep._default_run",
                        lambda home, slug, target: (0, "ok")):
            self.assertEqual(self._main(["compact"])[0], 0)

    def test_missing_root_is_a_usage_error(self):
        # Outside any checkout: inside one, the root defaults to it.
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": ""}), \
                tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            self.assertEqual(self._main(["compact"])[0], 2)


if __name__ == "__main__":
    unittest.main()
