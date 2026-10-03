"""cousin-gate: run the contamination gate or the triage over a tree."""
import argparse

from cousin_lib.gate.scanner import (Scanner, git_visible_files,
                                     load_denylist, manifest_lines, scan_commits)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-gate")
    parser.add_argument("--root", required=True)
    parser.add_argument("--denylist", help="term list, read from OUTSIDE the tree")
    parser.add_argument("--mode", choices=("gate", "triage"), default="gate")
    parser.add_argument(
        "--git-visible", action="store_true",
        help="scan only what git would publish (tracked files and"
             " untracked ones .gitignore does not exclude); use it on a"
             " checkout that also hosts a live install. Outside a git"
             " work tree the whole tree is scanned.")
    parser.add_argument(
        "--commits", metavar="RANGE",
        help="scan the commits in a git revision range (e.g."
             " origin/main..HEAD) instead of the tree: each message, any"
             " Co-authored-by trailer, and with --expect-author each"
             " author and committer. Run it before a push.")
    parser.add_argument(
        "--expect-author", metavar="'NAME <EMAIL>'",
        help="with --commits: the one identity every author and committer"
             " must be")
    args = parser.parse_args(argv)

    terms = load_denylist(args.denylist) if args.denylist else []
    scanner = Scanner(name_terms=terms)
    if args.commits:
        try:
            hits = scan_commits(scanner, args.root, args.commits, args.expect_author)
        except ValueError as err:
            parser.exit(2, "cousin-gate: %s\n" % err)
    else:
        files = git_visible_files(args.root) if args.git_visible else None
        hits = (scanner.scan_tree(args.root) if files is None
                else scanner.scan_files(args.root, files))

    if args.mode == "triage":
        for line in manifest_lines(hits):
            print(line)
        return 0

    for h in hits:
        print("%s:%s:%s %s/%s %s" % (h.file, h.line, h.col, h.kind, h.position, h.context))
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
