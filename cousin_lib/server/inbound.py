"""What every send path that stores a chat row runs on the way in:
chat_api.send (the console, cousin-chat) and the Telegram bridge alike.
`divert_login_code` runs FIRST: a message that is a login code is never
stored as written and never delivered.
"""
import re

from cousin_lib.server.storage import is_operator

# C0 controls except tab and newline, DEL, and C1: typed into a terminal
# they are keystrokes (Ctrl-C interrupts, Ctrl-D ends, ESC clears), never
# text a message may carry.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_controls(text):
    """`text` without control characters (tab and newline kept)."""
    return _CONTROLS.sub("", str(text))


def divert_login_code(config, user, message):
    """While `cousin-account login|token --via <this cousin>` waits,
    the operator's next message here is the code: stored under
    <root>/run/ for the flow (taken within one poll), never in chat.db,
    never delivered to the cousin. After the window (a take, a timeout,
    a dead flow), every code-shaped message from that operator is still
    diverted and discarded until the tombstone's hour is over. Returns
    the text to store in chat.db instead, or None.
    Every send path calls this FIRST."""
    import time
    from cousin_lib import accounts
    from cousin_lib.server.storage import normalize_chat_user
    if not is_operator(config, user):
        return None
    root, now = accounts.root_of(config.home), time.time()
    mine = []
    for cap in accounts.captures_for(root, config.slug):
        if normalize_chat_user(user) != normalize_chat_user(cap.get("operator") or ""):
            continue
        name, window = cap.get("account"), accounts.capture_window(cap, now)
        if window is None or (cap.get("state") == "armed"
                              and window not in ("armed", "taken")):
            # its flow died, its clock ran out without the flow's cleanup,
            # or its tombstone's hour is over: a stored code is dropped from
            # disk, the capture becomes a tombstone or goes
            accounts.retire_capture(root, name)
        mine.append((cap, name, window))
    # Only a code-shaped message is ever diverted: the operator's "ok,
    # doing it" while a flow waits is chat, not the code
    if not accounts.CODE_SHAPE.match(str(message).strip()):
        return None
    # An ARMED capture first, whatever its account sorts as: another
    # account's tombstone must never swallow the code a live flow awaits.
    for i, (cap, name, window) in enumerate(mine):
        if window != "armed":
            continue
        if accounts.store_code(root, name, message):
            return "[login code received for account %s]" % name
        # the window moved between the listing and the store (a take or a
        # tombstone won the lock): judge the message by the new one below
        mine[i] = (cap, name, accounts.capture_window(accounts.read_capture(root, name),
                                                      time.time()))
    for cap, name, window in mine:
        if window == "taken":
            return "[a second login code for account %s was discarded]" % name
    for cap, name, window in mine:
        if window == "late" and accounts.discard_late_code(root, name):
            if cap.get("state") == "done":
                return "[a second login code for account %s was discarded]" % name
            return ("[a late login code for account %s was discarded: its window had closed]"
                    % name)
    return None
