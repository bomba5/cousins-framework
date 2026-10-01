"""The tmux kind's pane hook.

The pane's CLI runs it for UserPromptSubmit, Stop, Notification and
SessionStart (harness_settings.TMUX_HOOK_EVENTS):

    <python> -m cousin_lib.runner.tmux_hook --home <home> <event>

with the hook's JSON on stdin. It does two things:

  - on SessionStart, writes {session_id, transcript_path, source, pid} to
    <home>/run/tmux-session.json, atomically and 0600: the runner
    reads the transcript path from it, and `pid` is the CLI's own;
  - on every event but an `idle_prompt` Notification, sends the runner
    one datagram, {"event", "session_id"} as JSON (SessionStart adds its
    `source`, a Notification its `type`), on its wake socket
    (runner/wake.py). Hooks are wake-ups: the runner decides nothing on
    one, the transcript confirms.

Only the pane's own CLI does either: the launcher
exports its pid as COUSIN_PANE_PID, and the exec chain makes that the
pane's pid and the CLI's. A hook whose CLI is not that pid (an operator's
claude in the home, a claude the pane's model started) writes nothing and
sends nothing.

On a SessionStart whose source is "resume" or "compact" it also prints
the runner's pointer (<home>/data/run/tmux-resume.md) as the
hook's `additionalContext`: `--append-system-prompt` is dropped on a
resume, while SessionStart context does reach a resumed session.

A hook must never block or fail the CLI: it always exits 0, prints
nothing else on stdout (SessionStart's and UserPromptSubmit's stdout
reach the model's context), ignores input it cannot read, and bounds
every step; HARD_S ends the process whatever it is doing.

It runs in the pane's `env -i` environment (tmux_launch), so it imports
only the standard library at import time, and wake.py when it sends
(M-a)."""
import json
import os
import select
import signal
import sys
import tempfile
import time
from pathlib import Path

EVENTS = ("UserPromptSubmit", "Stop", "Notification", "SessionStart")
RECORD = ("run", "tmux-session.json")
MAX_INPUT = 1 << 20        # a hook's JSON is small; more is not ours to read
READ_S = 1.0               # stdin the CLI never closes is given up on after this
HARD_S = 3.0               # the whole hook, whatever step it is in
PANE_PID_VAR = "COUSIN_PANE_PID"   # set by tmux_launch: the pane's pid, which is the CLI's
IGNORED_NOTIFICATIONS = ("idle_prompt",)
POINTER = ("data", "run", "tmux-resume.md")   # written by the runner before a resume
POINTER_SOURCES = ("resume", "compact")
MAX_POINTER = 8192
SHELLS = ("sh", "bash", "dash", "zsh", "ash", "busybox")


def _read_stdin():
    """The hook's input, read from fd 0 for at most READ_S; "" on any error."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return ""
    deadline = time.monotonic() + READ_S
    chunks, size = [], 0
    try:
        while size <= MAX_INPUT:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except (OSError, ValueError):
        return ""
    if size > MAX_INPUT:
        return ""
    return b"".join(chunks).decode("utf-8", "replace")


def _parse(text):
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _ppid_of(pid):
    stat = Path("/proc/%d/stat" % pid).read_text()
    return int(stat.rsplit(")", 1)[1].split()[1])


def cli_pid():
    """The CLI's pid: this hook's parent, or the parent of a shell the CLI
    ran the hook command through (a shell that did not exec it). The pane
    execs its way to the CLI, so this is also the pane's pid."""
    pid = os.getppid()
    for _ in range(2):
        try:
            comm = Path("/proc/%d/comm" % pid).read_text().strip()
            if comm not in SHELLS:
                break
            parent = _ppid_of(pid)
        except (OSError, ValueError, IndexError):
            break
        if parent <= 1:
            break
        pid = parent
    return pid


def from_pane(environ=None):
    """True when this hook's CLI is the pane's own: its pid is the one the
    launcher exported. A CLI started inside the pane inherits the variable
    but has a pid of its own; one started outside has none."""
    value = (os.environ if environ is None else environ).get(PANE_PID_VAR, "")
    try:
        pane = int(value)
    except ValueError:
        return False
    return pane > 1 and cli_pid() == pane


_TEMP = []                 # the record's temp file while it is being written


def _write_record(home, record):
    """The record at <home>/run/tmux-session.json: a temp file in the same
    directory (0600), then os.replace. `run/` is created 0700 when
    missing; a home that does not exist is left alone."""
    run = Path(home).joinpath(*RECORD[:-1])
    try:
        run.mkdir(mode=0o700)
    except FileExistsError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(run), prefix="." + RECORD[-1] + ".", suffix=".tmp")
    _TEMP.append(tmp)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(record))
        os.replace(tmp, str(run / RECORD[-1]))
    except BaseException:
        _remove_temp()
        raise
    finally:
        _TEMP.clear()


def _remove_temp():
    for tmp in _TEMP:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _TEMP.clear()


def session_start(home, data):
    sid, path = data.get("session_id"), data.get("transcript_path")
    if not (isinstance(sid, str) and sid and isinstance(path, str) and path):
        return
    source = data.get("source")
    _write_record(home, {"session_id": sid, "transcript_path": path,
                         "source": source if isinstance(source, str) else None,
                         "pid": cli_pid()})


def pointer(home, data):
    """The runner's resume pointer as SessionStart's hook output, or None."""
    if data.get("source") not in POINTER_SOURCES:
        return None
    try:
        with open(Path(home).joinpath(*POINTER), encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_POINTER + 1)
    except OSError:
        return None
    if not text.strip() or len(text) > MAX_POINTER:
        return None
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": text}})


def wake_runner(home, event, data):
    """One datagram to the runner; False when nobody listens."""
    kind = data.get("notification_type")
    if event == "Notification" and kind in IGNORED_NOTIFICATIONS:
        return False
    sid = data.get("session_id")
    message = {"event": event, "session_id": sid if isinstance(sid, str) else None}
    if event == "Notification" and isinstance(kind, str):
        message["type"] = kind
    source = data.get("source")
    if event == "SessionStart" and isinstance(source, str):
        message["source"] = source      # a "clear" is a session the runner does not follow
    from cousin_lib.runner import wake
    return wake.send(home, json.dumps(message).encode())


def _args(argv):
    """(home, event) from `--home H EVENT`, or None."""
    if len(argv) != 3 or argv[0] != "--home" or not argv[1] or argv[2] not in EVENTS:
        return None
    return argv[1], argv[2]


def main(argv=None, stdin=None):
    """Always 0. `stdin` is the hook's input as text (tests); None reads fd 0."""
    try:
        args = _args(list(sys.argv[1:] if argv is None else argv))
        if args is None:
            return 0
        home, event = args
        if not Path(home).is_dir():
            return 0
        if not from_pane():
            return 0
        data = _parse(_read_stdin() if stdin is None else stdin)
        if data is None:
            return 0
        if event == "SessionStart":
            try:
                session_start(home, data)
            except (OSError, ValueError, TypeError):
                pass            # no record: the runner locates the transcript itself
            out = pointer(home, data)
            if out is not None:
                sys.stdout.write(out)
                sys.stdout.flush()
        wake_runner(home, event, data)
    except Exception:  # noqa: BLE001 - a hook never fails the CLI
        pass
    return 0


def _expired(_sig, _frame):
    """HARD_S is up: leave no temp record behind, and exit 0."""
    _remove_temp()
    os._exit(0)


def _run():
    try:
        signal.signal(signal.SIGALRM, _expired)
        signal.setitimer(signal.ITIMER_REAL, HARD_S)
    except (AttributeError, ValueError, OSError):
        pass
    try:
        main()
    finally:
        os._exit(0)


if __name__ == "__main__":
    _run()
