"""Follow-up to storing one inbound chat message, shared by every send
path that stores a chat row: the chat server's own `/api/send` and the
Telegram bridge alike. Two things happen, both best-effort - the message
is already stored, and neither action may turn into a failed send:

- the presence marker (`<home>/data/.last-user-msg`) is touched, the gap
  baseline the tmux delivery line's time prefix reads for the NEXT
  message; and
- when the sender is the configured operator, their message is checked
  for a correction and recorded on a hit.

Before any of that, `divert_login_code` runs FIRST on every send path: a
message that is a login code (R18) is never stored as written and never
delivered.
"""
import sys
from pathlib import Path

from cousin_lib import corrections
from cousin_lib.server.storage import is_operator


def after_inbound_stored(config, user, message):
    """Touch the presence marker and, for the operator only, capture a
    correction. Call this after the message has been delivered: the
    marker's mtime must still carry the PREVIOUS message's timing when
    delivery composes this one's line."""
    marker = Path(config.home) / "data" / ".last-user-msg"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    if not is_operator(config, user):
        return
    try:
        corrections.detect_and_record(config.home, user=user, text=message)
    except Exception as err:  # noqa: BLE001 - never fails the send
        print("corrections: not recorded: %s" % err, file=sys.stderr)


def divert_login_code(config, user, message):
    """R18: while `cousin-account login|token --via <this cousin>` waits,
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
    if not accounts.CODE_SHAPE.match(str(message).strip()):
        return None
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
