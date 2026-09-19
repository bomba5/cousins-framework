"""Post a reply to this cousin's own chat surface.

The reply path is cousin -> own chat-server; it never reaches another
cousin's surface. Two contract rules: a process without cousin context is
refused rather than guessed, because a reply on the wrong surface is a
disclosure; and the body travels as JSON so newlines survive shell
quoting.

The body crosses the outbound filter before anything touches the wire,
exactly as cousin-chat and the media captions do: a blocked reply exits
3 and nothing is posted (no message, no attachment).
"""
import argparse
import json
import os
import pathlib
import shutil
import sys
import urllib.error
import urllib.request
import uuid

from cousin_lib.config import CousinConfig, MissingConfigError
from cousin_lib.outbound_filter import FilterBlocked, OutboundPolicy
from cousin_lib.trace import traced_cli


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v")
# kind -> (accepted suffixes, the media folder under <home>/chat/ that
# cousin-image and friends use, docs/media.md "Storage").
ATTACHMENT_KINDS = {"image": (IMAGE_EXTS, "images"),
                    "video": (VIDEO_EXTS, "video")}


def stage_attachment(home, kind, source):
    """Copy `source` into <home>/chat/<folder>/ under a fresh name and
    return the copy's path. The reply row then names it in
    attachment_kind / attachment_path, the one convention the console
    and the Telegram bridge both read (as cousin-image rows do)."""
    source = pathlib.Path(source)
    folder = pathlib.Path(home) / "chat" / ATTACHMENT_KINDS[kind][1]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("reply_%s%s" % (uuid.uuid4().hex[:12],
                                       source.suffix.lower()))
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)
    return target


def framework_root_for(home):
    """FRAMEWORK_ROOT, else the home's grandparent (homes live at
    <root>/cousins/<slug>): a shell that exported only COUSIN_HOME must
    not reply unfiltered."""
    env = os.environ.get("FRAMEWORK_ROOT")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(os.path.abspath(home)).parent.parent


def send_reply(cfg, body, user=None, reply_to=None, policy=None,
               attachment=None):
    """Post one reply. `attachment` is (kind, source path): the file is
    staged into the home's media folder only after the filter passed
    and the recipient is known, and removed again when the server
    refused the reply. A timeout keeps it: the row may have landed."""
    body = body.rstrip("\n")
    if not body.strip():
        raise ValueError("empty message body")
    if policy is not None:
        # The recipient is a person on the operator surface, not a
        # cousin: dest_slug is empty, as for a media caption.
        policy.check(body, from_slug=cfg.slug, dest_slug="",
                     surface="chat", context="reply")
    recipient = user or cfg.operator_name
    if not recipient:
        raise MissingConfigError(
            "no --user given and no [operator] name configured; "
            "a reply needs a recipient"
        )
    payload = {"message": body, "reply_to_user": recipient}
    if reply_to is not None:
        payload["reply_to"] = {"id": reply_to}
    if attachment is not None:
        kind, source = attachment
        staged = stage_attachment(cfg.home, kind, source)
        payload["attachment"] = {"kind": kind, "path": str(staged)}
        try:
            result = _post(cfg, payload)
        except urllib.error.HTTPError:
            staged.unlink(missing_ok=True)
            raise
        if not result.get("ok"):
            staged.unlink(missing_ok=True)
        return result
    return _post(cfg, payload)


def _post(cfg, payload):
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
    media = parser.add_mutually_exclusive_group()
    media.add_argument("--image", help="a PNG/JPEG/GIF/WebP file to attach;"
                       " it is copied to <home>/chat/images/ and shown on"
                       " the reply (and relayed by the Telegram bridge)")
    media.add_argument("--video", help="an MP4/WebM/MOV/M4V file to attach;"
                       " it is copied to <home>/chat/video/ and shown on"
                       " the reply (and relayed by the Telegram bridge)")
    args = parser.parse_args(argv)

    attachment = None
    for kind, value in (("image", args.image), ("video", args.video)):
        if not value:
            continue
        path = pathlib.Path(value)
        exts = ATTACHMENT_KINDS[kind][0]
        if path.suffix.lower() not in exts:
            print("cousin-reply: --%s must be one of %s, got %s"
                  % (kind, ", ".join(exts), path.name), file=sys.stderr)
            return 2
        if not path.is_file():
            print("cousin-reply: --%s %s: no such file" % (kind, path),
                  file=sys.stderr)
            return 2
        attachment = (kind, path)
    if args.message is not None:
        body = args.message
    elif attachment is not None and sys.stdin.isatty():
        body = ""
    else:
        body = sys.stdin.read()
    if attachment is not None and not body.strip():
        body = "(%s: %s)" % (attachment[0], attachment[1].name)
    try:
        cfg = CousinConfig.from_env()
        policy = OutboundPolicy.load(framework_root_for(cfg.home))
        result = send_reply(cfg, body, user=args.user,
                            reply_to=args.reply_to, policy=policy,
                            attachment=attachment)
    except FilterBlocked as e:
        print("cousin-reply: %s" % e, file=sys.stderr)
        return 3
    except (MissingConfigError, ValueError) as e:
        print("cousin-reply: %s" % e, file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print("cousin-reply: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("cousin-reply: the attachment could not be staged: %s" % e,
              file=sys.stderr)
        return 1
    if not result.get("ok"):
        print("cousin-reply: server returned %s" % result, file=sys.stderr)
        return 1
    print("reply posted (id=%s)" % result.get("id"))
    return 0
