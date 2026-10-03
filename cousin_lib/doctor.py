"""cousin-doctor: checks an install for what the operator should fix by hand.

Each check is a function of the framework root that only reads and
returns a result: its name, whether it is ok, a one-line summary, the
items it found, the shell lines that fix them (printed, never run) and
notes. CHECKS names them in the order they run; `cousin-doctor` runs
every one, `cousin-doctor <name>...` the ones named.

homes. Every cousin home (a directory under `<root>/cousins/` with a
cousin.toml) whose mode has a group or other bit is listed with the
`chmod 700 <home>` that closes it. Spawn makes new homes 0700; this
finds the ones an older release made. Be clear about what it buys:
every cousin runs as one user, so a 0700 home closes it to other users
on the host and to anything running as another uid (a container's
user, a second account), not one cousin to another. When a directory
above the homes is already closed to group and other (a 0700 `$HOME`),
other users cannot reach the homes today, and the check says so."""
import argparse
import json
import os
import shlex
import stat
import sys
from pathlib import Path

HOME_MODE = 0o700
OPEN_BITS = 0o077            # group and other: read, write, search

SCOPE = ("scope: every cousin runs as this one user, so 0700 homes close them to"
         " other users on the host and to other uids (a container's user, a second"
         " account), not one cousin to another")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def cousin_homes(root):
    """The homes under <root>/cousins/: each directory with a cousin.toml,
    sorted by name. A cousin.toml is not parsed: a home with a broken
    one is still a home to close."""
    base = Path(root) / "cousins"
    if not base.is_dir():
        return []
    return [entry for entry in sorted(base.iterdir())
            if entry.is_dir() and (entry / "cousin.toml").is_file()]


def closed_above(path):
    """The nearest directory above `path` that group and other cannot
    search (no x bit for either), or None. Through it, no other user
    reaches anything below, whatever the modes there."""
    path = Path(os.path.abspath(path))
    for parent in path.parents:
        try:
            if _mode(parent) & 0o011 == 0:
                return parent
        except OSError:
            continue
    return None


def _homes(n):
    return "%d cousin home%s" % (n, "" if n == 1 else "s")


def check_homes(root):
    root = Path(os.path.abspath(root))
    homes = cousin_homes(root)
    items, fixes, errors = [], [], []
    for home in homes:
        try:
            mode = _mode(home)
        except OSError as err:
            errors.append("%s: cannot stat: %s" % (home, err))
            continue
        if mode & OPEN_BITS:
            items.append({"home": str(home), "mode": "%04o" % mode})
            fixes.append("chmod %o %s    # now %04o" % (HOME_MODE, shlex.quote(str(home)), mode))
    notes = []
    if not homes:
        summary = "no cousin homes under %s" % (root / "cousins")
    elif items:
        summary = "%d of %s open to group or other" % (len(items), _homes(len(homes)))
    else:
        summary = "%s, none open to group or other" % _homes(len(homes))
    if errors:
        summary += "; %d could not be read" % len(errors)
    above = closed_above(root / "cousins")
    if items and above is not None:
        notes.append("%s (mode %04o) already keeps other users out of everything below it,"
                     " so this changes little today; the chmod keeps the homes closed if"
                     " that directory is ever opened" % (above, _mode(above)))
    notes.append(SCOPE)
    return {"check": "homes", "ok": not items and not errors, "summary": summary,
            "items": items, "fixes": fixes, "errors": errors, "notes": notes}


CHECKS = {"homes": check_homes}


def run_checks(root, names=None):
    return [CHECKS[name](root) for name in (names or list(CHECKS))]


def render(result):
    """A result as the lines cousin-doctor prints."""
    lines = ["%-5s %s: %s" % ("OK" if result["ok"] else "WARN", result["check"], result["summary"])]
    lines += ["      %s" % e for e in result["errors"]]
    if result["fixes"]:
        lines.append("      to fix, run:")
        lines += ["        %s" % f for f in result["fixes"]]
    lines += ["      %s" % n for n in result["notes"]]
    return lines


def doctor_main(argv=None):
    """cousin-doctor [check ...] [--json] [--root R]. Exit codes: 0 every
    check ok, 1 a check found something to fix, 2 usage or no root."""
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    parser = argparse.ArgumentParser(
        prog="cousin-doctor",
        description="check the install for what to fix by hand; prints the fix, changes nothing"
                    " (checks: %s)" % ", ".join(CHECKS),
        epilog="exit status: 0 all ok, 1 something to fix, 2 bad usage")
    parser.add_argument("checks", nargs="*", metavar="check",
                        help="run only these (default: all): %s" % ", ".join(CHECKS))
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    parser.add_argument("--root", default=None, help="framework root (else FRAMEWORK_ROOT)")
    args = parser.parse_args(argv)
    unknown = [name for name in args.checks if name not in CHECKS]
    if unknown:
        parser.error("no check %s (checks: %s)" % (", ".join(unknown), ", ".join(CHECKS)))
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
    except MissingConfigError as err:
        print("cousin-doctor: %s" % err, file=sys.stderr)
        return 2
    results = run_checks(root, args.checks)
    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for result in results:
            print("\n".join(render(result)))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(doctor_main())
