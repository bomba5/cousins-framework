"""Post a reply to this cousin's own chat surface.

The reply path is cousin -> own chat-server; it never reaches another
cousin's surface. Two contract rules: a process without cousin context is
refused rather than guessed, because a reply on the wrong surface is a
disclosure; and the body travels as JSON so newlines survive shell
quoting.
"""
import argparse
import json
import os
import pathlib
import shutil
import sys
import urllib.error
import urllib.request

from cousin_lib.config import CousinConfig, MissingConfigError
from cousin_lib.trace import traced_cli


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def attach_image(home, message_id, image):
    """Copy `image` to <home>/chat/inbound/<message_id>.<ext>: the name
    the console keys a message's attachment by."""
    message_id = int(message_id)
    inbox = pathlib.Path(home) / "chat" / "inbound"
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / ("%d%s" % (message_id, pathlib.Path(image).suffix.lower()))
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(image, tmp)
    os.replace(tmp, target)
    return target


def send_reply(cfg, body, user=None, reply_to=None):
    body = body.rstrip("\n")
    if not body.strip():
        raise ValueError("empty message body")
    recipient = user or cfg.operator_name
    if not recipient:
        raise MissingConfigError(
            "no --user given and no [operator] name configured; "
            "a reply needs a recipient"
        )
    payload = {"message": body, "reply_to_user": recipient}
    if reply_to is not None:
        payload["reply_to"] = {"id": reply_to}
    url = "http://localhost:%d/api/%s_reply" % (cfg.require_chat_port(), cfg.slug)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read() or b"{}")


@traced_cli("cousin-reply")
def reply_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-reply",
        description="post a reply to this cousin's own chat surface",
    )
    parser.add_argument("--user", help="recipient (default: configured operator)")
    parser.add_argument("--message", "-m", help="body (default: read from stdin)")
    parser.add_argument("--reply-to", type=int, help="message id to quote")
    parser.add_argument("--image", help="a PNG/JPEG/GIF/WebP file to attach;"
                        " it lands as <home>/chat/inbound/<reply id>.<ext>,"
                        " which the console shows on the reply")
    args = parser.parse_args(argv)

    image = None
    if args.image:
        image = pathlib.Path(args.image)
        if image.suffix.lower() not in IMAGE_EXTS:
            print("cousin-reply: --image must be one of %s, got %s"
                  % (", ".join(IMAGE_EXTS), image.name), file=sys.stderr)
            return 2
        if not image.is_file():
            print("cousin-reply: --image %s: no such file" % image,
                  file=sys.stderr)
            return 2
    if args.message is not None:
        body = args.message
    elif image is not None and sys.stdin.isatty():
        body = ""
    else:
        body = sys.stdin.read()
    if image is not None and not body.strip():
        body = "(image: %s)" % image.name
    try:
        cfg = CousinConfig.from_env()
        result = send_reply(cfg, body, user=args.user, reply_to=args.reply_to)
    except (MissingConfigError, ValueError) as e:
        print("cousin-reply: %s" % e, file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print("cousin-reply: %s" % e, file=sys.stderr)
        return 1
    if not result.get("ok"):
        print("cousin-reply: server returned %s" % result, file=sys.stderr)
        return 1
    if image is not None:
        try:
            attach_image(cfg.home, result.get("id"), image)
        except (OSError, ValueError) as e:
            print("cousin-reply: reply %s posted but the image did not land: %s"
                  % (result.get("id"), e), file=sys.stderr)
            return 1
    print("reply posted (id=%s)" % result.get("id"))
    return 0
