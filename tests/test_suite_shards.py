"""The CI suite split (.github/scripts/suite.py): the split is
deterministic and puts each module in exactly one shard, and the check
that reads one run's shard records catches a shard that is missing,
failed, or ran a module twice or not at all."""
import importlib.util
import json
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / ".github" / "scripts" / "suite.py"


def _load():
    spec = importlib.util.spec_from_file_location("suite_ci", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


suite = _load()
NAMES = ["test_%02d" % i for i in range(20)] + ["console.test_x", "gate.test_self"]


class TestSplit(unittest.TestCase):
    def test_every_module_lands_in_exactly_one_shard(self):
        timings = {n: float(i % 7) for i, n in enumerate(NAMES)}
        for count in (1, 2, 3, 5):
            shards, _ = suite.split(NAMES, timings, count)
            self.assertEqual(len(shards), count)
            self.assertEqual(suite.coverage_problems(NAMES, shards), [])
            self.assertEqual(sorted(n for s in shards for n in s), sorted(NAMES))

    def test_the_same_input_gives_the_same_split_in_any_order(self):
        timings = {n: 1.0 + (i * 13) % 5 for i, n in enumerate(NAMES)}
        first = suite.split(NAMES, timings, 3)
        self.assertEqual(suite.split(list(reversed(NAMES)), dict(reversed(timings.items())), 3),
                         first)

    def test_the_slow_modules_are_spread_out(self):
        timings = dict.fromkeys(NAMES, 1.0)
        timings.update({"test_00": 100.0, "test_01": 90.0, "test_02": 80.0})
        shards, load = suite.split(NAMES, timings, 3)
        self.assertEqual(sorted(sum(n in s for n in ("test_00", "test_01", "test_02"))
                                for s in shards), [1, 1, 1])
        self.assertEqual(max(load), 100.0)       # the slowest module alone sets the floor

    def test_a_module_without_a_time_weighs_the_median(self):
        timings = {"a": 1.0, "b": 2.0, "c": 30.0}
        _, load = suite.split(["a", "b", "c", "new"], timings, 1)
        self.assertEqual(load, [35.0])

    def test_a_module_in_none_or_two_shards_or_not_discovered_is_named(self):
        problems = suite.coverage_problems(["a", "b", "c"], [["a", "b"], ["b", "x"]])
        self.assertEqual(problems, ["b is in shards [1, 2]", "c is in no shard",
                                    "x is in shard [2] but was not discovered"])


class TestVerify(unittest.TestCase):
    def records(self):
        found = ["a", "b", "c", "d"]
        return [{"shard": 1, "of": 2, "discovered": found, "modules": ["a", "c"], "ok": True},
                {"shard": 2, "of": 2, "discovered": found, "modules": ["b", "d"], "ok": True}]

    def test_a_module_that_skips_itself_has_the_id_check_expects(self):
        # check() lets these through: a module skipped whole at import (an
        # optional dependency missing) is not a module that failed to load
        import tempfile, sys as _sys
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "test_gone.py").write_text(
                "import unittest\nraise unittest.SkipTest('no optional dependency')\n")
            _sys.path.insert(0, tmp)
            try:
                found = unittest.defaultTestLoader.discover(tmp, pattern="test*.py", top_level_dir=tmp)
            finally:
                _sys.path.remove(tmp)
                _sys.modules.pop("test_gone", None)
            ids = [t.id() for s in found for t in s]
        self.assertEqual(ids, [suite.SKIPPED_MODULE + "test_gone"])

    def test_a_shard_with_a_skipped_module_writes_its_record(self):
        # unittest drops each test from its suite once run, so the record
        # must not read the strays after the run
        import tempfile
        from unittest import mock
        skipped = unittest.TestSuite([unittest.loader._make_skipped_test(
            "test_gone", unittest.SkipTest("no optional dependency"), unittest.TestSuite)])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(suite, "discover", return_value=({}, [skipped], [])):
            out = pathlib.Path(tmp, "r.json")
            args = mock.Mock(shard=1, of=1, result=str(out))
            self.assertEqual(suite.cmd_run(args), 0)
            record = json.loads(out.read_text())
        self.assertEqual(record["strays"], [suite.SKIPPED_MODULE + "test_gone"])
        self.assertEqual(record["skipped"], 1)

    def test_a_clean_run_has_no_problems(self):
        self.assertEqual(suite.verify(self.records(), 2), [])

    def test_a_missing_shard_fails(self):
        problems = suite.verify(self.records()[:1], 2)
        self.assertIn("shard records [1], expected 1..2 once each", problems)
        self.assertIn("b is in no shard", problems)

    def test_records_from_two_pythons_fail(self):
        records = self.records()
        records[0]["python"], records[1]["python"] = "3.11", "3.12"
        self.assertEqual(suite.verify(records, 2),
                         ["the records come from more than one Python (3.11, 3.12)"])

    def test_a_failed_shard_fails(self):
        records = self.records()
        records[1].update(ok=False, failures=1, errors=0)
        self.assertEqual(suite.verify(records, 2), ["shard 2 failed (1 failures, 0 errors)"])

    def test_a_module_run_twice_fails(self):
        records = self.records()
        records[1]["modules"] = ["a", "b", "d"]
        self.assertEqual(suite.verify(records, 2), ["a is in shards [1, 2]"])

    def test_shards_that_discovered_different_modules_fail(self):
        records = self.records()
        records[1]["discovered"] = ["a", "b", "c"]
        self.assertIn("the shards discovered different modules", suite.verify(records, 2))


class TestCommittedFiles(unittest.TestCase):
    def test_the_timings_file_is_module_seconds(self):
        data = json.loads((_REPO / ".github" / "test-timings.json").read_text())
        self.assertGreater(len(data), 100)
        for name, seconds in data.items():
            self.assertRegex(name, r"^([a-z_]+\.)*test_\w+$")
            self.assertIsInstance(seconds, (int, float))
        self.assertEqual(suite.read_timings(), {k: float(v) for k, v in data.items()})

    def test_the_workflow_cuts_as_many_shards_as_its_matrix_has(self):
        text = (_REPO / ".github" / "workflows" / "ci.yml").read_text()
        matrix = re.search(r"^\s+shard: \[([\d, ]+)\]$", text, re.M)
        count = len(matrix.group(1).split(","))
        self.assertEqual(set(re.findall(r"--of (\d+)", text)), {str(count)})
        self.assertIn("shard ${{ matrix.shard }}/%d" % count, text)
        # the names branch protection requires
        self.assertIn("    name: test (${{ matrix.python-version }})\n", text)
        self.assertIn("COUSIN_REQUIRE_TAGS", text)
        self.assertIn("fetch-depth: 0", text)


if __name__ == "__main__":
    unittest.main()
