"""The tmux kind's pane (phase 11 Task 2, interfaces I3): an interactive
Claude Code in a tmux session on the framework's own socket.

What it may do is narrow on purpose. It starts the CLI with an
allowlisted environment set INSIDE the pane (`exec env -i ...`), because
tmux runs a command through the login shell, which rebuilds the
environment (phase 11 findings S0+). Only variable NAMES reach tmux: each
is expanded by that login shell (`${NAME+"NAME=$NAME"}`), so no value, and
no secret, is ever on a tmux command line (`#{pane_start_command}` keeps it
for the pane's life); a denied name (DENY_PREFIXES, accounts.AUTH_VARS) is
refused. The framework's server starts with `-f /dev/null`, so no
~/.tmux.conf changes its shell or its window size. It types a row as one typed line
(the sender's own words, outside `<pasted_content>`) plus a bracketed
paste of the body, and never into a screen that is waiting on a person:
the trust dialog, onboarding, the login menu (whose option 2 is API
billing), the bypass and MCP dialogs, a usage-limit screen, or the rewind
selector a double Escape opens (findings S0, I6a, Z7). Its keys are an
allowlist (Escape, C-u, Enter). It reads the screen at a fixed window size,
so an attached terminal cannot change what it parses.

It never decides whether a prompt was received: that is the transcript's
(runner/transcript.py)."""
import enum
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Protocol

WIDTH, HEIGHT = 200, 50
KEYS = ("Escape", "C-u", "Enter")
QUEUED_HINT = "Press up to edit queued messages"
PROMPT = "❯ "
# The hard deny (R3, P11-12), in ONE place: tmux_launch imports it. A name
# with one of these prefixes, an auth variable or a credential-shaped name
# (accounts.credential_name, opencode's rule too) never enters the pane's
# environment from the shell; the launcher adds only the account's own.
DENY_PREFIXES = ("CLAUDE", "ANTHROPIC")      # CLAUDECODE, CLAUDE_AGENT_SDK_* included
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# What never reaches the pane as a key or inside a paste (review C4): ESC
# and every other C0 control but \n and \t, DEL, and the C1 controls. An
# ESC[201~ in a paste ends the bracketed paste early (tmux 3.6a passes it
# through, measured), and a CR after it submits the rest as typed input
# (`!cmd` runs a shell with no model and no policy, `/login`, `/clear`).
_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
RULE_CHAR = "─"

# (id, substrings that must all appear); `rewind` is the only one the
# runner answers (one Escape); every other waits for a person
ATTENTION = (
    ("trust", ("Quick safety check",)),
    ("onboarding", ("Choose the text style",)),
    ("login", ("Select login method",)),
    ("bypass", ("Bypass Permissions mode",)),
    ("mcp_approval", ("New MCP server",)),
    ("limit", ("usage limit",)),
    ("limit", ("limit reached",)),
    ("limit", ("resets at",)),
    ("limit", ("extra usage",)),
    ("rewind", ("Enter to continue", "(current)")),
)


def denied(name):
    from cousin_lib import accounts
    return name.startswith(DENY_PREFIXES) or accounts.credential_name(name)


def env_command(names, argv):
    """The pane's command: `exec env -i` with each allowlisted NAME expanded
    by the pane's login shell (an unset name is left out), plus TERM from
    tmux, then argv. Raises ValueError on a denied or malformed name."""
    names = [n for n in dict.fromkeys(names) if n != "TERM"]
    for n in names:
        if not _NAME.match(n):
            raise ValueError("not a variable name: %r" % (n,))
        if denied(n):
            raise ValueError("%s is denied in the pane's environment (R3)" % n)
    words = ['${%s+"%s=$%s"}' % (n, n, n) for n in names + ["TERM"]]
    return "exec env -i " + " ".join(words + [shlex.quote(a) for a in argv])


class Outcome(enum.Enum):
    TYPED = "typed"
    BLOCKED = "blocked"
    FAILED = "failed"


class Pane(Protocol):
    def alive(self) -> bool: ...
    def pid(self) -> int | None: ...
    def start(self, argv, *, cwd, env_base) -> None: ...
    def kill(self) -> None: ...
    def process_alive(self, pid) -> bool: ...
    def process_kill(self, pid) -> None: ...
    def capture(self) -> str: ...
    def box_text(self) -> str | None: ...
    def queued(self) -> bool: ...
    def attention(self) -> str | None: ...
    def type_row(self, first_line, body) -> Outcome: ...
    def key(self, name) -> None: ...
    def clear(self) -> None: ...


def process_alive(pid):
    """True while `pid` is a process that can still run: /proc/<pid> is
    there and not a zombie (a zombie writes nothing). Without /proc, a
    signal 0 answers."""
    if not os.path.isdir("/proc/self"):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        stat = Path("/proc/%d/stat" % pid).read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except IndexError:
        return True


def process_kill(pid):
    """SIGKILL `pid`; one already gone, or not ours, is left as it is."""
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def printable(text):
    """`text` without ESC, the C0 controls but \\n and \\t, DEL and C1."""
    return _CONTROLS.sub("", text)


def attention_in(screen):
    for ident, needles in ATTENTION:
        if all(n in screen for n in needles):
            return ident
    return None


def box_in(screen):
    """The input box's text: the prompt line between the last two rules;
    "" when empty, None when no box is on screen."""
    lines = screen.splitlines()
    rules = [i for i, l in enumerate(lines) if l.strip().startswith(RULE_CHAR * 3)]
    if len(rules) < 2:
        return None
    for line in lines[rules[-2] + 1:rules[-1]]:
        s = line.lstrip()
        if s.startswith(PROMPT.rstrip()):
            return s[len(PROMPT.rstrip()):].strip()
    return None


class TmuxPane:
    def __init__(self, socket, name, *, width=WIDTH, height=HEIGHT, tmux_bin="tmux", timeout=5.0):
        self.socket = Path(socket)
        self.name = name
        self.width, self.height = width, height
        self.tmux_bin = tmux_bin
        self.timeout = timeout
        self._residue = None      # a first line typed into a box a dialog then took

    def _tmux(self, *args, input=None):
        """One tmux call; a hung or missing tmux is a failed call (rc -1),
        never a raise, so stop and interrupt survive it."""
        argv = [self.tmux_bin, "-f", "/dev/null", "-S", str(self.socket), *args]
        try:
            return subprocess.run(argv, capture_output=True, text=True, input=input,
                                  timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(argv, -1, "", "%s: %s" % (type(exc).__name__, exc))

    def _session(self):
        """A session target, exact match (has-session, kill-session)."""
        return "=" + self.name

    def _target(self):
        """A pane or window target: the exact session, its current window.
        tmux 3.6a answers `-t =name` with nothing for pane commands
        (display-message prints an empty line, capture-pane fails);
        `=name:` keeps the exact match and works (measured, phase 11)."""
        return "=" + self.name + ":"

    def alive(self):
        return self._tmux("has-session", "-t", self._session()).returncode == 0

    def pid(self):
        r = self._tmux("display-message", "-p", "-t", self._target(), "#{pane_pid}")
        try:
            return int((r.stdout or "").strip()) if r.returncode == 0 else None
        except ValueError:
            return None

    def start(self, argv, *, cwd, env_base):
        """`env_base` is the allowlist of NAMES (a dict's keys; its values
        are never used): the pane's login shell supplies the values."""
        command = env_command(sorted(env_base), argv)
        self.socket.parent.mkdir(parents=True, exist_ok=True)
        self.socket.parent.chmod(0o700)
        for args in (("new-session", "-d", "-s", self.name, "-x", str(self.width),
                      "-y", str(self.height), "-c", str(cwd), command),
                     ("set-option", "-t", self._target(), "window-size", "manual"),
                     ("resize-window", "-t", self._target(), "-x", str(self.width), "-y", str(self.height))):
            r = self._tmux(*args)
            if r.returncode != 0:     # the fixed size is what the screen parsers rely on (R25)
                raise OSError("tmux %s failed (rc=%d): %s" % (args[0], r.returncode,
                                                             (r.stderr or "").strip()[:200]))

    def kill(self):
        self._tmux("kill-session", "-t", self._session())

    def process_alive(self, pid):
        return process_alive(pid)

    def process_kill(self, pid):
        process_kill(pid)

    def capture(self):
        """The screen, or None when there is no pane to read."""
        r = self._tmux("capture-pane", "-p", "-t", self._target())
        return (r.stdout or "") if r.returncode == 0 else None

    def box_text(self):
        screen = self.capture()
        return None if screen is None else box_in(screen)

    def queued(self):
        return QUEUED_HINT in (self.capture() or "")

    def attention(self):
        return attention_in(self.capture() or "")

    def type_row(self, first_line, body):
        """Type `first_line` as keys, paste `body` bracketed after a blank
        line, press Enter. Both lose ESC and the other controls first
        (printable). BLOCKED (nothing sent) on an attention screen, a box
        that is not empty, or queued input; BLOCKED with nothing more sent
        when the box is gone once the first line is in (a dialog took it;
        that line is cleared before the next row once the box is back);
        FAILED when there is no pane to read, the first line holds a newline
        (it would submit early), or tmux fails; any failure from the first
        key onward clears the box (C-u), so the next row is not blocked by
        the leftovers."""
        first_line, body = printable(first_line), printable(body or "")
        if "\n" in first_line:
            return Outcome.FAILED
        screen = self.capture()
        if screen is None:
            return Outcome.FAILED
        box = box_in(screen)
        if box and self._residue and self._residue.startswith(box) \
                and not attention_in(screen) and QUEUED_HINT not in screen:
            self.clear()                  # our own first line, left by a blocked row
            screen = self.capture()
            if screen is None:
                return Outcome.FAILED
            box = box_in(screen)
        self._residue = None
        if attention_in(screen) or box != "" or QUEUED_HINT in screen:
            return Outcome.BLOCKED
        if self._tmux("send-keys", "-t", self._target(), "-l", first_line).returncode != 0:
            self.clear()
            return Outcome.FAILED
        screen = self.capture()
        if screen is None:
            return Outcome.FAILED
        if attention_in(screen) or box_in(screen) is None:
            self._residue = first_line    # nothing more typed into whatever took the box
            return Outcome.BLOCKED
        steps = []
        if body:
            buf = "cousin-%s" % self.name
            steps += [(("load-buffer", "-b", buf, "-"), "\n\n" + body),
                      (("paste-buffer", "-p", "-d", "-b", buf, "-t", self._target()), None)]
        steps.append((("send-keys", "-t", self._target(), "Enter"), None))
        for args, stdin in steps:
            if self._tmux(*args, input=stdin).returncode != 0:
                self.clear()
                return Outcome.FAILED
        return Outcome.TYPED

    def key(self, name):
        if name not in KEYS:
            raise ValueError("key %r is not one the runner sends (%s)" % (name, ", ".join(KEYS)))
        self._tmux("send-keys", "-t", self._target(), name)

    def clear(self):
        self.key("C-u")
