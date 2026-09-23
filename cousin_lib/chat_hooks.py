"""Chat-pattern hooks: regex-match an inbound chat message and fire a
handler, declaratively, so a recurring reaction does not depend on the
cousin's judgment every time.

The hook file is <home>/chat-hooks.json, a list of entries:

    [
      {
        "pattern": "(?i)\\bprice\\s+check\\b",
        "user": "Sam",
        "handler": "shell:scripts/quote-prices.sh",
        "desc": "quote the tariff when Sam asks"
      },
      {
        "pattern": "(?i)\\bdebug\\s+health\\b",
        "user": "*",
        "handler": "inject:[fw-hook] consider running the health check",
        "desc": "nudge on a debug request"
      }
    ]

`user` is compared case-insensitively; `*` (or an absent key) matches
anyone. Two handler kinds:

- `shell:<path>`: a script, relative to the home or absolute, run
  detached (its own session, stdin closed, reaped by a waiter thread so
  a long-lived server never collects zombies) with COUSIN_HOOK_USER,
  COUSIN_HOOK_MESSAGE, COUSIN_HOOK_PATTERN, COUSIN_SLUG and COUSIN_HOME
  in the environment, stdout and stderr appended to
  <home>/data/chat-hooks.log. A path that resolves outside both the
  home and the framework root is refused: the hook file is data a
  cousin edits, and data must not be able to name /usr/bin/anything.
- `inject:<text>`: the text is handed to the injection seam the server
  passes in, delivered as its own line after the original message.

Best-effort by contract. A missing file, malformed JSON, a malformed
entry, a bad regex, a missing or refused script, a dead injection seam:
each is a no-op that never reaches the message handling that fired it.
A bad regex is reported once per process to stderr, because reporting
it on every message would bury the log and silence would hide a typo
forever.
"""
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from cousin_lib.config import FrameworkConfig, MissingConfigError

HOOKS_FILENAME = "chat-hooks.json"
LOG_RELPATH = Path("data") / "chat-hooks.log"

# The author name an inject line is delivered under, so the cousin can
# tell a hook's reminder from the sender's words.
HOOK_SENDER = "fw-hook"

# Bad patterns already reported this process; a set, not a flag, so a
# second bad hook is still reported once.
_reported = set()


def _report(text):
    print("chat-hooks: %s" % text, file=sys.stderr)


def load_hooks(home):
    """The well-formed entries of <home>/chat-hooks.json, else []."""
    path = Path(home) / HOOKS_FILENAME
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("pattern"), str):
            continue
        if not isinstance(entry.get("handler"), str):
            continue
        out.append(entry)
    return out


def evaluate(hooks, user, message):
    """The hooks matching this (user, message) pair, in file order."""
    matched = []
    sender = (user or "").lower()
    text = message or ""
    for hook in hooks:
        hook_user = hook.get("user", "*")
        if not isinstance(hook_user, str):
            continue
        if hook_user != "*" and hook_user.lower() != sender:
            continue
        pattern = hook["pattern"]
        try:
            hit = re.search(pattern, text)
        except re.error as err:
            if pattern not in _reported:
                _reported.add(pattern)
                _report("bad regex %r skipped: %s" % (pattern, err))
            continue
        if hit:
            matched.append(hook)
    return matched


def fire(matched, *, user, message, slug, home, inject=None):
    """Run every matched handler. `inject` is callable(text) for
    inject: handlers, or None when there is nowhere to deliver."""
    home = Path(home)
    for hook in matched:
        handler = hook.get("handler", "")
        if handler.startswith("shell:"):
            _fire_shell(handler[len("shell:"):], user=user, message=message,
                        slug=slug, home=home,
                        pattern=hook.get("pattern", ""))
        elif handler.startswith("inject:"):
            if inject is None:
                continue
            try:
                inject(handler[len("inject:"):])
            except Exception as err:  # noqa: BLE001 - never fails the send
                _report("inject handler failed: %s" % err)


def on_message(home, *, user, message, message_id, slug, deliver):
    """Evaluate the home's hooks against one stored message and fire
    them. `deliver(user=, message=, message_id=, attachments=)` is the
    send path's own delivery seam (the chat server today, the console in
    phase 5): an inject: handler rides it and lands as a `hook` row.
    Never raises: a failure is reported and nothing else fires."""
    try:
        hooks = load_hooks(home)
        matched = evaluate(hooks, user, message)
        if not matched:
            return []
        inject = None
        if deliver is not None:
            def inject(text):
                deliver(user=HOOK_SENDER, message=text, message_id=message_id,
                        attachments=[])
        fire(matched, user=user, message=message, slug=slug, home=home,
             inject=inject)
        return matched
    except Exception as err:  # noqa: BLE001 - never fails the send
        _report("skipped: %s" % err)
        return []


def _allowed_roots(home):
    """Where a shell: script may live: the home, and the framework root
    when one is configured. Nothing else, however the path is spelled."""
    roots = [home.resolve()]
    try:
        roots.append(FrameworkConfig.resolve().root.resolve())
    except MissingConfigError:
        pass
    return roots


def _fire_shell(script, *, user, message, slug, home, pattern):
    candidate = Path(script)
    if not candidate.is_absolute():
        candidate = home / candidate
    try:
        resolved = candidate.resolve()
    except OSError as err:
        _report("shell handler %r unusable: %s" % (script, err))
        return
    if not any(resolved.is_relative_to(root) for root in _allowed_roots(home)):
        _report("shell handler %r refused: outside the home and the "
                "framework root" % script)
        return
    if not resolved.is_file():
        return
    env = os.environ.copy()
    env.update({
        "COUSIN_HOOK_USER": user or "",
        "COUSIN_HOOK_MESSAGE": message or "",
        "COUSIN_HOOK_PATTERN": pattern,
        "COUSIN_SLUG": slug,
        "COUSIN_HOME": str(home),
    })
    log_path = home / LOG_RELPATH
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            child = subprocess.Popen(
                [str(resolved)],
                env=env,
                cwd=str(home),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError as err:
        _report("shell handler %r did not start: %s" % (script, err))
        return
    # The child is nobody's concern once started, but an unreaped child
    # is a zombie for the life of the server: wait on it off-thread.
    threading.Thread(target=child.wait, daemon=True).start()
