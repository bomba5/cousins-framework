"""cousin-gate: run the contamination gate or the triage over a tree."""
import argparse

from cousin_lib.gate.scanner import Scanner, load_denylist, manifest_lines


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-gate")
    parser.add_argument("--root", required=True)
    parser.add_argument("--denylist", help="term list, read from OUTSIDE the tree")
    parser.add_argument("--mode", choices=("gate", "triage"), default="gate")
    args = parser.parse_args(argv)

    terms = load_denylist(args.denylist) if args.denylist else []
    hits = Scanner(name_terms=terms).scan_tree(args.root)

    if args.mode == "triage":
        for line in manifest_lines(hits):
            print(line)
        return 0

    for h in hits:
        print("%s:%s:%s %s/%s %s" % (h.file, h.line, h.col, h.kind, h.position, h.context))
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
