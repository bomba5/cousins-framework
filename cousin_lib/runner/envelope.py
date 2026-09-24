"""The envelope: an inbox item as the model reads it.

Header, then the body verbatim, then any context in a block that says
it is not the sender's words. The header names the thread so the
model knows whom it answers (spec, "Threads"); with side sessions off
every thread shares one session, so the name is the only routing cue.
"""
import base64
from datetime import datetime, timezone
from pathlib import Path

_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}
CONTEXT_MARK = "--- context (not the sender's words) ---"
# The API refuses an image over 5 MB, and base64 grows a file by 4/3. A
# larger file (a phone photo) is handed over by path for the model's Read
# tool, which downsizes it; the core has no image library to do it here.
INLINE_IMAGE_MAX_BYTES = 3_700_000


def _image_type(path):
    """Return the media type if path has an image suffix and is a file, else None."""
    path = Path(str(path))
    media = _IMAGE_TYPES.get(path.suffix.lower())
    return media if media and path.is_file() else None


def _too_big(path):
    try:
        return Path(str(path)).stat().st_size > INLINE_IMAGE_MAX_BYTES
    except OSError:
        return False


def _read_marker(path):
    return "[image attached, too large to inline -> Read %s]" % path


def _header(item, now):
    now = now or datetime.now(timezone.utc)
    return "[%s] %s from %s at %s" % (
        item.thread_id, item.source, item.sender or "unknown",
        now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))


def _text(item, now):
    parts = [_header(item, now), "", item.body]
    if item.context:
        parts += ["", CONTEXT_MARK, item.context]
    return "\n".join(parts)


def _attachment_blocks(item):
    blocks = []
    for raw in item.attachments:
        path = Path(str(raw))
        media = _image_type(path)
        if media and _too_big(path):
            blocks.append({"type": "text", "text": _read_marker(path)})
        elif media:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": media,
                                      "data": data}})
        else:
            blocks.append({"type": "text", "text": "[attachment: %s]" % path.name})
    return blocks


def render(item, *, now=None):
    text = _text(item, now)
    names = [_read_marker(a) if _image_type(a) and _too_big(a)
             else "[image: %s]" % Path(str(a)).name if _image_type(a)
             else "[attachment: %s]" % Path(str(a)).name for a in item.attachments]
    return text + ("\n" + "\n".join(names) if names else "")


def render_message(item, *, now=None):
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": _text(item, now)},
                                    *_attachment_blocks(item)]}}
