#!/usr/bin/env python3
"""The README's Docker quick start, run as CI (.github/workflows/quickstart.yml).

A CI helper, not a user-facing tool: it ships with the checkout, never in
the image. The README is the only copy of the quick start: `extract` pulls
the one fenced block under the heading "## Quick start: Docker" and writes
it to a script, so the job runs what a stranger reads and cannot drift
from it.

    quickstart.py extract --tier 1|2 --out FILE [--readme README.md]
    quickstart.py run FILE [--timeout S]
    quickstart.py healthy [--timeout S]
    quickstart.py login
    quickstart.py converse --tier 1|2 [--timeout S] [--retries N]
    quickstart.py remember --state FILE
    quickstart.py recall --state FILE

`extract` drops the `git clone` and `cd` lines (the job is already in the
checkout) and, for tier 1 only, swaps the spawn line's opencode lane for
the fake runner (`--runner fake`); each of those edits must match exactly
once, so an edited README line turns the job red instead of skipping.
`run` runs the script as `bash -euo pipefail FILE` under a pty and types
QUICKSTART_PASSWORD at `cousin-console adduser`'s two prompts. The rest are
the checks after the block, against the compose project in the current
directory: the console on CONSOLE (default http://127.0.0.1:8600), user
QUICKSTART_USER (default ana), cousin QUICKSTART_COUSIN (default wren).

Exit codes: 0 green, 1 a failed step or check (its `::error::` line says
which), 2 bad usage.
"""
import argparse
import http.cookiejar
import json
import os
import pathlib
import pty
import re
import select
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

HEADING = "## Quick start: Docker"
FENCE = "```"

# The two lines a stranger types before the block can run; the job is
# already in a checkout. Each must match exactly once.
CLONE = re.compile(r"^git clone [^\n]*\n", re.M)
CD = re.compile(r"^cd [^\n]*\n", re.M)

# Tier 1's one substitution: the spawn line's opencode lane (account and
# model included) becomes the fake runner, which runs no model and needs
# no account (runner/main.py still checks the lane, so the fake gets none).
# The flags may sit on one line or across a `\` continuation.
_GAP = r"(?:[ \t]+|[ \t]*\\\n[ \t]*)"
LANE = re.compile(r"--runner opencode" + _GAP + r"--account zen" + _GAP
                  + r"--model opencode/big-pickle")
FAKE_LANE = "--runner fake"

# The script's head: on a failing command it names the README step, so a
# red run reads as "compose build", "adduser" or "spawn" at a glance and a
# network flake can be told from a regression.
HEAD = r"""# Extracted from README.md "%(heading)s" by .github/scripts/quickstart.py
# (tier %(tier)s%(note)s). Run as: bash -euo pipefail <this file>
quickstart_failed() {
    rc=$1 cmd=$2
    case "$cmd" in
        *"compose up"*) step="compose build" ;;
        *adduser*) step="adduser" ;;
        *accounts.toml*) step="accounts.toml" ;;
        *cousin-spawn*) step="spawn" ;;
        *) step="a quick start command" ;;
    esac
    printf '::error title=quick start::failed at %%s (exit %%s): %%s\n' "$step" "$rc" "$cmd" >&2
}
trap 'quickstart_failed "$?" "$BASH_COMMAND"' ERR

"""

PROMPTS = re.compile(r"password for [^\s:]+: |again: ")

CONSOLE = os.environ.get("CONSOLE", "http://127.0.0.1:8600")
USER = os.environ.get("QUICKSTART_USER", "ana")
COUSIN = os.environ.get("QUICKSTART_COUSIN", "wren")


class StepError(Exception):
    """A red step: its message is the `::error::` line."""


# -- extract --------------------------------------------------------------

def quick_start_block(readme_text):
    """The text of the one fenced block under HEADING, or StepError: the
    heading missing or doubled, no block, more than one, an unclosed
    fence, or a block with nothing in it."""
    lines = readme_text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(HEADING)]
    if len(starts) != 1:
        raise StepError("README.md has %d headings starting %r, not 1"
                        % (len(starts), HEADING))
    section = []
    for line in lines[starts[0] + 1:]:
        if line.startswith("## "):
            break
        section.append(line)
    blocks, current = [], None
    for line in section:
        if line.lstrip().startswith(FENCE):
            if current is None:
                current = []
            else:
                blocks.append("".join(current))
                current = None
        elif current is not None:
            current.append(line)
    if current is not None:
        raise StepError("the %r section has an unclosed code fence" % HEADING)
    if len(blocks) != 1:
        raise StepError("the %r section has %d fenced blocks, not 1"
                        % (HEADING, len(blocks)))
    if not blocks[0].strip():
        raise StepError("the %r section's code block is empty" % HEADING)
    return blocks[0]


def _sub_once(pattern, replacement, text, what):
    out, n = pattern.subn(replacement, text)
    if n != 1:
        raise StepError("%s matched %d times in the quick start, not 1" % (what, n))
    return out


def transform(block, tier):
    """The block as the job runs it: clone and cd dropped; tier 1 swaps the
    spawn line's lane for the fake runner. Every edit matches exactly once."""
    out = _sub_once(CLONE, "", block, "the `git clone` line")
    out = _sub_once(CD, "", out, "the `cd` line")
    if tier == 1:
        out = _sub_once(LANE, FAKE_LANE, out,
                        "tier 1's substitution (%s)" % LANE.pattern)
    return out


def script_for(readme_text, tier):
    note = ", spawn lane -> %s" % FAKE_LANE if tier == 1 else ", literal"
    return (HEAD % {"heading": HEADING, "tier": tier, "note": note}
            + transform(quick_start_block(readme_text), tier))


def parses(script_text):
    """StepError unless bash parses the script: a quoting break in the
    README is red here, before anything runs, not halfway through."""
    proc = subprocess.run(["bash", "-n"], input=script_text, capture_output=True, text=True)
    if proc.returncode != 0:
        raise StepError("the quick start does not parse as bash: %s" % proc.stderr.strip())


def cmd_extract(args):
    text = script_for(pathlib.Path(args.readme).read_text(), args.tier)
    parses(text)
    pathlib.Path(args.out).write_text(text)
    print(text, end="")
    return 0


# -- run ------------------------------------------------------------------

def run_under_pty(script, password, *, timeout=1800.0, out=None, env=None):
    """`bash -euo pipefail script` under a pty, its output copied to `out`
    (a binary stream); each password prompt answered with `password`. The
    script's exit status, or 124 when it outlives `timeout`."""
    out = out if out is not None else sys.stdout.buffer
    env = dict(os.environ if env is None else env)
    env.setdefault("TERM", "dumb")
    pid, fd = pty.fork()
    if pid == 0:  # the child: its controlling terminal is the pty
        try:
            os.execvpe("bash", ["bash", "-euo", "pipefail", str(script)], env)
        finally:
            os._exit(127)
    seen = ""
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([fd], [], [], min(left, 1.0))
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # EIO: the child closed the pty
                break
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            seen += chunk.decode("utf-8", "replace")
            match = PROMPTS.search(seen)
            if match:
                os.write(fd, (password + "\n").encode())
                seen = seen[match.end():]
            seen = seen[-512:]
    finally:
        if timed_out:  # the child leads its own session: its group is its pid
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    if timed_out:
        return 124
    return os.waitstatus_to_exitcode(status)


def cmd_run(args):
    password = os.environ.get("QUICKSTART_PASSWORD") or ""
    if not password:
        raise StepError("QUICKSTART_PASSWORD is not set: adduser's prompts need it")
    started = time.monotonic()
    code = run_under_pty(args.script, password, timeout=args.timeout)
    took = time.monotonic() - started
    if code == 124:
        raise StepError("the quick start ran past %d s" % args.timeout)
    if code != 0:
        # the script's own ERR trap already named the step
        print("quick start: exit %d after %.0f s" % (code, took), file=sys.stderr)
        return 1
    print("quick start: green in %.0f s" % took)
    return 0


# -- the checks -----------------------------------------------------------

def compose(*args, input=None, check=True, timeout=300):
    try:
        proc = subprocess.run(["docker", "compose"] + list(args), input=input,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # a hung exec names its command, not a bare traceback
        raise StepError("docker compose %s: no answer in %ds" % (" ".join(args), timeout))
    if check and proc.returncode != 0:
        raise StepError("docker compose %s: exit %d: %s"
                        % (" ".join(args), proc.returncode,
                           (proc.stderr or proc.stdout).strip()[-800:]))
    return proc


def in_framework(*argv, input=None, check=True):
    """A command inside the framework container, from /data (the root)."""
    return compose("exec", "-T", "framework", *argv, input=input, check=check)


def health():
    cid = compose("ps", "-q", "framework").stdout.strip()
    if not cid:
        return "no container"
    proc = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", cid],
                          capture_output=True, text=True, timeout=60)
    return proc.stdout.strip() or proc.stderr.strip()


def cmd_healthy(args):
    """Check 1 (and checks 4 and 5 after their restarts): the image's
    HEALTHCHECK (GET /api/version) passes. Docker resets a container's
    health to `starting` on every start, so a pass is this start's."""
    deadline = time.monotonic() + args.timeout
    started = time.monotonic()
    status = None
    while time.monotonic() < deadline:
        status = health()
        if status == "healthy":
            print("check %s: framework healthy after %.0f s"
                  % (args.check, time.monotonic() - started))
            return 0
        if status == "unhealthy":
            break
        time.sleep(2)
    raise StepError("check %s: the framework container is %s, not healthy, after %d s"
                    % (args.check, status, args.timeout))


class Console:
    """The console's HTTP API with a cookie jar, as the browser uses it."""

    def __init__(self, base=CONSOLE):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "replace")
            try:
                return err.code, json.loads(text)
            except ValueError:
                return err.code, text

    def login(self, password):
        status, body = self.call("POST", "/api/auth/login",
                                 {"user": USER, "password": password})
        if status != 200:
            raise StepError("check 2: POST /api/auth/login as %s: %s %s"
                            % (USER, status, body))
        status, me = self.call("GET", "/api/auth/me")
        if status != 200 or not isinstance(me, dict) or me.get("user") != USER:
            raise StepError("check 2: /api/auth/me after the login: %s %s" % (status, me))
        if me.get("configured") is not True:
            raise StepError("check 2: the console is not configured for users: %s" % me)
        return me

    def send(self, message):
        status, body = self.call("POST", "/api/chat/send",
                                 {"cousin": COUSIN, "user": USER, "message": message})
        if status != 200 or not isinstance(body, dict) or not body.get("ok") \
                or not isinstance(body.get("id"), int):
            raise StepError("check 3: POST /api/chat/send to %s: %s %s"
                            % (COUSIN, status, body))
        return body


def _password():
    password = os.environ.get("QUICKSTART_PASSWORD") or ""
    if not password:
        raise StepError("QUICKSTART_PASSWORD is not set")
    return password


def cmd_login(args):
    """Check 2: log in as the user adduser made; a wrong password is refused."""
    console = Console()
    # Refused is the check, not the status: the console under the
    # supervisor answers a route's own refusal with 500, not its 401.
    # Tighten to 401 in the fix for that (route errors under python -m).
    wrong, _ = console.call("POST", "/api/auth/login",
                            {"user": USER, "password": _password() + "-wrong"})
    status, me = console.call("GET", "/api/auth/me")
    if wrong == 200 or (isinstance(me, dict) and me.get("user")):
        raise StepError("check 2: a wrong password logged in (%s, me %s)" % (wrong, me))
    console.login(_password())
    status, body = Console().call("GET", "/api/messages?cousin=%s&user=%s" % (COUSIN, USER))
    if status != 401:
        raise StepError("check 2: /api/messages without the cookie got %s, not 401" % status)
    print("check 2: logged in as %s; a wrong password (answered %s) and no cookie (401)"
          " are refused" % (USER, wrong))
    return 0


def stream_events():
    proc = in_framework("cousin-watch", COUSIN, "--json", "--tail", "0", check=False)
    if proc.returncode != 0:
        # a stream not written yet is exit 0 with no lines; anything else is
        # the tool failing, and must not read as "the turn never came"
        raise StepError("cousin-watch %s: exit %d: %s" % (
            COUSIN, proc.returncode, (proc.stderr or proc.stdout).strip()[-400:]))
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


_ROW = """
import json, sys
from cousin_lib.runner.inbox import Inbox
rows = [r for r in (Inbox("cousins/%s").get(i) for i in json.load(sys.stdin)) if r]
print(json.dumps([{k: r[k] for k in ("id", "message_id", "state", "outcome")} for r in rows]))
"""


def inbox_rows(ids):
    proc = in_framework("python", "-c", _ROW % COUSIN, input=json.dumps(ids))
    return json.loads(proc.stdout)


def turn_for(events, message):
    """The (turn_start, result) pair whose turn carried `message`, or None
    for a part not yet in the stream."""
    start = next((e for e in events if e.get("kind") == "turn_start"
                  and message in (e.get("payload") or {}).get("bodies", [])), None)
    if start is None:
        return None, None
    ids = set(start["payload"].get("inbox_ids") or [])
    result = next((e for e in events if e.get("kind") == "result"
                   and e.get("seq", 0) > start.get("seq", 0)
                   and ids & set((e.get("payload") or {}).get("inbox_ids") or [])), None)
    return start, result


def converse_fake(console, timeout):
    """Tier 1's check 3: the fake runner writes no chat reply; its turn is
    the proof. The message's inbox row opens a turn_start that carries it,
    a result with is_error false names that row, and the row is closed
    `delivered`."""
    message = "hi"
    receipt = console.send(message)
    deadline = time.monotonic() + timeout
    start = result = None
    while time.monotonic() < deadline:
        start, result = turn_for(stream_events(), message)
        if result is not None:
            break
        time.sleep(2)
    if start is None:
        raise StepError("check 3: no turn_start carrying %r in %s's stream after %d s"
                        % (message, COUSIN, timeout))
    if result is None:
        raise StepError("check 3: the turn started (%s) but no result in %d s"
                        % (start["payload"], timeout))
    payload = result["payload"]
    if payload.get("is_error") is not False or payload.get("interrupted") is not False:
        raise StepError("check 3: the turn's result is not clean: %s" % payload)
    rows = []
    while time.monotonic() < deadline:
        rows = inbox_rows(payload["inbox_ids"])
        mine = [r for r in rows if r["message_id"] == receipt["id"]]
        if mine and all(r["state"] == "done" for r in mine):
            break
        time.sleep(1)
    mine = [r for r in rows if r["message_id"] == receipt["id"]]
    if not mine or any((r["state"], r["outcome"]) != ("done", "delivered") for r in mine):
        raise StepError("check 3: message %s's inbox rows are not closed delivered: %s"
                        % (receipt["id"], rows))
    print("check 3: message %s reached the fake runner; turn %s closed it delivered"
          % (receipt["id"], payload["inbox_ids"]))


def converse_live(console, timeout, retries):
    """Tier 2's check 3: a non-empty reply from the cousin on the chat
    surface. A retry (the free model's vendor rate-limits now and then)
    sends again and is logged as one."""
    for attempt in range(retries + 1):
        if attempt:
            print("::warning title=quick start check 3::retry %d of %d: no reply from %s"
                  " in %d s; sending again" % (attempt, retries, COUSIN, timeout))
        receipt = console.send("hi")
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        while time.monotonic() < deadline:
            status, body = console.call(
                "GET", "/api/messages?cousin=%s&user=%s&since=%d" % (COUSIN, USER, receipt["id"]))
            if status != 200:
                raise StepError("check 3: GET /api/messages: %s %s" % (status, body))
            replies = [m for m in body.get("messages") or []
                       if m.get("type") == COUSIN
                       and ((m.get("message") or "").strip() or m.get("attachment_path"))]
            if replies:
                print("check 3: %s replied in %.0f s%s: %r"
                      % (COUSIN, time.monotonic() - started,
                         " (after %d retr%s)" % (attempt, "y" if attempt == 1 else "ies")
                         if attempt else "", replies[0].get("message", "")[:200]))
                return
            time.sleep(3)
    tail = stream_events()[-20:]
    raise StepError("check 3: reply timeout: no reply from %s in %d s, %d attempt(s);"
                    " the stream's last events: %s"
                    % (COUSIN, timeout, retries + 1, json.dumps(tail)[-1500:]))


def cmd_converse(args):
    console = Console()
    console.login(_password())
    if args.tier == 1:
        converse_fake(console, args.timeout)
    else:
        converse_live(console, args.timeout, args.retries)
    return 0


def cmd_remember(args):
    """Check 4's write: one memory entry for the cousin, its id kept in
    `--state` for the reads after the restarts."""
    token = uuid.uuid4().hex
    topic = "quickstart-ci-" + token[:12]
    fact = "the quick start check wrote %s" % token
    home = "cousins/%s" % COUSIN
    in_framework("cousin-memory", "--home", home, "remember", topic, fact)
    lines = in_framework("cousin-memory", "--home", home, "history", topic).stdout.splitlines()
    if len(lines) != 1 or token not in lines[0]:
        raise StepError("check 4: `history %s` after the write: %r" % (topic, lines))
    entry_id = lines[0].split()[0]
    pathlib.Path(args.state).write_text(json.dumps(
        {"id": entry_id, "topic": topic, "token": token}))
    print("check 4: remembered %s as entry %s" % (topic, entry_id))
    return 0


def cmd_recall(args):
    """Checks 4 and 5's read: the entry, by id, as written."""
    state = json.loads(pathlib.Path(args.state).read_text())
    proc = in_framework("cousin-memory", "--home", "cousins/%s" % COUSIN,
                        "why", state["id"], "--json", check=False)
    if proc.returncode != 0:
        raise StepError("check %s: memory entry %s is gone: %s"
                        % (args.check, state["id"], (proc.stderr or proc.stdout).strip()))
    entry = json.loads(proc.stdout)["entry"]
    if entry.get("topic") != state["topic"] or state["token"] not in entry.get("content", ""):
        raise StepError("check %s: entry %s reads back as %s" % (args.check, state["id"], entry))
    print("check %s: memory entry %s read back intact" % (args.check, state["id"]))
    return 0


# -- cli ------------------------------------------------------------------

def _parser():
    parser = argparse.ArgumentParser(prog="quickstart.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extract")
    p.add_argument("--tier", type=int, choices=(1, 2), required=True)
    p.add_argument("--readme", default="README.md")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser("run")
    p.add_argument("script")
    p.add_argument("--timeout", type=float, default=1800)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("healthy")
    p.add_argument("--timeout", type=float, default=180)
    p.add_argument("--check", default="1", help="the check number the output names")
    p.set_defaults(func=cmd_healthy)
    p = sub.add_parser("login")
    p.set_defaults(func=cmd_login)
    p = sub.add_parser("converse")
    p.add_argument("--tier", type=int, choices=(1, 2), required=True)
    p.add_argument("--timeout", type=float, default=None,
                   help="per attempt; default 120 s for tier 1, 180 s for tier 2")
    p.add_argument("--retries", type=int, default=1, help="tier 2 only")
    p.set_defaults(func=cmd_converse)
    p = sub.add_parser("remember")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_remember)
    p = sub.add_parser("recall")
    p.add_argument("--state", required=True)
    p.add_argument("--check", default="4", help="the check number the output names")
    p.set_defaults(func=cmd_recall)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if getattr(args, "cmd", None) == "converse" and args.timeout is None:
        args.timeout = 120 if args.tier == 1 else 180
    try:
        return args.func(args)
    except StepError as err:
        print("::error title=quick start::%s" % err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
