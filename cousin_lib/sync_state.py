"""Deprecated (3.47.0): `cousin-sync-state` rendered STATUS.md into
data/state.json, a second copy of the open loops that only one reader
used, before it fell back to STATUS.md anyway (meeting 11 A). STATUS.md is
the one copy now. The command stays one release, says so and writes
nothing; then it goes with the next major release."""
import argparse
import sys

from cousin_lib.trace import traced_cli


@traced_cli("cousin-sync-state")
def sync_state_main(argv=None):
    argparse.ArgumentParser(
        prog="cousin-sync-state",
        description="deprecated: STATUS.md is the one copy of the open loops").parse_known_args(argv)
    print("cousin-sync-state is deprecated and does nothing: STATUS.md's open loops are the"
          " one copy, read directly. It is removed in the next major release.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(sync_state_main())
