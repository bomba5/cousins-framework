"""The per-runner contract table (phase 9, R19): one row per item of the
runner contract suite (`tests/runner/contract/suite.py`), one column per
runner kind (`delivery.RUNNER_KINDS`), each cell IMPLEMENTED, PLUGIN or
DECLARED, rendered into `docs/reference/runners.md` between the markers.

A cell comes from the runner class's own declarations, read at class
level so no runner (and no server) is built: `UNSUPPORTED`, the items
`unsupported()` returns (DECLARED: the suite skips exactly those tests,
and the runner reports them in its `runner` event, which the console
shows), and `PLUGIN_ITEMS`, the items `plugin_items()` returns (met by a
plugin the runner loads rather than by the runner's own code). Every
other item is IMPLEMENTED. The suite runs every item that is not
DECLARED, so an IMPLEMENTED or PLUGIN cell is one the suite enforces.

`ITEMS` is the suite's `CONTRACT_ITEMS`, kept here because `tests/` is
not shipped; a test asserts the two are equal.

    python3 -m cousin_lib.runner.contract_table            # print the table
    python3 -m cousin_lib.runner.contract_table --write    # rewrite the page
    python3 -m cousin_lib.runner.contract_table --check    # exit 1 when stale
"""
import argparse
import importlib
import sys
from pathlib import Path

from cousin_lib.delivery import RUNNER_KINDS

IMPLEMENTED, PLUGIN, DECLARED = "IMPLEMENTED", "PLUGIN", "DECLARED"

# (item, what the suite proves), in the suite's order
ITEMS = (
    ("enqueue_receipt", "`enqueue` answers a `Receipt` with an inbox id, outcome `queued`"),
    ("priority_order", "queued rows run in priority order: operator, then peer, then loop"),
    ("consume_after_start", "a row put while the runner is stopped runs after `start()`"),
    ("interrupt_ends_turn", "`interrupt()` ends the running turn; the runner is idle again"),
    ("turn_events", "every turn emits `turn_start`, then `tool`, then `result`"),
    ("unsupported_list", "`unsupported()` names contract items only"),
    ("midturn_fold", "an operator message put mid-turn is closed by the same `result`"),
    ("outcome_delivered", "a finished turn closes its row `delivered`"),
    ("outcome_failed", "a failed turn closes its row `failed`, the result an error"),
    ("outcome_interrupted", "an interrupted turn's row is `delivered` (the model had it)"),
    ("failure_recovers", "after a failure (`errored`, then `idle`) the next row runs"),
    ("stop_ends_turn", "`stop()` during a turn ends it within its timeout"),
    ("peer_waits", "a peer message put mid-turn waits for a turn of its own"),
    ("state_events", "every state transition is a `state` event, in order"),
    ("events_after", "`events(after=n)` resumes exactly after event `n`"),
    ("interrupt_idle_false", "`interrupt()` with no turn running answers False"),
    ("enqueue_type_error", "`enqueue` refuses anything but an `Item` (TypeError)"),
    ("rollover_shape", "`rollover()` answers `{ok, reason}`"),
    ("rollover_generation", "a rollover moves the generation and loses no row"),
    ("interrupt_row", "an `interrupt` inbox row ends the live turn and is `delivered`"),
    ("interrupt_row_idle", "an `interrupt` row with no turn running is `failed`, never a turn"),
)

# runner kind -> "module:Class"; every kind in delivery.RUNNER_KINDS has one
RUNNER_CLASSES = {
    "sdk": "cousin_lib.runner.sdk:SdkRunner",
    "fake": "cousin_lib.runner.fake:FakeRunner",
    "opencode": "cousin_lib.runner.opencode:OpencodeRunner",
}

PAGE = Path("docs") / "reference" / "runners.md"
BEGIN = "<!-- contract-table:begin (python3 -m cousin_lib.runner.contract_table --write) -->"
END = "<!-- contract-table:end -->"


class TableError(Exception):
    pass


def item_names():
    return tuple(name for name, _ in ITEMS)


def runner_class(kind):
    """The class behind a runner kind, imported (no runner is built)."""
    try:
        module, _, name = RUNNER_CLASSES[kind].partition(":")
    except KeyError:
        raise TableError("runner kind %r has no class in contract_table.RUNNER_CLASSES" % kind)
    return getattr(importlib.import_module(module), name)


def declarations(cls):
    """(DECLARED items, PLUGIN items) of a runner class, checked: each a
    contract item, none both."""
    declared = tuple(getattr(cls, "UNSUPPORTED", ()))
    plugin = tuple(getattr(cls, "PLUGIN_ITEMS", ()))
    known = set(item_names())
    for what, names in (("UNSUPPORTED", declared), ("PLUGIN_ITEMS", plugin)):
        unknown = sorted(set(names) - known)
        if unknown:
            raise TableError("%s.%s names no contract item: %s"
                             % (cls.__name__, what, ", ".join(unknown)))
    both = sorted(set(declared) & set(plugin))
    if both:
        raise TableError("%s declares %s both unsupported and met by a plugin"
                         % (cls.__name__, ", ".join(both)))
    return declared, plugin


def cells(kinds=RUNNER_KINDS):
    """{kind: {item: IMPLEMENTED | PLUGIN | DECLARED}}."""
    out = {}
    for kind in kinds:
        declared, plugin = declarations(runner_class(kind))
        out[kind] = {name: DECLARED if name in declared else PLUGIN if name in plugin
                     else IMPLEMENTED for name in item_names()}
    return out


def render(kinds=RUNNER_KINDS):
    """The markdown table, one row per item, one column per kind."""
    table = cells(kinds)
    lines = ["| item | what the suite proves | %s |" % " | ".join("`%s`" % k for k in kinds),
             "|---|---|" + "---|" * len(kinds)]
    for name, what in ITEMS:
        lines.append("| `%s` | %s | %s |" % (name, what,
                                           " | ".join(table[k][name] for k in kinds)))
    return "\n".join(lines) + "\n"


def splice(page, table):
    """`page` with the text between BEGIN and END replaced by `table`."""
    if page.count(BEGIN) != 1 or page.count(END) != 1 or page.index(BEGIN) > page.index(END):
        raise TableError("the page needs exactly one %s ... %s pair" % (BEGIN, END))
    head, rest = page.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return "%s%s\n%s%s%s" % (head, BEGIN, table, END, tail)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m cousin_lib.runner.contract_table",
                                     description="The per-runner contract table.")
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--write", action="store_true", help="rewrite the table in the page")
    what.add_argument("--check", action="store_true", help="exit 1 when the page is stale")
    parser.add_argument("--page", default=None,
                        help="the page (default: docs/reference/runners.md in the checkout)")
    args = parser.parse_args(argv)
    try:
        table = render()
        if not (args.write or args.check):
            sys.stdout.write(table)
            return 0
        page = Path(args.page) if args.page else Path(__file__).resolve().parents[2] / PAGE
        text = page.read_text()
        fresh = splice(text, table)
    except (TableError, OSError) as err:
        print("contract_table: %s" % err, file=sys.stderr)
        return 2
    if args.check:
        if fresh != text:
            print("contract_table: %s is stale: run python3 -m cousin_lib.runner.contract_table"
                  " --write" % page, file=sys.stderr)
            return 1
        return 0
    if fresh != text:
        page.write_text(fresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
