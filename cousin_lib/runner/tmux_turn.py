"""run/turn.json: the tmux kind's live turn, as the stdio `cousin-mcp` sees
it. The runner writes it when a turn starts in the pane
and clears it at the turn's end; the stdio server reads it to route `reply`
(the live turn's threads) and `handoff`. It counts only for the pane's own
CLI session (run/tmux-session.json, written by the SessionStart hook): an
absent, unreadable, stale or foreign file means no live turn.

{"session_id": str, "turn_nonce": str, "threads": [str]}, atomic (tmp +
rename), 0600."""
import json
import os
from pathlib import Path

TURN = ("run", "turn.json")
SESSION = ("run", "tmux-session.json")


def write(home, *, session_id, turn_nonce, threads):
    """The live turn: which pane session, which runner nonce, which threads."""
    path = Path(home).joinpath(*TURN)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"session_id": str(session_id), "turn_nonce": str(turn_nonce),
                   "threads": [str(t) for t in threads]}, f)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def clear(home):
    """No turn is live (idempotent)."""
    Path(home).joinpath(*TURN).unlink(missing_ok=True)


def _read(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def live(home):
    """(active, threads): the turn file's threads when it names the pane's
    current session; (False, ()) otherwise."""
    turn = _read(Path(home).joinpath(*TURN))
    pane = _read(Path(home).joinpath(*SESSION))
    if not turn or not pane or not turn.get("session_id") \
            or turn.get("session_id") != pane.get("session_id"):
        return False, ()
    threads = turn.get("threads")
    if not isinstance(threads, list) or not all(isinstance(t, str) for t in threads):
        return False, ()
    return True, tuple(threads)
