#!/usr/bin/env python3
"""The test suite timed, and split into shards (.github/workflows/ci.yml).

A CI and development helper, not a user-facing tool: it ships with the
checkout, never in the image. Run it from the checkout's root.

    suite.py time [--top N] [--write]
    suite.py check [--of N]
    suite.py run --shard I [--of N] [--result FILE]
    suite.py verify [--of N] DIR

Discovery is exactly `python -m unittest discover -s tests`: the same
loader call, start directory and module names, so a shard runs each of
its modules the way the full run does.

`time` runs the whole suite and prints the N slowest test modules and
test classes (default 20); `--write` also saves every module's time to
.github/test-timings.json, the weights the shards are cut by.

`check` prints the split and fails unless every discovered module is in
exactly one shard (and discovery imported every module cleanly). `run`
runs one shard, prints its slowest modules, and with `--result` writes a
JSON record of what it ran. `verify` reads the N records of one CI run
and fails unless all N shards are there and passed, they all discovered
the same modules, and together they ran each of those modules exactly
once.

The split: modules sorted slowest first by their recorded time, each put
on the shard with the least time so far (ties to the lowest shard). A
module the timings file does not name weighs the median of those it
does. The same modules and the same file always give the same split.

Exit codes: 0 green, 1 a failed test or check, 2 bad usage.
"""
import argparse
import json
import os
import pathlib
import statistics
import sys
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
TIMINGS = REPO / ".github" / "test-timings.json"
START = "tests"
SHARDS = 3


class ModuleSuite(unittest.TestSuite):
    """One discovered module's tests, timed while they run (class and
    module fixtures included)."""

    def __init__(self, module, tests=()):
        super().__init__(tests)
        self.module = module
        self.cases = 0
        self.seconds = 0.0

    def run(self, result, debug=False):
        start = time.perf_counter()
        try:
            return super().run(result, debug)
        finally:
            self.seconds += time.perf_counter() - start


class Loader(unittest.TestLoader):
    """discover() calls loadTestsFromModule once per module (and per
    package); each answer is wrapped so it keeps its module's name."""

    def loadTestsFromModule(self, module, *args, **kwargs):
        suite = ModuleSuite(module.__name__,
                            [super().loadTestsFromModule(module, *args, **kwargs)])
        suite.cases = suite.countTestCases()
        return suite


class TimedResult(unittest.TextTestResult):
    """The text result, plus each test's wall time (setUp and tearDown
    included, setUpClass not)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.durations = {}
        self._started = {}

    def startTest(self, test):
        self._started[test.id()] = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        began = self._started.pop(test.id(), None)
        if began is not None:
            self.durations[test.id()] = time.perf_counter() - began


def discover():
    """(every module with tests by name, the suites that are not a module:
    an import that failed, a module skipped whole, discovery's errors)."""
    # the path `python -m unittest` starts with: the checkout first, not
    # this script's directory
    here = {os.path.dirname(os.path.abspath(__file__)), str(pathlib.Path(__file__).resolve().parent)}
    sys.path[:] = [str(REPO)] + [p for p in sys.path if p not in here | {str(REPO), ""}]
    os.chdir(REPO)
    loader = Loader()
    top = loader.discover(START, pattern="test*.py")
    modules, strays = {}, []
    for suite in top:
        if isinstance(suite, ModuleSuite):
            if suite.cases:
                modules[suite.module] = suite
        else:
            strays.append(suite)
    return modules, strays, list(loader.errors)


def read_timings(path=TIMINGS):
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def split(names, timings, count):
    """`count` lists of module names: slowest first, each onto the shard
    with the least time so far. Deterministic in `names` and `timings`."""
    if count < 1:
        raise ValueError("a split needs at least one shard")
    known = [timings[n] for n in names if n in timings]
    default = statistics.median(known) if known else 1.0
    weight = {n: timings.get(n, default) for n in names}
    shards = [[] for _ in range(count)]
    load = [0.0] * count
    for name in sorted(set(names), key=lambda n: (-weight[n], n)):
        i = min(range(count), key=lambda k: (load[k], k))
        shards[i].append(name)
        load[i] += weight[name]
    return [sorted(s) for s in shards], load


def coverage_problems(discovered, shards):
    """Every discovered module in exactly one shard, and nothing else."""
    seen = {}
    for i, shard in enumerate(shards, 1):
        for name in shard:
            seen.setdefault(name, []).append(i)
    problems = []
    for name in sorted(discovered):
        where = seen.get(name, [])
        if len(where) != 1:
            problems.append("%s is in %s" % (
                name, "no shard" if not where else "shards %s" % where))
    for name in sorted(set(seen) - set(discovered)):
        problems.append("%s is in shard %s but was not discovered" % (name, seen[name]))
    return problems


def _runner(stream=None):
    # unittest.main's default: show each warning once, unless -W says otherwise
    return unittest.TextTestRunner(stream=stream, verbosity=1, resultclass=TimedResult,
                                   warnings=None if sys.warnoptions else "default")


def _slowest(rows, top):
    return sorted(rows.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def _print_table(title, rows, out=sys.stdout):
    print(title, file=out)
    for name, seconds in rows:
        print("  %8.1fs  %s" % (seconds, name), file=out)


def cmd_time(args):
    modules, strays, _ = discover()
    suite = unittest.TestSuite(list(modules.values()) + strays)
    result = _runner().run(suite)
    by_module = {m: s.seconds for m, s in modules.items()}
    by_class = {}
    for test_id, seconds in result.durations.items():
        cls = test_id.rsplit(".", 1)[0]
        by_class[cls] = by_class.get(cls, 0.0) + seconds
    print("", file=sys.stderr)
    _print_table("the %d slowest modules (of %d, %.0fs in all):"
                 % (args.top, len(by_module), sum(by_module.values())),
                 _slowest(by_module, args.top), sys.stderr)
    _print_table("the %d slowest classes:" % args.top, _slowest(by_class, args.top), sys.stderr)
    if args.write:
        TIMINGS.write_text(json.dumps({m: round(s, 1) for m, s in sorted(by_module.items())},
                                      indent=1) + "\n")
        print("wrote %s" % TIMINGS.relative_to(REPO), file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


def cmd_check(args):
    modules, strays, errors = discover()
    shards, load = split(list(modules), read_timings(), args.of)
    for i, (shard, seconds) in enumerate(zip(shards, load), 1):
        print("shard %d/%d: %d modules, %.0fs recorded" % (i, args.of, len(shard), seconds))
    problems = coverage_problems(modules, shards)
    problems += ["discovery: %s" % e.strip().splitlines()[-1] for e in errors]
    problems += ["%s did not load as a module" % t.id() for s in strays for t in _cases(s)]
    for line in problems:
        print("::error title=test shards::%s" % line, file=sys.stderr)
    return 1 if problems else 0


def _cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _cases(test)
        else:
            yield test


def cmd_run(args):
    if not 1 <= args.shard <= args.of:
        print("suite.py: --shard is 1..%d" % args.of, file=sys.stderr)
        return 2
    modules, strays, _ = discover()
    shards, load = split(list(modules), read_timings(), args.of)
    mine = shards[args.shard - 1]
    print("shard %d/%d: %d of %d modules, %.0fs recorded"
          % (args.shard, args.of, len(mine), len(modules), load[args.shard - 1]),
          file=sys.stderr)
    # a module that failed to import fails every shard, never none
    suite = unittest.TestSuite([modules[m] for m in mine] + strays)
    result = _runner().run(suite)
    seconds = {m: round(modules[m].seconds, 1) for m in mine}
    _print_table("the slowest modules in this shard:", _slowest(seconds, 10), sys.stderr)
    if args.result:
        record = {"shard": args.shard, "of": args.of,
                  "python": "%d.%d" % sys.version_info[:2],
                  "discovered": sorted(modules), "modules": mine,
                  "strays": sorted(t.id() for s in strays for t in _cases(s)),
                  "ran": result.testsRun, "failures": len(result.failures),
                  "errors": len(result.errors), "skipped": len(result.skipped),
                  "ok": result.wasSuccessful(), "seconds": seconds}
        pathlib.Path(args.result).write_text(json.dumps(record, indent=1) + "\n")
    return 0 if result.wasSuccessful() else 1


def verify(records, count):
    """The problems with one run's shard records ([] when all is well)."""
    problems = []
    got = sorted(r.get("shard") for r in records)
    if got != list(range(1, count + 1)):
        problems.append("shard records %s, expected 1..%d once each" % (got, count))
    if any(r.get("of") != count for r in records):
        problems.append("a record was cut for another shard count")
    discovered = {tuple(r.get("discovered") or ()) for r in records}
    if len(discovered) > 1:
        problems.append("the shards discovered different modules")
    for r in records:
        if not r.get("ok"):
            problems.append("shard %s failed (%s failures, %s errors)"
                            % (r.get("shard"), r.get("failures"), r.get("errors")))
    if records and len(discovered) == 1:
        problems += coverage_problems(next(iter(discovered)),
                                      [r.get("modules") or [] for r in records])
    return problems


def cmd_verify(args):
    records = []
    for path in sorted(pathlib.Path(args.dir).glob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError) as err:
            print("::error title=test shards::%s: %s" % (path, err), file=sys.stderr)
            return 1
    problems = verify(records, args.of)
    for r in sorted(records, key=lambda r: r.get("shard") or 0):
        print("shard %s/%s on %s: %s tests, %s modules, %.0fs, %s"
              % (r.get("shard"), r.get("of"), r.get("python"), r.get("ran"),
                 len(r.get("modules") or []), sum((r.get("seconds") or {}).values()),
                 "ok" if r.get("ok") else "FAILED"))
    for line in problems:
        print("::error title=test shards::%s" % line, file=sys.stderr)
    return 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="suite.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("time", help="run everything; print the slowest modules and classes")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--write", action="store_true", help="save the module times as the weights")
    p = sub.add_parser("check", help="every module in exactly one shard")
    p.add_argument("--of", type=int, default=SHARDS)
    p = sub.add_parser("run", help="run one shard")
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--of", type=int, default=SHARDS)
    p.add_argument("--result", help="write what ran here, as JSON")
    p = sub.add_parser("verify", help="one run's shard records: all passed, each module once")
    p.add_argument("--of", type=int, default=SHARDS)
    p.add_argument("dir")
    args = parser.parse_args(argv)
    return {"time": cmd_time, "check": cmd_check, "run": cmd_run, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
