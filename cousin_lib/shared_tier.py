"""The shared memory tier: private by default, shared by review.

docs/memory-tiers.md is this module's specification and the tests
hold it to the page's promises. The boundary that everything else
serves: the proposing side and the promoting side are never the same
principal, and no configuration can express otherwise - a boundary
that config can switch off is not a boundary.
"""
import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib.config import CousinConfig, FrameworkConfig

SHAREABLE_PREFIXES = ("project_", "project-", "reference_", "reference-")


class PromoteRefused(Exception):
    """Promotion cannot proceed; the message says why and how to fix."""


def _shared_root():
    return FrameworkConfig.from_env().root / "shared"


def _proposed_name(slug, file):
    return "%s__%s.md" % (slug, Path(file).stem)


def _audit(kind, actor, file, extra=None):
    """Append-only write log with source attribution - every mutation
    of the tier leaves a row, successes and overwrites alike."""
    path = _shared_root() / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "actor": actor,
        "file": file,
    }
    if extra:
        entry.update(extra)
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def list_shared():
    root = _shared_root()
    canonical = sorted(p.name for p in root.glob("*.md") if p.is_file())
    proposed_dir = root / "proposed"
    pending = (sorted(p.name for p in proposed_dir.glob("*.md"))
               if proposed_dir.is_dir() else [])
    return {"canonical": canonical, "pending": pending}


def read_shared(file):
    return (_shared_root() / file).read_text()


def diff_proposal(file, slug):
    canonical = _shared_root() / file
    proposed = _shared_root() / "proposed" / _proposed_name(slug, file)
    base = (canonical.read_text().splitlines(keepends=True)
            if canonical.exists() else [])
    new = (proposed.read_text().splitlines(keepends=True)
           if proposed.exists() else [])
    return "".join(difflib.unified_diff(base, new, fromfile=file,
                                        tofile=proposed.name))


def propose(file, body, *, slug, reason="", force=False):
    """The single entry path: a candidate lands in proposed/, never in
    canonical, with the proposer's slug in the filename. Replacing an
    existing proposal requires force and both writes are audited."""
    proposed_dir = _shared_root() / "proposed"
    proposed_dir.mkdir(parents=True, exist_ok=True)
    target = proposed_dir / _proposed_name(slug, file)
    exists = target.exists()
    if exists and not force:
        raise FileExistsError(
            "proposal already exists at %s; pass force to replace"
            % target)
    target.write_text(body)
    _audit("propose-overwrite" if exists else "propose", slug, file, {
        "proposal": target.name,
        "sha": hashlib.sha256(body.encode()).hexdigest()[:12],
        "reason": reason,
    })
    return target


def _reviewers():
    path = FrameworkConfig.from_env().root / "config" \
        / "shared-reviewers.json"
    try:
        return json.loads(path.read_text()).get("reviewers", [])
    except (OSError, ValueError):
        return None


def _principal(name_or_slug):
    """Resolve an identity string to its canonical principal id.

    Identity here must be a resolved thing, not text: the framework
    deliberately separates slugs from display names, so a cousin with
    slug 'wren' and name 'Sam' is ONE principal wearing two strings -
    and comparing the strings lets it approve its own proposal by
    wearing the other one. A string that names a registered cousin (by
    slug or display name, case-insensitive) resolves to that cousin's
    slug; anything else - a human reviewer - resolves to its own
    folded form. A human whose name collides with a cousin's display
    name resolves to the cousin and may be refused: on ambiguity the
    boundary denies, consistent with the tier's posture everywhere
    else."""
    folded = name_or_slug.strip().lower()
    for config in FrameworkConfig.from_env().list_cousins():
        if folded in (config.slug.lower(), config.name.lower()):
            return config.slug.lower()
    return folded


def _check_reviewer(proposer, by):
    """The boundary, enforced at the promote site over RESOLVED
    principals - one normalisation, used by every check. Order
    matters: the self-approval refusal fires even for a configured
    reviewer, because the allowlist must not be able to express
    proposer==approver."""
    # Resolution requires the registry. Without it every name would
    # resolve to its own folded form and this check would silently
    # revert to the raw-string comparison it replaced - a security
    # check degrading to its weaker predecessor when its data source
    # is absent. Absence is not a no-op here; it changes who can
    # approve what. Refuse.
    if not (FrameworkConfig.from_env().root / "cousins").is_dir():
        raise PromoteRefused(
            "cousin registry not found under the framework root;"
            " cannot resolve principals, refusing to promote"
        )
    by_principal = _principal(by)
    if by_principal == _principal(proposer):
        raise PromoteRefused(
            "%s cannot promote their own proposal: the proposing side"
            " and the promoting side are never the same principal"
            % by)
    reviewers = _reviewers()
    if reviewers is None:
        raise PromoteRefused(
            "no reviewers configured; write config/"
            "shared-reviewers.json ({\"reviewers\": [name, ...]})"
            " before promoting - an implicit reviewer set is the"
            " self-approval hole one step removed")
    if by_principal not in {_principal(r) for r in reviewers}:
        raise PromoteRefused(
            "%r is not in the configured reviewer list" % by)


def promote(file, *, proposer, by):
    """Reviewed promotion: the proposal becomes canonical, atomically,
    with the reviewer on the audit record."""
    _check_reviewer(proposer, by)
    source = _shared_root() / "proposed" / _proposed_name(proposer, file)
    if not source.exists():
        raise FileNotFoundError("no proposal at %s" % source)
    target = _shared_root() / file
    source.replace(target)
    _audit("promote", by, file, {"proposer": proposer})
    return target


def reject(file, *, proposer, by, reason=""):
    """Rejection removes the proposal; the reason survives on the
    audit record even though the content does not."""
    _check_reviewer(proposer, by)
    source = _shared_root() / "proposed" / _proposed_name(proposer, file)
    if not source.exists():
        raise FileNotFoundError("no proposal at %s" % source)
    source.unlink()
    _audit("reject", by, file, {"proposer": proposer, "reason": reason})


def _is_marked_shareable(body):
    """Per-file opt-in: bulk-by-type is too loose because project and
    reference memory can hold confidential operator specifics; only a
    file that explicitly says 'shareable: true' enters the queue.
    Deny-on-uncertainty at the file level."""
    head = body[:500]
    return bool(re.search(r"(?mi)^shareable:\s*true\s*$", head))


def plan_bulk_propose(home, slug):
    """Plan (never write) the bulk nomination of a cousin's shareable
    memories. The perimeter, outermost first: the cousin's [memory]
    scope must be 'shared' or 'both' (private and UNSET are excluded -
    deny-on-uncertainty, because a privacy gate defaults opposite to a
    retention default); only project_/reference_ files; only files
    marked shareable; deduplicated against canonical and pending."""
    config = CousinConfig.load(home)
    scope = getattr(config, "memory_scope", "private")
    plan = {"eligible": scope in ("shared", "both"), "scope": scope,
            "propose": [], "skipped": []}
    if not plan["eligible"]:
        return plan
    existing = set(list_shared()["canonical"])
    pending = set(list_shared()["pending"])
    memory_dir = Path(home) / "memory"
    if not memory_dir.is_dir():
        return plan
    for path in sorted(memory_dir.glob("*.md")):
        fname = path.name
        if not fname.startswith(SHAREABLE_PREFIXES):
            plan["skipped"].append((fname, "not project_/reference_"))
            continue
        body = path.read_text(errors="replace")
        if not _is_marked_shareable(body):
            plan["skipped"].append((fname, "not marked shareable: true"))
            continue
        proposed_name = _proposed_name(slug, fname)
        if fname in existing or proposed_name in pending:
            plan["skipped"].append((fname, "already shared or pending"))
            continue
        plan["propose"].append({"fname": fname, "path": str(path),
                                "proposed_name": proposed_name})
    return plan


def shared_main(argv=None):
    """Console entry point: cousin-shared list/read/diff/propose/
    promote/reject. Exit codes: 0 ok, 1 not found, 2 usage,
    3 refused by the boundary."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="cousin-shared")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("read")
    p.add_argument("file")
    p = sub.add_parser("diff")
    p.add_argument("file")
    p.add_argument("--slug", required=True)
    p = sub.add_parser("propose")
    p.add_argument("file")
    p.add_argument("--slug", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--force", action="store_true")
    for name in ("promote", "reject"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--proposer", required=True)
        p.add_argument("--by", required=True)
        if name == "reject":
            p.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            state = list_shared()
            print("canonical:")
            for name in state["canonical"]:
                print("  %s" % name)
            print("pending proposals:")
            for name in state["pending"] or ["  (none)"]:
                print("  %s" % name if not name.startswith(" ") else name)
        elif args.cmd == "read":
            sys.stdout.write(read_shared(args.file))
        elif args.cmd == "diff":
            sys.stdout.write(diff_proposal(args.file, args.slug))
        elif args.cmd == "propose":
            body = sys.stdin.read()
            if not body.strip():
                print("cousin-shared: empty body", file=sys.stderr)
                return 2
            target = propose(args.file, body, slug=args.slug,
                             reason=args.reason, force=args.force)
            print("proposed: %s" % target)
        elif args.cmd == "promote":
            target = promote(args.file, proposer=args.proposer,
                             by=args.by)
            print("promoted: %s" % target)
        elif args.cmd == "reject":
            reject(args.file, proposer=args.proposer, by=args.by,
                   reason=args.reason)
            print("rejected: %s (reason on the audit record)"
                  % args.file)
    except PromoteRefused as err:
        print("cousin-shared: %s" % err, file=sys.stderr)
        return 3
    except FileExistsError as err:
        print("cousin-shared: %s" % err, file=sys.stderr)
        return 1
    except FileNotFoundError as err:
        print("cousin-shared: %s" % err, file=sys.stderr)
        return 1
    return 0


def commit_bulk_propose(plan, slug):
    """Write the planned proposals into the queue. Only ever writes to
    proposed/; the approval path is untouched."""
    count = 0
    for item in plan["propose"]:
        body = Path(item["path"]).read_text(errors="replace")
        propose(item["fname"], body, slug=slug,
                reason="bulk-propose from memory/")
        count += 1
    return count
