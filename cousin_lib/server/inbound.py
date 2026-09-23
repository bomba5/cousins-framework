"""Follow-up to storing one inbound chat message, shared by every send
path that stores a chat row: the chat server's own `/api/send` and the
Telegram bridge alike. Two things happen, both best-effort - the message
is already stored, and neither action may turn into a failed send:

- the presence marker (`<home>/data/.last-user-msg`) is touched, the gap
  baseline the tmux delivery line's time prefix reads for the NEXT
  message; and
- when the sender is the configured operator, their message is checked
  for a correction and recorded on a hit.
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
