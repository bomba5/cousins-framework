"""Media generation: a provider seam that is off until configured.

docs/media.md is the contract. The rule that shapes the module:
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

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.jobs import track_job

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


def generate_tracked(kind, prompt, *, home=None, **params):
    """generate() recorded as a `media` job in the jobs store: running
    while the provider works, then done with the asset path or failed
    with the error. An unconfigured kind is refused before a row is
    made - a refusal before any work is not a job."""
    if load_provider(kind) is None:
        return generate(kind, prompt, home=home, **params)
    title = "%s: %s" % (kind, " ".join(str(prompt).split())[:80])
    provider = load_provider(kind)
    with track_job("media", title, description=str(prompt)[:500]) as job:
        job.log("request: %s via %s (model %s)%s" % (
            kind, provider.get("url"), provider.get("model") or "-",
            " params %s" % json.dumps(params, sort_keys=True)
            if params else ""))
        path = generate(kind, prompt, home=home, **params)
        job.log("saved %s (%d bytes)" % (path, Path(path).stat().st_size))
        job.summary = str(path)
        # the render as an artifact of its job (#284): a claim can name
        # artifact:<id>, and `why` reaches the job and its prompt
        try:
            from cousin_lib import artifacts
            from cousin_lib.jobs import get_job
            owner = (get_job(job.job_id) or {}).get("spawned_by") or ""
            row = artifacts.add(str(path), created_by=owner, job_id=job.job_id,
                                note="%s render" % kind)
            job.log("artifact #%s" % row["id"])
        except Exception as err:  # noqa: BLE001 - the render stands without its row
            job.log("artifact not recorded: %s" % err)
    return path


def _post_reply(config, *, user, message, attachment):
    """Store a generated asset as a reply on the cousin's own chat
    surface (chat_api.reply, in this process: the same path cousin-reply
    uses). The bytes stay on disk; the reply carries the path."""
    from cousin_lib.server import chat_api
    chat_api.reply(config, {"message": message, "reply_to_user": user,
                            "attachment": attachment})


def _run_cli(kind, argv):
    """Shared entry point for the three media CLIs: gen writes a file,
    chat generates and posts. Exit codes: 0 ok, 2 unconfigured or
    usage, 3 filtered caption, 4 provider error."""
    import argparse
    import sys

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
        # the root a typed command finds (the checkout it runs in, too),
        # exported for load_provider and the job tracking under it
        FrameworkConfig.for_command()
        return _dispatch(kind, args)
    except MissingConfigError as err:
        print("cousin-%s: %s" % (kind, err), file=sys.stderr)
        return 2


def _dispatch(kind, args):
    import sys

    from cousin_lib.outbound_filter import FilterBlocked, OutboundPolicy

    config = caption = None
    if args.cmd == "chat":
        config = CousinConfig.from_env()
        caption = args.caption
        # The caption is outbound: it crosses the same filter every
        # outbound surface does, BEFORE anything is generated, so a
        # blocked caption leaves no file and no job row. dest_slug is
        # empty - the recipient is a person, not a cousin - so only the
        # always-active terms apply.
        if caption:
            policy = OutboundPolicy.load(FrameworkConfig.from_env().root)
            try:
                policy.check(caption, from_slug=config.slug, dest_slug="",
                             surface="media", context="media caption")
            except FilterBlocked as err:
                print("cousin-%s: %s" % (kind, err), file=sys.stderr)
                return 3
    try:
        path = generate_tracked(kind, args.prompt)
    except NoProviderConfigured as err:
        print("cousin-%s: %s" % (kind, err), file=sys.stderr)
        return 2
    except MediaError as err:
        print("cousin-%s: %s" % (kind, err), file=sys.stderr)
        return 4
    if args.cmd == "gen":
        print(str(path))
        return 0
    _post_reply(config, user=args.user, message=caption,
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
