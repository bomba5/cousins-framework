"""One validating model turn for a cousin's account, in a process of its
own (#100 review). sdk.validate_account scrubs os.environ for the turn
(_ScrubbedAuthEnv), which is process-wide: a long-lived process with
other threads, the console above all, must never run it in place. It
runs this module as a child instead and reads only the verdict:

    python3 -m cousin_lib.runner.validate_turn --home H --root R
        --model M [--effort E] [--timeout S]

The verdict is one JSON line on stdout, {"rc": n, "line": "..."}, with
validate_account's codes: 0 the turn answered, 4 it did not, 2 a
configuration error. The exit code is the same rc."""
import argparse
import json
import sys
from pathlib import Path

from cousin_lib import accounts


def _verdict(rc, line):
    print(json.dumps({"rc": rc, "line": line}), flush=True)
    return rc


def main(argv=None):
    parser = argparse.ArgumentParser(prog="validate_turn")
    parser.add_argument("--home", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    home, root = Path(args.home), Path(args.root)
    try:
        account = accounts.for_cousin(home, root)
    except accounts.AccountsError as err:
        return _verdict(2, "validate: %s" % err)
    try:
        from cousin_lib.runner import sdk
        rc, line = sdk.validate_account(account, root, model=args.model,
                                        effort=args.effort, timeout=args.timeout)
    except Exception as err:  # noqa: BLE001 - a validation that cannot run did not pass
        return _verdict(2, "validate: %s: %s" % (type(err).__name__, err))
    return _verdict(rc, line)


if __name__ == "__main__":
    sys.exit(main())
