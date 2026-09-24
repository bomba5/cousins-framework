"""The tmux kind's pane (phase 11 Task 2, interfaces I3): an interactive
Claude Code in a tmux session on the framework's own socket.

What it may do is narrow on purpose. It starts the CLI with an
allowlisted environment set INSIDE the pane (`exec env -i ...`), because
tmux runs a command through the login shell, which rebuilds the
environment (phase 11 findings S0+). It types a row as one typed line
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
import shlex
import subprocess
from pathlib import Path
from typing import Protocol

WIDTH, HEIGHT = 200, 50
KEYS = ("Escape", "C-u", "Enter")
QUEUED_HINT = "Press up to edit queued messages"
PROMPT = "❯ "
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


class Outcome(enum.Enum):
    TYPED = "typed"
    BLOCKED = "blocked"
    FAILED = "failed"


class Pane(Protocol):
    def alive(self) -> bool: ...
    def pid(self) -> int | None: ...
    def start(self, argv, *, cwd, env_base) -> None: ...
    def kill(self) -> None: ...
    def capture(self) -> str: ...
    def box_text(self) -> str | None: ...
    def queued(self) -> bool: ...
    def attention(self) -> str | None: ...
    def type_row(self, first_line, body) -> Outcome: ...
    def key(self, name) -> None: ...
    def clear(self) -> None: ...


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

    def _tmux(self, *args, input=None):
        return subprocess.run([self.tmux_bin, "-S", str(self.socket), *args], capture_output=True,
                              text=True, input=input, timeout=self.timeout, check=False)

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
        try:
            return self._tmux("has-session", "-t", self._session()).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def pid(self):
        r = self._tmux("display-message", "-p", "-t", self._target(), "#{pane_pid}")
        try:
            return int((r.stdout or "").strip()) if r.returncode == 0 else None
        except ValueError:
            return None

    def start(self, argv, *, cwd, env_base):
        self.socket.parent.mkdir(parents=True, exist_ok=True)
        self.socket.parent.chmod(0o700)
        env = ["%s=%s" % (k, v) for k, v in sorted(env_base.items())]
        command = "exec env -i " + shlex.join(env + list(argv))
        r = self._tmux("new-session", "-d", "-s", self.name, "-x", str(self.width),
                       "-y", str(self.height), "-c", str(cwd), command)
        if r.returncode != 0:
            raise OSError("tmux new-session failed (rc=%d): %s" % (r.returncode, (r.stderr or "").strip()[:200]))
        self._tmux("set-option", "-t", self._target(), "window-size", "manual")
        self._tmux("resize-window", "-t", self._target(), "-x", str(self.width), "-y", str(self.height))

    def kill(self):
        self._tmux("kill-session", "-t", self._session())

    def capture(self):
        r = self._tmux("capture-pane", "-p", "-t", self._target())
        return (r.stdout or "") if r.returncode == 0 else ""

    def box_text(self):
        return box_in(self.capture())

    def queued(self):
        return QUEUED_HINT in self.capture()

    def attention(self):
        return attention_in(self.capture())

    def type_row(self, first_line, body):
        """Type `first_line` as keys, paste `body` bracketed after a blank
        line, press Enter. BLOCKED (nothing sent) on an attention screen, a
        box that is not empty, or queued input; FAILED when tmux fails."""
        try:
            screen = self.capture()
            if attention_in(screen) or box_in(screen) != "" or QUEUED_HINT in screen:
                return Outcome.BLOCKED
            if self._tmux("send-keys", "-t", self._target(), "-l", first_line).returncode != 0:
                return Outcome.FAILED
            if body:
                buf = "cousin-%s" % self.name
                if self._tmux("load-buffer", "-b", buf, "-", input="\n\n" + body).returncode != 0:
                    return Outcome.FAILED
                if self._tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", self._target()).returncode != 0:
                    return Outcome.FAILED
            if self._tmux("send-keys", "-t", self._target(), "Enter").returncode != 0:
                return Outcome.FAILED
            return Outcome.TYPED
        except (OSError, subprocess.SubprocessError):
            return Outcome.FAILED

    def key(self, name):
        if name not in KEYS:
            raise ValueError("key %r is not one the runner sends (%s)" % (name, ", ".join(KEYS)))
        self._tmux("send-keys", "-t", self._target(), name)

    def clear(self):
        self.key("C-u")
