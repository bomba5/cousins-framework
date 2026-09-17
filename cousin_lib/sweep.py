"""Fleet compaction sweep.

Runs `cousin-memory compact` for every cousin home under the framework
root, one at a time, and reports per cousin. The sweep only enumerates,
sequences and reports: every safety rule (budget, hot window, lossless
raw fold, uncertainty keeps) lives in the compactor itself. Two rules
earned in the source framework:

- One cousin's failure never aborts the rest; the exit code carries
  "any failed" so the service unit's journal shows the alert.
- The compactor is driven out of process with the cousin's own home,
  never by a bare command name on PATH: a service unit carries no PATH
  worth trusting, and inheriting the runner's own cousin identity made
  every home resolve against the wrong cousin.
"""
import argparse
import subprocess
import sys

from cousin_lib.config import FrameworkConfig, MissingConfigError

TARGETS = ("index", "raw", "both")
PER_RUN_TIMEOUT_S = 300
_MEMORY_MAIN = ("import sys; from cousin_lib.memory import memory_main;"
                " sys.exit(memory_main())")


def _default_run(home, slug, target):
    """Run one compaction out of process. Returns (rc, output)."""
    cmd = [sys.executable, "-c", _MEMORY_MAIN, "--home", str(home),
           "compact", "--target", target]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PER_RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ds" % PER_RUN_TIMEOUT_S
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out


def sweep_compact(root, *, target="index", run=_default_run):
    """One result per cousin, in registry order:
    {slug, home, target, rc, ok, output}. A raised runner counts as a
    failed result and the sweep continues."""
    if target not in TARGETS:
        raise ValueError("target must be one of %s" % ", ".join(TARGETS))
    steps = ("index", "raw") if target == "both" else (target,)
    results = []
    for config in FrameworkConfig(root).list_cousins():
        home = config.home
        for step in steps:
            try:
                rc, output = run(home, config.slug, step)
            except Exception as err:  # the sweep outlives any one home
                rc, output = 1, "ERROR %s: %s" % (type(err).__name__, err)
            results.append({
                "slug": config.slug, "home": str(home), "target": step,
                "rc": int(rc), "ok": int(rc) == 0,
                "output": output.replace("\n", " | ")[:400],
            })
    return results


def sweep_main(argv=None):
    """cousin-sweep compact [--target index|raw|both] [--root R].
    Exit 0 all ok, 1 any cousin failed, 2 usage."""
    parser = argparse.ArgumentParser(
        prog="cousin-sweep",
        description="fleet-wide maintenance, one cousin at a time")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("compact",
                       help="cousin-memory compact for every cousin home")
    p.add_argument("--target", choices=TARGETS, default="index")
    p.add_argument("--root", default=None,
                   help="framework root (else FRAMEWORK_ROOT)")
    args = parser.parse_args(argv)
    try:
        root = FrameworkConfig.resolve(args.root).root
    except MissingConfigError as err:
        print("cousin-sweep: %s" % err, file=sys.stderr)
        return 2
    # Looked up at call time, not bound at definition, so a test (or a
    # wrapper) can substitute the runner through the module.
    results = sweep_compact(root, target=args.target, run=_default_run)
    if not results:
        print("[sweep] no cousins under %s" % root)
        return 0
    failed = 0
    for r in results:
        print("[sweep] %s %s: rc=%d %s"
              % (r["slug"], r["target"], r["rc"], r["output"]))
        failed += 0 if r["ok"] else 1
    print("[sweep] %d run(s), %d failed" % (len(results), failed))
    return 1 if failed else 0
