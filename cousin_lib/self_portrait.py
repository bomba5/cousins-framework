"""The cousin self-portrait: a reviewed identity layer.

A compact identity file at <home>/self-portrait.md, synthesized from
the cousin's real sources and committed only through a review gate:
synthesize writes a candidate, a human reviews and edits it, commit
promotes it. The boot packet reads ONLY the committed version - an
identity nobody reviewed does not boot.
"""
import re
from pathlib import Path
from cousin_lib import perimeter
from cousin_lib.trace import traced_cli

# Voice and how the cousin works, nothing else (meeting 11 D). The role is
# in CLAUDE.md and cousin.toml, the rules are law and L0 memory, the lane's
# mechanics are the generated contract; a copy here went stale and loaded
# at boot beside its source (a superseded diode pinout, a reversed billing
# rule). A portrait from an older synthesize keeps its sections until the
# operator reviews a new candidate.
SECTIONS = ["Temperament", "Working Style", "Voice"]


def candidate_path(home):
    return Path(home) / ".self-portrait-candidate.md"


def committed_path(home):
    return Path(home) / "self-portrait.md"


def _read(path):
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


def _cap(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (trimmed - review)"


def md_section(text, *keys):
    """Body under the first '##'/'###' heading whose title contains any
    key (case-insensitive), up to the next heading."""
    if not text:
        return ""
    parts = re.split(r"(?m)^(#{2,3}[ \t]+.+)$", text)
    for i in range(1, len(parts), 2):
        title = parts[i].lstrip("#").strip().lower()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if any(k.lower() in title for k in keys) and body.strip():
            return body.strip()
    return ""


def _gather_evidence(home, slug):
    """Per-section draft from the cousin's real sources. Empty body
    means no source; the candidate marks it as a review TODO."""
    claude = _read(Path(home) / "CLAUDE.md")
    evidence = {s: "" for s in SECTIONS}
    evidence["Temperament"] = _cap(
        md_section(claude, "principles", "Identity"), 800)
    evidence["Working Style"] = _cap(md_section(
        claude, "working style", "how you work", "Working method"), 800)
    evidence["Voice"] = _cap(md_section(claude, "Voice"), 500)
    return evidence


def synthesize_candidate(home, slug):
    """Distill a candidate portrait from the cousin's sources. Sections
    with no source become explicit review TODOs, never silent blanks."""
    evidence = _gather_evidence(home, slug)
    lines = ["# Cousin Self-Portrait: %s" % slug, ""]
    for section in SECTIONS:
        lines.append("## %s" % section)
        body = (evidence.get(section) or "").strip()
        lines.append(body or "<TODO: fill %s - review gate>"
                     % section.lower())
        lines.append("")
    lines.append("<!-- Drafted by synthesize; review and edit, then"
                 " commit. -->")
    path = candidate_path(home)
    write_candidate_text(path, "\n".join(lines))
    return path


def write_candidate_text(path, text):
    """Write `text` to `path` without ever following a link there: a
    fresh temp file in the same directory (O_CREAT|O_EXCL|O_NOFOLLOW, so
    nothing planted at its name is opened), then os.replace onto the
    path, which replaces a symlink instead of writing through it."""
    import os
    import secrets
    path = Path(path)
    # The path arrives from the caller, and the committed portrait is the
    # one file here an agent must never write (law rule 3: the persona is
    # authored by the operator, never improvised at runtime). The candidate
    # beside it is writable by design - that is what synthesizing is.
    perimeter.assert_writable(path, writer="self_portrait.write_candidate_text")
    tmp = path.with_name("%s.%s.tmp" % (path.name, secrets.token_hex(6)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o644)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def commit_candidate(home):
    """Promote the reviewed candidate to the committed portrait, backing
    up the previous one so an operator-revertable trail exists."""
    cand = candidate_path(home)
    if not cand.exists():
        raise FileNotFoundError(
            "no candidate at %s; run synthesize first" % cand)
    committed = committed_path(home)
    if committed.exists():
        committed.replace(Path(home) / ".self-portrait.md.bak")
    cand.replace(committed)
    return committed


@traced_cli("cousin-self-portrait")
def portrait_main(argv=None):
    """Console entry point: synthesize / commit / show / diff. The
    review gate in CLI form - synthesize and commit are separate on
    purpose; nothing promotes an unreviewed candidate."""
    import argparse
    import difflib
    import os
    import sys

    parser = argparse.ArgumentParser(prog="cousin-self-portrait")
    parser.add_argument("cmd",
                        choices=["synthesize", "commit", "show", "diff"])
    parser.add_argument("--home",
                        default=os.environ.get("COUSIN_HOME"))
    args = parser.parse_args(argv)
    if not args.home:
        print("cousin-self-portrait: set COUSIN_HOME or pass --home",
              file=sys.stderr)
        return 2
    home = args.home
    slug = os.environ.get("COUSIN_SLUG") or Path(home).name
    if args.cmd == "synthesize":
        path = synthesize_candidate(home, slug)
        print("candidate written: %s (review, then commit)" % path)
        return 0
    if args.cmd == "commit":
        try:
            path = commit_candidate(home)
        except FileNotFoundError as err:
            print("cousin-self-portrait: %s" % err, file=sys.stderr)
            return 1
        print("committed: %s" % path)
        return 0
    if args.cmd == "show":
        text = _read(committed_path(home))
        if not text:
            print("(no committed self-portrait)", file=sys.stderr)
            return 1
        print(text, end="")
        return 0
    committed = _read(committed_path(home)).splitlines(keepends=True)
    candidate = _read(candidate_path(home)).splitlines(keepends=True)
    sys.stdout.writelines(difflib.unified_diff(
        committed, candidate, fromfile="committed", tofile="candidate"))
    return 0


def for_boot_packet(home):
    """The committed portrait for the boot packet - never the
    candidate. The degraded marker is explicit so the boot assembler's
    per-layer check can name the gap."""
    text = _read(committed_path(home)).strip()
    if text:
        return text
    return ("(no committed self-portrait yet - synthesize, review, and"
            " commit one)")
