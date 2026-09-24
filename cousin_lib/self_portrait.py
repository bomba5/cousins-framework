"""The cousin self-portrait: a reviewed identity layer.

A compact identity file at <home>/self-portrait.md, synthesized from
the cousin's real sources and committed only through a review gate:
synthesize writes a candidate, a human reviews and edits it, commit
promotes it. The boot packet reads ONLY the committed version - an
identity nobody reviewed does not boot.
"""
import json
import re
from pathlib import Path
from cousin_lib.trace import traced_cli

SECTIONS = [
    "Role", "Temperament", "Operator Calibration", "Working Style",
    "Recurring Risks", "Voice", "Identity Invariants",
]

# Signals that a decision entry encodes a risk/reversal/failure LESSON.
# Deliberately failure language, not prohibition words ("do not",
# "never") - prohibitions appear in ordinary decisions and would flood
# the section with non-risks.
_RISK_KW = (
    "wrong", "revert", "regress", "caught", "footgun", "leak", "broke",
    "broken", "mistake", "failed", "false ", "almost ", "nearly ",
    "slipped", "should have", "turned out", "actually not", "clobber",
)


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


def _join(*parts):
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


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


def _mine_risks(home, *, limit=8):
    """Recurring risks mined from failure-language decisions (newest
    first) plus the known-failures distilled file when present."""
    out, seen = [], set()
    try:
        lines = (Path(home) / "data" / "decisions.jsonl").read_text() \
            .splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        blob = ("%s %s" % (entry.get("decision", ""),
                           entry.get("reasoning", ""))).lower()
        if any(k in blob for k in _RISK_KW):
            topic = (entry.get("topic") or "").strip()
            if topic and topic.lower() not in seen:
                seen.add(topic.lower())
                out.append("- %s: %s"
                           % (topic, (entry.get("decision") or "")[:140]))
        if len(out) >= limit:
            break
    for line in _read(Path(home) / "memory" / "distilled"
                      / "known-failures.md").splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            out.append(stripped)
    return out[:limit]


def _toml_role(home):
    text = _read(Path(home) / "cousin.toml")
    if not text:
        return ""
    try:
        import tomllib
        return tomllib.loads(text).get("cousin", {}).get("role", "")
    except Exception:
        return ""


def _gather_evidence(home, slug):
    """Per-section draft from the cousin's real sources. Empty body
    means no source; the candidate marks it as a review TODO."""
    claude = _read(Path(home) / "CLAUDE.md")
    hard = md_section(claude, "Hard rules", "Hard rule")
    evidence = {s: "" for s in SECTIONS}
    evidence["Role"] = _cap(_join(
        _toml_role(home),
        md_section(claude, "Identity").split("\n\n", 1)[0]
        if md_section(claude, "Identity") else "",
    ), 700)
    evidence["Temperament"] = _cap(
        md_section(claude, "principles", "Identity"), 800)
    evidence["Working Style"] = _cap(md_section(
        claude, "working style", "how you work", "Working method"), 800)
    evidence["Voice"] = _cap(md_section(claude, "Voice"), 500)
    evidence["Operator Calibration"] = _cap(hard, 900)
    evidence["Recurring Risks"] = "\n".join(_mine_risks(home))
    evidence["Identity Invariants"] = _cap(hard, 800)
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
