"""Identity surgery with memory continuity: reincarnate and transplant.

A cousin's working state lives in a finite context window; its
durable state lives in files. Changing who a cousin is (its role, or
which memory it carries) without losing the thread it was holding is
a ceremony, not an edit: snapshot first, ask the running session for
a bequest, mutate the files, then flip so the next generation boots
from the new identity. Every step lands in an audit log.

Two operations, specified in docs/reference/lifecycle.md:

  cousin-reincarnate <slug> --new-role TEXT
      One cousin. Snapshot, bequest, rewrite the role in CLAUDE.md
      and cousin.toml, flip.

  cousin-transplant --donor A --recipient B --mode MODE
      Two cousins. Snapshot both, apply the mode, flip both.
      soul-donation: B carries A's memory in B's body.
      body-swap:     the two bodies trade places; memory stays put.
      merge:         A's memory is braided into B's; A keeps its own.

Nothing here talks to a console: the registry is the filesystem
(FrameworkConfig.list_cousins), the restart is flip.flip, and the one
network call is the bequest prompt to the cousin's own chat server.
The flip is an injected seam (`do_flip`) so the choreography is
testable without a terminal.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib.backup import MEMORY_SKIP
from cousin_lib.config import FrameworkConfig, MissingConfigError
from cousin_lib.trace import traced_cli

BEQUEST_TIMEOUT_SECONDS = 300
MODES = ("soul-donation", "body-swap", "merge")
# The continuity files a snapshot preserves; memory/ travels whole
# (minus rebuildable indexes) beside them.
SNAPSHOT_FILES = ("MEMORY.md", "STATUS.md", "CLAUDE.md", "cousin.toml",
                  "self-portrait.md")
IDENTITY_FILES = ("CLAUDE.md", "self-portrait.md")
BEQUEST_SENDER = "framework"

BEQUEST_PROMPT = (
    "[cousin-reincarnate] You are about to be reincarnated: your role"
    " changes, your memory persists. Before the framework rebuilds"
    " your session, write data/handoff.md in your own voice, present"
    " tense: who you are right now, the last work that mattered and"
    " its texture, what is in flight, and a bequest to your successor"
    " - what nothing else on disk will tell them. You have {timeout}"
    " seconds; the flip follows either way."
)

# The runner lane's bequest: the same request, answered through the handoff
# tool (R10). Over 120 characters, so rollover.is_bequest keeps it whole.
BEQUEST_PROMPT_RUNNER = (
    "[cousin-reincarnate] You are about to be reincarnated: your role"
    " changes, your memory persists. Before the framework rebuilds"
    " your session, call the `handoff` tool and put in its `position`"
    " field, in your own voice, present tense: who you are right now,"
    " the last work that mattered and its texture, what is in flight,"
    " and a bequest to your successor - what nothing else on disk will"
    " tell them. You have {timeout} seconds; the rollover follows"
    " either way."
)


class LifecycleError(Exception):
    """A refusal: unknown cousin, unknown mode, same slug twice."""


# ---------- shared bookkeeping ----------

def _now():
    return datetime.now(timezone.utc)


def _audit_path(root):
    return Path(root) / "data" / "lifecycle" / "audit.jsonl"


def _audit(root, record):
    path = _audit_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": _now().isoformat()}
    row.update(record)
    with open(path, "a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


def _registry(root):
    return {c.slug: c for c in FrameworkConfig(root).list_cousins()}


def _resolve(registry, slug):
    try:
        return registry[slug]
    except KeyError:
        raise LifecycleError("unknown cousin %r (no cousins/%s/cousin.toml"
                             " under the root)" % (slug, slug))


def snapshot(home, root, slug):
    """Copy the continuity files to <root>/data/lifecycle/<slug>/<ts>/
    and return that directory. A second snapshot in the same second
    gets a suffix rather than overwriting the first."""
    base = Path(root) / "data" / "lifecycle" / slug
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    dest = base / stamp
    n = 1
    while dest.exists():
        n += 1
        dest = base / ("%s-%d" % (stamp, n))
    dest.mkdir(parents=True)
    home = Path(home)
    for name in SNAPSHOT_FILES:
        src = home / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    memory = home / "memory"
    if memory.is_dir():
        shutil.copytree(memory, dest / "memory",
                        ignore=shutil.ignore_patterns(*MEMORY_SKIP))
    return dest


def _default_do_flip(root):
    """flip.flip reads the root from the environment; the lifecycle
    commands take --root. Bridge the two without a second discovery
    rule: an explicit root wins, an already-set environment is left."""
    def run(slug, reason=None):
        from cousin_lib.flip import flip
        os.environ.setdefault("FRAMEWORK_ROOT", str(root))
        return flip(slug, queue_if_stopped=True,
                    **({"reason": reason} if reason is not None else {}))
    return run


def _flip_one(root, slug, do_flip, base_record, reason=None):
    result = (do_flip(slug) if reason is None else do_flip(slug, reason=reason)) or {}
    ok = bool(result.get("ok"))
    record = dict(base_record, step="flip", flipped=slug, ok=ok)
    if not ok:
        record["error"] = result.get("error", "flip failed")
    _audit(root, record)
    return ok, result


# ---------- the bequest ----------

def send_prompt(config, text):
    """Deliver a prompt to a cousin through its own chat server's
    /api/send: it lands in the chat history and in the terminal, the
    same path an operator's message takes."""
    payload = {"user": BEQUEST_SENDER, "message": text}
    url = "http://%s:%d/api/send" % (config.chat_host or "localhost",
                                     config.require_chat_port())
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def _wait_for_write(path, mtime_before, timeout):
    """Bounded wait for a file's mtime to move past what it was when the
    prompt went out; a file that appears counts as a write."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if path.stat().st_mtime > mtime_before:
                return True
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    try:
        return path.stat().st_mtime > mtime_before
    except FileNotFoundError:
        return False


def _bequest(config, home, *, timeout, send):
    """Ask the running cousin for its bequest and wait, bounded, for
    data/handoff.md to change. Never raises: an unreachable chat server
    or a silent cousin is a recorded outcome, and the flip that follows
    has its own handoff window."""
    handoff = Path(home) / "data" / "handoff.md"
    mtime_before = handoff.stat().st_mtime if handoff.exists() else 0.0
    step = {"step": "bequest", "sent": False, "wrote": False}
    try:
        send(config, BEQUEST_PROMPT.format(timeout=int(timeout)))
        step["sent"] = True
    except (urllib.error.URLError, OSError, ValueError,
            MissingConfigError) as err:
        step["reason"] = "prompt not delivered: %s" % err
        return step
    step["wrote"] = _wait_for_write(handoff, mtime_before, timeout)
    if not step["wrote"]:
        step["reason"] = "no handoff write within %ss" % timeout
    return step


# ---------- rewriting identity ----------

def rewrite_role(claude_md, *, name, new_role):
    """Carry a new role into a CLAUDE.md: the title line the template
    renders as `# <Name> - <role>` and, when the file has one, the body
    of a `## Role` section. Everything else is preserved byte for
    byte; a file with neither returns unchanged."""
    lines = claude_md.splitlines(keepends=True)
    out = []
    i = 0
    title_done = False
    while i < len(lines):
        line = lines[i]
        if (not title_done and i == 0
                and re.match(r"^# .+ - .+", line.rstrip("\n"))):
            newline = "\n" if line.endswith("\n") else ""
            out.append("# %s - %s%s" % (name, new_role.strip(), newline))
            title_done = True
            i += 1
            continue
        out.append(line)
        if line.strip().lower().startswith("## role"):
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            out.append("\n" + new_role.strip() + "\n")
            if i < len(lines):
                out.append("\n")
            continue
        i += 1
    return "".join(out)


def _toml_quote(value):
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _set_cousin_keys(home, **values):
    """Targeted line replaces inside cousin.toml's [cousin] table (a
    missing key is appended to that table), re-parsed before an atomic
    rename: the file is never left half-written or unreadable."""
    path = Path(home) / "cousin.toml"
    text = path.read_text()
    for key, value in values.items():
        line = "%s = %s" % (key, _toml_quote(str(value)))
        table = re.search(r"(?ms)^\[cousin\]\s*$(.*?)(?=^\[|\Z)", text)
        if table is None:
            raise MissingConfigError("no [cousin] table in %s" % path)
        body = table.group(1)
        key_re = re.compile(r"(?m)^%s\s*=.*$" % re.escape(key))
        if key_re.search(body):
            new_body = key_re.sub(lambda m: line, body, count=1)
        else:
            new_body = body.rstrip("\n") + "\n" + line + "\n"
            if not body.rstrip("\n"):
                new_body = "\n" + line + "\n"
        text = text[:table.start(1)] + new_body + text[table.end(1):]
    tomllib.loads(text)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _event(home, kind, content):
    """An L1 raw-memory note of the surgery in the cousin's own home;
    best-effort, never a reason for the ceremony to fail."""
    from cousin_lib import memory
    memory.record_event(home, "L1_FRAMEWORK", "framework:%s" % kind,
                        content, "framework")


# ---------- reincarnate ----------

def reincarnate(slug, *, new_role, root, timeout=BEQUEST_TIMEOUT_SECONDS,
                do_flip=None, send=send_prompt):
    """Snapshot, bequest, rewrite, flip. Returns a structured result;
    ok reflects the flip's own verdict. Refusals come back as a result
    with an error and touch nothing."""
    root = Path(root)
    result = {"op": "reincarnate", "slug": slug, "ok": False, "steps": []}
    if not (new_role or "").strip():
        result["error"] = "an empty --new-role is not a role"
        return result
    try:
        config = _resolve(_registry(root), slug)
    except LifecycleError as err:
        result["error"] = str(err)
        return result
    home = config.home
    base = {"op": "reincarnate", "slug": slug}
    do_flip = do_flip or _default_do_flip(root)

    snap = snapshot(home, root, slug)
    result["snapshot"] = str(snap)
    result["steps"].append({"step": "snapshot", "path": str(snap)})
    _audit(root, dict(base, step="snapshot", path=str(snap)))

    from cousin_lib.delivery import _runner_kind
    bequest_reason = None
    if _runner_kind(home) in ("sdk", "fake"):
        from cousin_lib.runner.rollover import HANDOFF_DEADLINE_S
        bequest_reason = BEQUEST_PROMPT_RUNNER.format(timeout=int(HANDOFF_DEADLINE_S))
        step = {"step": "bequest", "sent": False, "carried": True,
                "skipped": "runner lane: the bequest rides the rollover's handoff request"}
    else:
        step = _bequest(config, home, timeout=timeout, send=send)
    result["steps"].append(step)
    _audit(root, dict(base, **step))

    claude = home / "CLAUDE.md"
    if claude.is_file():
        claude.write_text(rewrite_role(claude.read_text(), name=config.name,
                                       new_role=new_role))
    _set_cousin_keys(home, role=new_role.strip())
    _event(home, "role", "reincarnated: role rewritten to %r"
           % new_role.strip())
    result["steps"].append({"step": "rewrite", "new_role": new_role.strip(),
                            "claude_md": claude.is_file()})
    _audit(root, dict(base, step="rewrite", new_role=new_role.strip()))

    ok, flip_result = _flip_one(root, slug, do_flip, base, reason=bequest_reason)
    result["steps"].append({"step": "flip", "ok": ok})
    result["flip"] = flip_result
    result["ok"] = ok
    if not ok:
        result["error"] = "flip failed: %s" % flip_result.get(
            "error", "see flip result")
    _audit(root, dict(base, step="done", ok=ok))
    return result


# ---------- transplant ----------

def braid_memory(*, donor_md, recipient_md, donor_name, day=None):
    """Merge mode's MEMORY.md: the recipient's timeline first, a dated
    heading, then the donor's - both histories readable, neither
    rewritten."""
    day = day or _now().date().isoformat()
    head = recipient_md.strip()
    body = donor_md.strip()
    heading = "## Memories inherited from %s (%s)" % (donor_name, day)
    return "%s\n\n---\n\n%s\n\n%s\n" % (head, heading, body)


def _copy_memory_over(src_home, dst_home):
    """soul-donation: the recipient's memory becomes the donor's. The
    recipient's own is gone from the home (it lives in the snapshot)."""
    for name in ("MEMORY.md",):
        src = src_home / name
        if src.is_file():
            shutil.copy2(src, dst_home / name)
    dst = dst_home / "memory"
    if dst.exists():
        shutil.rmtree(dst)
    src = src_home / "memory"
    if src.is_dir():
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns(*MEMORY_SKIP))


def _swap_file(a, b):
    """Exchange two paths; an absent side simply moves the other."""
    a, b = Path(a), Path(b)
    if not a.exists() and not b.exists():
        return
    tmp = a.with_name(a.name + ".swap-tmp")
    if a.exists():
        os.replace(a, tmp)
    if b.exists():
        os.replace(b, a)
    if tmp.exists():
        os.replace(tmp, b)


# The one line of templates/cousin-CLAUDE.template.md that renders the
# slot's address ({{PORT}}, {{SLUG}}) into CLAUDE.md.
_ADDRESS_LINE = re.compile(
    r"Your chat-server runs on port \d+ and binds `/api/[A-Za-z0-9_-]+_reply`\.")


def _readdress_body(slot, previous):
    """After a body swap, CLAUDE.md in `slot`'s home came from
    `previous`'s slot and names that slot's port and reply route.
    Re-render the address for the slot it now lives in: the template's
    address line takes the slot's own port and slug, and any other
    `/api/<previous slug>_reply` route (a cousin-specific section may
    repeat it) becomes the slot's own. Identity text is left alone."""
    path = Path(slot.home) / "CLAUDE.md"
    if not path.is_file():
        return
    text = path.read_text()
    new = text.replace("/api/%s_reply" % previous.slug,
                       "/api/%s_reply" % slot.slug)
    if slot.chat_port is not None:
        line = ("Your chat-server runs on port %d and binds"
                " `/api/%s_reply`." % (slot.chat_port, slot.slug))
        new = _ADDRESS_LINE.sub(lambda m: line, new)
    if new != text:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new)
        os.replace(tmp, path)


def _swap_bodies(donor, recipient):
    """body-swap: identity files and the [cousin] name/role trade
    places; slug, port and session stay with each slot, memory stays
    where it is. CLAUDE.md carries the slot's rendered port and reply
    route, so each swapped file is re-addressed to its new slot."""
    for name in IDENTITY_FILES:
        _swap_file(donor.home / name, recipient.home / name)
    _readdress_body(donor, recipient)
    _readdress_body(recipient, donor)
    donor_cfg = tomllib.loads((donor.home / "cousin.toml").read_text())
    recip_cfg = tomllib.loads((recipient.home / "cousin.toml").read_text())
    d_role = donor_cfg.get("cousin", {}).get("role", "")
    r_role = recip_cfg.get("cousin", {}).get("role", "")
    _set_cousin_keys(donor.home, name=recipient.name, role=r_role)
    _set_cousin_keys(recipient.home, name=donor.name, role=d_role)


def _union_raw(src_dir, dst_dir):
    """Union two memory/raw trees: files the recipient lacks are
    copied; files both have gain the donor's lines they were missing,
    in the donor's order. Line identity is byte identity."""
    if not src_dir.is_dir():
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_dir)
        dst = dst_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(src, dst)
            added += 1
            continue
        have = set(dst.read_text().splitlines())
        new = [line for line in src.read_text().splitlines()
               if line and line not in have]
        if new:
            with open(dst, "a") as fh:
                text = dst.read_text()
                if text and not text.endswith("\n"):
                    fh.write("\n")
                fh.write("\n".join(new) + "\n")
            added += len(new)
    return added


def _merge_memory(donor, recipient):
    """merge: braid MEMORY.md and union memory/raw. The donor keeps
    everything; the recipient carries both."""
    d_md = donor.home / "MEMORY.md"
    r_md = recipient.home / "MEMORY.md"
    if d_md.is_file():
        recipient_text = r_md.read_text() if r_md.is_file() else ""
        r_md.write_text(braid_memory(donor_md=d_md.read_text(),
                                     recipient_md=recipient_text,
                                     donor_name=donor.name))
    return _union_raw(donor.home / "memory" / "raw",
                      recipient.home / "memory" / "raw")


def transplant(*, donor, recipient, mode, root, do_flip=None):
    """Snapshot both, apply the mode, flip both (donor first, so the
    recipient boots last into its new interior). Refusals - unknown
    slug, unknown mode, same slug twice - come back as a result with
    an error and touch nothing."""
    root = Path(root)
    result = {"op": "transplant", "donor": donor, "recipient": recipient,
              "mode": mode, "ok": False, "steps": [], "snapshots": {}}
    if mode not in MODES:
        result["error"] = "unknown mode %r (one of %s)" % (
            mode, ", ".join(MODES))
        return result
    if donor == recipient:
        result["error"] = "donor and recipient must differ"
        return result
    try:
        registry = _registry(root)
        d = _resolve(registry, donor)
        r = _resolve(registry, recipient)
    except LifecycleError as err:
        result["error"] = str(err)
        return result
    base = {"op": "transplant", "donor": donor, "recipient": recipient,
            "mode": mode}
    do_flip = do_flip or _default_do_flip(root)

    for cfg in (d, r):
        snap = snapshot(cfg.home, root, cfg.slug)
        result["snapshots"][cfg.slug] = str(snap)
        result["steps"].append({"step": "snapshot", "slug": cfg.slug,
                                "path": str(snap)})
        _audit(root, dict(base, step="snapshot", slug=cfg.slug,
                          path=str(snap)))

    detail = {}
    if mode == "soul-donation":
        _copy_memory_over(d.home, r.home)
    elif mode == "body-swap":
        _swap_bodies(d, r)
    else:
        detail["raw_lines_added"] = _merge_memory(d, r)
    result["steps"].append(dict({"step": "apply"}, **detail))
    _audit(root, dict(base, step="apply", **detail))
    # After the apply, so each note lands in the home it describes (a
    # soul donation replaces the recipient's memory wholesale).
    for cfg, other, side in ((d, r, "donor"), (r, d, "recipient")):
        _event(cfg.home, "transplant", "memory transplant (%s) as %s with"
               " %s%s" % (mode, side, other.slug,
                          "; %d raw lines merged in"
                          % detail["raw_lines_added"]
                          if side == "recipient" and "raw_lines_added"
                          in detail else ""))

    ok = True
    result["flips"] = {}
    for cfg in (d, r):
        flipped, flip_result = _flip_one(root, cfg.slug, do_flip, base)
        result["flips"][cfg.slug] = flip_result
        result["steps"].append({"step": "flip", "slug": cfg.slug,
                                "ok": flipped})
        ok = ok and flipped
    result["ok"] = ok
    if not ok:
        failed = [s for s, fr in result["flips"].items()
                  if not fr.get("ok")]
        result["error"] = "flip failed for %s" % ", ".join(failed)
    _audit(root, dict(base, step="done", ok=ok))
    return result


# ---------- entry points ----------

def _root_or_exit(prog, flag):
    try:
        return FrameworkConfig.resolve(flag, cwd_fallback=True).root
    except MissingConfigError as err:
        print("%s: %s" % (prog, err), file=sys.stderr)
        return None


def _finish(prog, result):
    if "error" in result and not result["steps"]:
        print("%s: %s" % (prog, result["error"]), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 1


@traced_cli("cousin-reincarnate")
def reincarnate_main(argv=None):
    """cousin-reincarnate <slug> --new-role TEXT [--root R]
    [--timeout N]. Operator-driven; a cousin never reincarnates
    itself."""
    parser = argparse.ArgumentParser(
        prog="cousin-reincarnate",
        description="change a cousin's role, keep its memory, flip it")
    parser.add_argument("slug")
    parser.add_argument("--new-role", required=True,
                        help="the new one-line role")
    parser.add_argument("--root", help="framework root (else"
                                       " FRAMEWORK_ROOT)")
    parser.add_argument("--timeout", type=float,
                        default=BEQUEST_TIMEOUT_SECONDS,
                        help="seconds to wait for the bequest"
                             " (default %d)" % BEQUEST_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    root = _root_or_exit(parser.prog, args.root)
    if root is None:
        return 2
    result = reincarnate(args.slug, new_role=args.new_role, root=root,
                         timeout=args.timeout)
    return _finish(parser.prog, result)


@traced_cli("cousin-transplant")
def transplant_main(argv=None):
    """cousin-transplant --donor A --recipient B --mode MODE [--root R]."""
    parser = argparse.ArgumentParser(
        prog="cousin-transplant",
        description="move one cousin's memory or body into another")
    parser.add_argument("--donor", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--root", help="framework root (else"
                                       " FRAMEWORK_ROOT)")
    args = parser.parse_args(argv)
    root = _root_or_exit(parser.prog, args.root)
    if root is None:
        return 2
    result = transplant(donor=args.donor, recipient=args.recipient,
                        mode=args.mode, root=root)
    return _finish(parser.prog, result)
