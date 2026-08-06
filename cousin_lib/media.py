"""Media generation: a provider seam that is off until configured.

docs/media-spec.md is the contract. The rule that shapes the module:
media generates nothing and reaches no network until a provider is
declared in config/media.toml, and a request goes to the declared
provider or nowhere - never a silent reroute to a vendor the operator
did not choose. The framework names no vendor; a provider fronts a
documented wire contract.
"""
import hashlib
import json
import time
import tomllib
import urllib.request
from pathlib import Path

from cousin_lib.config import CousinConfig, FrameworkConfig

_KIND_EXT = {"image": "png", "voice": "mp3", "video": "mp4"}
_KIND_SUBDIR = {"image": "images", "voice": "audio", "video": "video"}


class MediaError(Exception):
    """A configured provider could not serve; the message names it."""


class NoProviderConfigured(Exception):
    """The kind is unconfigured. Off is a refusal, not a default."""


def load_provider(kind):
    """The provider config for one kind, or None if unconfigured. None
    means off: the caller refuses before any socket work."""
    try:
        root = FrameworkConfig.from_env().root
    except Exception:
        return None
    path = root / "config" / "media.toml"
    try:
        config = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = config.get(kind)
    if not section or not section.get("url"):
        return None
    section = dict(section)
    section["_root"] = root
    return section


def _read_key(provider):
    key_file = provider.get("key_file")
    if not key_file:
        return None
    try:
        return (provider["_root"] / key_file).read_text().strip()
    except OSError:
        return None


def generate(kind, prompt, *, home=None, **params):
    """Generate an asset of `kind` from `prompt` and return its path
    under the cousin's home. Refuses if the kind is unconfigured;
    fails loudly, naming the provider, if it is configured but the
    request does not succeed - it never reroutes."""
    provider = load_provider(kind)
    if provider is None:
        raise NoProviderConfigured(
            "no %s provider configured; declare [%s] in"
            " config/media.toml before generating %s"
            % (kind, kind, kind))
    body = {"model": provider.get("model", ""), "prompt": prompt}
    body.update(params)
    request = urllib.request.Request(
        provider["url"], data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    key = _read_key(provider)
    if key:
        request.add_header("Authorization", "Bearer %s" % key)
    try:
        with urllib.request.urlopen(
                request, timeout=provider.get("timeout_s", 120)) as resp:
            asset = resp.read()
    except Exception as err:
        raise MediaError(
            "%s provider at %s did not serve: %s (no reroute)"
            % (kind, provider["url"], err))
    return _write_asset(kind, asset, home=home)


def _post_reply(*, slug, port, user, message, attachment):
    """Post a generated asset to the cousin's own chat surface via its
    slug-bound reply endpoint - the same path cousin-reply uses. The
    bytes stay on disk; the reply carries the path."""
    body = json.dumps({
        "message": message,
        "reply_to_user": user,
        "attachment": attachment,
    }).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:%d/api/%s_reply" % (port, slug),
        data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10)


def _run_cli(kind, argv):
    """Shared entry point for the three media CLIs: gen writes a file,
    chat generates and posts. Exit codes: 0 ok, 2 unconfigured or
    usage, 3 filtered caption, 4 provider error."""
    import argparse
    import sys

    from cousin_lib.outbound_filter import FilterBlocked, OutboundPolicy

    parser = argparse.ArgumentParser(prog="cousin-%s" % kind)
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("prompt")
    c = sub.add_parser("chat")
    c.add_argument("prompt")
    c.add_argument("--user", required=True)
    c.add_argument("--caption", default="")
    args = parser.parse_args(argv)
    try:
        path = generate(kind, args.prompt)
    except NoProviderConfigured as err:
        print("cousin-%s: %s" % (kind, err), file=sys.stderr)
        return 2
    except MediaError as err:
        print("cousin-%s: %s" % (kind, err), file=sys.stderr)
        return 4
    if args.cmd == "gen":
        print(str(path))
        return 0
    config = CousinConfig.from_env()
    caption = args.caption
    # The caption is outbound: it crosses the same filter every
    # outbound surface does. dest_slug is empty - the recipient is a
    # person, not a cousin - so only the always-active terms apply.
    if caption:
        policy = OutboundPolicy.load(FrameworkConfig.from_env().root)
        try:
            policy.check(caption, from_slug=config.slug, dest_slug="",
                         surface="media", context="media caption")
        except FilterBlocked as err:
            print("cousin-%s: %s" % (kind, err), file=sys.stderr)
            return 3
    _post_reply(slug=config.slug, port=config.require_chat_port(),
                user=args.user, message=caption,
                attachment={"kind": kind, "path": str(path)})
    print("posted %s to %s" % (path.name, args.user))
    return 0


def image_main(argv=None):
    return _run_cli("image", argv)


def voice_main(argv=None):
    return _run_cli("voice", argv)


def video_main(argv=None):
    return _run_cli("video", argv)


def _write_asset(kind, data, *, home=None):
    home = Path(home) if home else CousinConfig.from_env().home
    slug = CousinConfig.from_env().slug
    out_dir = home / "chat" / _KIND_SUBDIR[kind]
    out_dir.mkdir(parents=True, exist_ok=True)
    name = "%s_%d_%s.%s" % (
        slug, int(time.time()),
        hashlib.sha256(data).hexdigest()[:8], _KIND_EXT[kind])
    path = out_dir / name
    path.write_bytes(data)
    return path
