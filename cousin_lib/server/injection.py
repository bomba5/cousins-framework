"""Terminal delivery: compose the chat line, then type it into tmux.

Inbound messages reach the cousin as one line typed into its tmux
session. Composition and wiring are separate concerns: composition is
pure (and carries the recall seam), the injector owns the subprocess
sequence and its timing.
"""
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# One process-wide lock: concurrent senders (chat send + reaction notify)
# otherwise interleave keystrokes and merge messages in the pane.
_INJECT_LOCK = threading.Lock()

# Longest line typed with send-keys; longer ones go through a paste
# buffer. tmux's own ceiling is its ~16 KB message size, less the rest
# of the command: 12 KB leaves room for that and for multibyte text.
SEND_KEYS_MAX_BYTES = 12000

# A line longer than this may reach the agent as a paste, and gets the
# sender header (paste_header) typed before it. Claude Code (2.1.281)
# reads one keyboard read of more than 800 characters as a paste; bytes
# count at least as many as its UTF-16 characters, and the margin covers
# a keystroke or two landing in the same read.
PASTE_HEADER_MIN_BYTES = 600

# Seconds between typing the header and typing the body. Two writes the
# agent has not read yet come back as ONE read: the header would ride
# inside the paste it exists to stand outside of. Measured with a slow
# fake reader on a private tmux server: back to back, the header and a
# 1521-character body arrived as one 1570-character read; with a pause
# longer than the reader's stall, as 49 and 1521.
HEADER_SETTLE_S = 0.25


def default_settle(text_length):
    """Seconds to wait between the paste and the Enter. send-keys returns
    once tmux queues the bytes, not once the terminal has consumed them;
    Enter processed before the paste drains lands as a bare newline and
    strands the message in the input box. Scale with length, capped."""
    return min(0.1 + text_length / 4000.0, 0.6)


def _clean_name(name):
    """The sender name as it appears on a line: control characters
    dropped (they would be typed as keystrokes, review I5) and every run
    of whitespace, newlines included, collapsed to one space, so a name
    cannot open a second line."""
    from cousin_lib.server.inbound import strip_controls
    return " ".join(strip_controls(name).split())


def paste_header(name):
    """The one-line header typed before a chat line that may be read as a
    paste: `(Chat <Name>): <Name>'s message follows in full below;
    answer the message, not this line.` The second clause makes it read
    as a label for what follows, not a message of its own to answer.

    Why it exists: Claude Code treats a long burst of typed input (one
    read over 800 characters, or a bracketed paste) as a paste, puts it
    in a pasted-content block, and its own system prompt tells the model
    to follow instructions inside such a block only where the user's own
    message asks it to. A long chat line typed in one burst therefore
    arrived as a bare paste with nothing typed outside it (#111: 41 of
    244 inbound messages in one cousin's transcript), and the operator's
    own instructions could be read as untrusted pasted data. The header
    is typed as ordinary keystrokes first, so the typed part of the turn
    says whose message the block is.

    What it does not change: the line that follows is the whole line the
    sender's message became, byte for byte, with its own `(Chat <Name>): `
    prefix; the header adds a sentence before it and removes nothing.
    It is built from the sender's name alone, never from the message, so
    a message cannot forge one."""
    name = _clean_name(name)
    return ("(Chat %s): %s's message follows in full below; answer the"
            " message, not this line." % (name, name))


def compose_delivery(name, message, *, marker_path, attachments=(),
                     suffix_provider=None, now=None):
    """Build the single delivery line.

    The time prefix reports the gap since the previous inbound message,
    read from the presence marker's mtime, and degrades to the wall clock
    alone when no marker exists yet. The line is single-line by
    construction: an embedded newline would split the tmux paste into a
    premature submit.

    suffix_provider is the reserved recall seam: an optional callable
    whose output is appended to the line. It is best-effort by contract -
    a failing provider never costs the delivery itself.
    """
    now = now or datetime.now(timezone.utc)
    prefix = "[now: %s UTC" % now.strftime("%Y-%m-%d %H:%M")
    try:
        gap_s = now.timestamp() - marker_path.stat().st_mtime
        prefix += " | dt-since-msg: %dm" % int(gap_s // 60)
    except OSError:
        pass
    prefix += "]"
    # control characters would be typed as keystrokes (review I5); the
    # name gets the same, and no newline, so it cannot open a second line
    from cousin_lib.server.inbound import strip_controls
    name = _clean_name(name)
    text = " ".join(strip_controls(message).split())
    text = " ".join(filter(None, [text, *attachments]))
    if suffix_provider is not None:
        try:
            suffix = suffix_provider()
        except Exception:
            suffix = None
        if suffix:
            text += " " + suffix
    return "%s (Chat %s): %s" % (prefix, name, text)


def install_attention_patterns(root=None):
    """config/harness.toml attention_patterns for `root`, or for the
    install this process serves (FrameworkConfig.resolve()) when none
    is given; [] when there is no root, no file, or the file is
    unusable: the guard is a safety net and never costs a delivery on
    its own configuration."""
    from cousin_lib.config import (FrameworkConfig, MissingConfigError,
                                   harness_config)
    try:
        cfg = harness_config(root if root is not None
                             else FrameworkConfig.resolve().root)
    except MissingConfigError:
        return []
    return (cfg or {}).get("attention_patterns") or []


def install_input_mode(root=None):
    """config/harness.toml [input_mode] for `root` (or the install this
    process serves): {"normal_marker": text shown while a modal input box
    is in its command mode, "insert_keys": literal keys that return it to
    typing}. {} when absent or unusable: like the attention guard, a
    safety net that never costs a delivery on its own configuration."""
    from cousin_lib.config import (FrameworkConfig, MissingConfigError,
                                   _read_harness_toml)
    try:
        data = _read_harness_toml(root if root is not None
                                  else FrameworkConfig.resolve().root) or {}
    except (MissingConfigError, OSError, ValueError):
        return {}
    table = data.get("input_mode")
    if not isinstance(table, dict):
        return {}
    marker, keys = table.get("normal_marker"), table.get("insert_keys")
    if not (isinstance(marker, str) and marker and isinstance(keys, str)
            and keys):
        return {}
    return {"normal_marker": marker, "insert_keys": keys}


def pane_attention(pane_text, patterns):
    """The first attention pattern the pane shows, else None."""
    if not pane_text:
        return None
    for pattern in patterns:
        if pattern in pane_text:
            return pattern
    return None


def _normalize_for_verify(s):
    """Lowercase alphanumerics only: drops whitespace, punctuation, and
    the input box's border and wrap glyphs, so a wrapped line still
    matches its source text."""
    return "".join(c for c in s.lower() if c.isalnum())


class TmuxInjector:
    """Types delivery lines into one tmux session.

    The wire sequence is: paste the text literally, wait for the paste to
    drain, send Enter, then verify via capture-pane that the text left
    the input box and retry Enter exactly once. The tmux binary and
    socket come from configuration/PATH.

    A chat line that may be read as a paste (multi-line, or over
    PASTE_HEADER_MIN_BYTES, which includes every line over
    SEND_KEYS_MAX_BYTES) is preceded by paste_header(sender): typed as
    its own send-keys call, with no newline, then a pause (header_settle)
    so the agent reads it as typing before the body arrives. The one
    Enter still comes after the body and submits both. The attention
    gate is read again after the pause. If the body cannot be typed, or
    the pane now shows an attention pattern, the header is erased with
    one BSpace per character (never Escape: a double Escape opens the
    CLI's rewind) and nothing is submitted. A short
    single-line message, and anything delivered without a sender (loops,
    schedules, meetings, reactions), is typed exactly as before.

    Before any of that, when attention patterns are configured, the
    pane is read once: a pane showing one (the agent's login or trust
    menu) is waiting on a person, and typed text there selects menu
    options. The line is then logged and skipped, never typed.
    attention_patterns=None reads them from config/harness.toml at
    each inject, under `root` when given, else under the install the
    environment names; a list pins them.
    """

    def __init__(self, session, *, tmux_bin="tmux", socket=None,
                 settle=default_settle, verify_delay=0.2, log=None,
                 attention_patterns=None, root=None, input_mode=None,
                 header_settle=HEADER_SETTLE_S,
                 paste_header_min_bytes=PASTE_HEADER_MIN_BYTES):
        self.session = session
        self.header_settle = header_settle
        self.paste_header_min_bytes = paste_header_min_bytes
        self.attention_patterns = attention_patterns
        self.input_mode = input_mode
        self.root = root
        self.tmux_bin = tmux_bin
        self.socket = socket
        self.settle = settle
        self.verify_delay = verify_delay
        self.log = log if log is not None else sys.stderr

    def _tmux(self, *args, capture=False, input=None):
        cmd = [self.tmux_bin]
        if self.socket:
            cmd += ["-S", self.socket]
        cmd += list(args)
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=3, check=False, input=input)

    def _paste(self, text):
        """Put the text in the input box. A short line goes as literal
        keys; a long one through a paste buffer, because tmux refuses
        a command over its ~16 KB message size ("command too long")
        and send-keys carries the text as an argument. The buffer is
        named per session and deleted by the paste (-d)."""
        if len(text.encode("utf-8")) <= SEND_KEYS_MAX_BYTES:
            return self._tmux("send-keys", "-t", self.session, "-l", text)
        buffer = "cf-inject-%s" % self.session
        r = self._tmux("load-buffer", "-b", buffer, "-", input=text)
        if r.returncode != 0:
            return r
        return self._tmux("paste-buffer", "-b", buffer, "-d", "-t",
                          self.session)

    def _may_read_as_paste(self, text):
        return ("\n" in text or "\r" in text
                or len(text.encode("utf-8")) > self.paste_header_min_bytes)

    def _skipped(self, blocked, typed="nothing typed"):
        print(
            "[chat-server] tmux delivery SKIPPED target=%r: the pane shows"
            " %r (waiting on a person, not ready); %s"
            % (self.session, blocked, typed),
            file=self.log, flush=True,
        )

    def _erase(self, header):
        """Take a typed header back out of the input box: one BSpace per
        character, in one call, so a failed or skipped body never leaves
        it to prefix the next delivery. Never Escape (a double Escape
        opens the CLI's rewind, which can roll back the conversation and
        files) and never C-u (unmeasured in the CLI's input). A failed
        erase is logged; nothing else is typed."""
        r = self._tmux("send-keys", "-t", self.session,
                       *(["BSpace"] * len(header)))
        if r.returncode != 0:
            print(
                "[chat-server] tmux header erase FAILED (rc=%d) target=%r:"
                " the header may still be in the input box: %s"
                % (r.returncode, self.session, (r.stderr or "")[:200]),
                file=self.log, flush=True,
            )
        return r.returncode == 0

    def _failed(self, r):
        # The only trace that the cousin never received the message
        # (dead or renamed session, tmux down).
        print(
            "[chat-server] tmux delivery FAILED (rc=%d) target=%r: %s"
            % (r.returncode, self.session, (r.stderr or "")[:200]),
            file=self.log, flush=True,
        )

    def _submitted(self, text):
        """Did the Enter actually submit? The probe is the normalized tail
        of the message; if it still shows in the pane's bottom lines, the
        text is sitting in the input box.

        The window is 3 lines: an empty bottom-pinned input box renders
        as roughly border, prompt, border, and the submitted message
        often echoes DIRECTLY above it - a wider window reads that echo
        as "still in the box" and fires the retry exactly when nothing
        was stranded. A stranded paste always reaches the box's bottom
        lines, however long it wrapped, because the probe is the tail of
        the text. Assumes a bottom-pinned input box (an agent CLI); a
        bare shell prompt echoes on the last line and cannot be told
        apart, which costs at most one harmless Enter."""
        probe = _normalize_for_verify(text)[-24:]
        if not probe:
            return True
        r = self._tmux("capture-pane", "-p", "-t", self.session)
        tail = "\n".join((r.stdout or "").splitlines()[-3:])
        return probe not in _normalize_for_verify(tail)

    def _patterns(self):
        if self.attention_patterns is not None:
            return list(self.attention_patterns)
        return install_attention_patterns(self.root)

    def _mode(self):
        if self.input_mode is not None:
            return dict(self.input_mode)
        return install_input_mode(self.root)

    def _capture(self):
        r = self._tmux("capture-pane", "-p", "-t", self.session)
        return (r.stdout or "") if r.returncode == 0 else None

    def _blocked_by(self):
        """The attention pattern the pane shows now, else None. An
        unreadable pane is not evidence of a menu: the paste that
        follows reports a dead session on its own."""
        patterns = self._patterns()
        if not patterns:
            return None
        r = self._tmux("capture-pane", "-p", "-t", self.session)
        if r.returncode != 0:
            return None
        return pane_attention(r.stdout or "", patterns)

    def inject(self, text, *, sender=None):
        """Type one line into the session. Failures are logged loudly and
        never raised: the message is already stored, and the HTTP
        response that triggered this has long since returned. Returns
        True when the line was typed, False when it was skipped or
        failed. `sender` names who a chat line is from: a line that may
        be read as a paste gets paste_header(sender) typed first."""
        with _INJECT_LOCK:
            try:
                blocked = self._blocked_by()
                if blocked is not None:
                    self._skipped(blocked)
                    return False
                mode = self._mode()
                if mode:
                    pane = self._capture()
                    if pane and mode["normal_marker"] in pane:
                        # A modal input box in its command mode reads the
                        # text as commands; put it back in typing mode.
                        self._tmux("send-keys", "-t", self.session, "-l",
                                   mode["insert_keys"])
                header = None
                if sender and self._may_read_as_paste(text):
                    # typed on its own, no newline, trailing space as a
                    # separator: keystrokes, not part of the paste below
                    header = paste_header(sender) + " "
                    r = self._tmux("send-keys", "-t", self.session, "-l",
                                   header)
                    if r.returncode != 0:
                        self._failed(r)
                        return False
                    time.sleep(self.header_settle)
                    blocked = self._blocked_by()
                    if blocked is not None:
                        # a menu came up during the pause: the body
                        # would pick its options
                        self._erase(header)
                        self._skipped(blocked, "header erased, body not"
                                      " typed")
                        return False
                r = self._paste(text)
                if r.returncode != 0:
                    # Nothing was pasted, so there is nothing to submit:
                    # stop here rather than pressing Enter into the void,
                    # and take back a header typed ahead of it.
                    self._failed(r)
                    if header is not None:
                        self._erase(header)
                    return False
                time.sleep(self.settle(len(text)))
                self._tmux("send-keys", "-t", self.session, "Enter")
                time.sleep(self.verify_delay)
                if not self._submitted(text):
                    self._tmux("send-keys", "-t", self.session, "Enter")
                return True
            except (subprocess.TimeoutExpired, FileNotFoundError,
                    OSError) as err:
                print(
                    "[chat-server] tmux delivery FAILED target=%r: %s: %s"
                    % (self.session, type(err).__name__, err),
                    file=self.log, flush=True,
                )
                return False

    def inject_async(self, text, *, sender=None):
        """Fire inject on a background thread so the HTTP response never
        waits on tmux. Returns the thread (tests join it; the server
        does not)."""
        thread = threading.Thread(target=self.inject, args=(text,),
                                  kwargs={"sender": sender}, daemon=True)
        thread.start()
        return thread


def make_deliver(home, injector, *, suffix_provider=None):
    """Bind composition and injector into the server's deliver seam.

    Composition runs synchronously in the request thread - it must read
    the presence marker BEFORE the request handler touches it - and only
    the tmux wiring happens on the background thread.
    """
    marker = home / "data" / ".last-user-msg"

    def deliver(*, user, message, message_id, attachments=()):
        line = compose_delivery(
            user, message, marker_path=marker, attachments=attachments,
            suffix_provider=suffix_provider,
        )
        injector.inject_async(line, sender=user)

    return deliver
