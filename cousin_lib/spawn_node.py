"""cousin-spawn-node: build the copy-over archive for a hive node.

A node is a cousin on another machine that reaches the queen outbound
only (docs/hive-spec.md). Deployment is therefore copy-over, not push:
this builder mints the node's token through the queen's own store,
renders its identity from templates/hive-node/, and writes one
self-contained tarball the operator carries to the other machine and
installs there. The framework never dials the node and never installs
itself anywhere; the operator moves the archive, and with it the token.

The archive is a secret (the token is inside). It is built under the
output directory the caller names and never in the tree.
"""
import argparse
import io
import os
import re
import shlex
import sys
import tarfile
import time

from cousin_lib.config import FrameworkConfig, MissingConfigError
from cousin_lib.hive import HiveStore
from cousin_lib.template import TemplateError, render_template
from cousin_lib.trace import traced_cli

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_TEMPLATE_FILES = ("cousin_node.py", "install.sh", "CLAUDE.md")
_DEFAULT_PORT = 8210


class SpawnNodeError(Exception):
    """The archive cannot be built; the message says why."""


def render_node_env(*, slug, name, port, queen_url, token, home_chat,
                    agent_cmd, poll_seconds=5, agent_timeout=120):
    """The node's environment file: every key the runtime reads, shell
    quoted so install.sh can source it whatever the values hold. Keys
    that are optional are present and empty, so the operator edits a
    line rather than guessing a name."""
    rows = [
        ("COUSIN_SLUG", slug),
        ("NODE_NAME", name),
        ("NODE_PORT", str(port)),
        ("NODE_HOST", "127.0.0.1"),
        ("QUEEN_URL", queen_url),
        ("HIVE_TOKEN", token),
        ("HOME_CHAT_URL", home_chat or ""),
        ("AGENT_CMD", agent_cmd or ""),
        ("NODE_POLL_SECONDS", str(poll_seconds)),
        ("AGENT_TIMEOUT_SECONDS", str(agent_timeout)),
    ]
    lines = [
        "# node.env - written by cousin-spawn-node; carries the bearer",
        "# token, keep it owner-only. install.sh sources it and the unit",
        "# reads it as EnvironmentFile. Fill AGENT_CMD on the node to",
        "# replace the placeholder brain.",
    ]
    for key, value in rows:
        lines.append("%s=%s" % (key, _shell_value(value)))
    return "\n".join(lines) + "\n"


def _shell_value(value):
    """Quote only when the shell would otherwise split or expand it, so
    the common case stays readable and the test-style KEY=VALUE parse
    holds for it."""
    if value == "" or re.match(r"^[A-Za-z0-9_./:@%+=,-]+$", value):
        return value
    return shlex.quote(value)


def _render_readme(*, slug, name, queen_url, port):
    return (
        "%s (%s): a hive node built by cousin-spawn-node\n"
        "\n"
        "This directory is the whole node. Copy it to the machine that\n"
        "will run it and, from inside it, run:\n"
        "\n"
        "    ./install.sh\n"
        "\n"
        "It needs python3 and outbound network to the queen at\n"
        "%s, nothing else. The node then answers on\n"
        "http://127.0.0.1:%d/health and polls its own inbox on the queen.\n"
        "\n"
        "Files:\n"
        "  cousin_node.py  the runtime: chat server, brain loop, poller\n"
        "  install.sh      installs a systemd unit (--print-unit to see it,\n"
        "                  --foreground to run without systemd)\n"
        "  CLAUDE.md       the node's identity; edit it here to shape it\n"
        "  node.env        configuration incl. the bearer token: SECRET\n"
        "\n"
        "The brain is a placeholder until you set AGENT_CMD in node.env\n"
        "to the command that runs your agent (prompt on stdin, reply on\n"
        "stdout). See docs/deploying-a-node.md in the framework.\n"
        % (name, slug, queen_url, port))


def build_node_archive(root, *, slug, queen_url, name, role, out,
                       token=None, home_chat=None, port=_DEFAULT_PORT,
                       agent_cmd=""):
    """Mint (or take) the token, render everything in memory, and only
    then write the tarball: a failed render leaves no archive behind.
    Returns {tarball, token, slug}."""
    root = FrameworkConfig(root).root
    if not slug or not _SLUG_RE.match(slug):
        raise SpawnNodeError(
            "invalid slug %r: use ^[a-z][a-z0-9_-]{1,31}$" % (slug,))
    if not queen_url:
        raise SpawnNodeError("a queen URL is required")
    template_dir = root / "templates" / "hive-node"
    sources = {}
    for filename in _TEMPLATE_FILES:
        try:
            sources[filename] = (template_dir / filename).read_text()
        except OSError as err:
            raise SpawnNodeError(
                "cannot read the node template %s: %s"
                % (template_dir / filename, err))
    try:
        claude_md = render_template(sources["CLAUDE.md"], {
            "NAME": name, "SLUG": slug, "ROLE_ONE_LINE": role,
            "PORT": port,
        })
    except TemplateError as err:
        raise SpawnNodeError(str(err))
    if token is None:
        # The queen's own store: the same row cousin-hive mint writes,
        # idempotent per slug so a rebuild never orphans a deployed node.
        token = HiveStore(root / "shared" / "hive").mint_token(
            slug, scope=("own", "shared"))
    files = [
        ("cousin_node.py", sources["cousin_node.py"], 0o644),
        ("install.sh", sources["install.sh"], 0o755),
        ("CLAUDE.md", claude_md, 0o644),
        ("node.env", render_node_env(
            slug=slug, name=name, port=port, queen_url=queen_url,
            token=token, home_chat=home_chat, agent_cmd=agent_cmd), 0o600),
        ("README", _render_readme(slug=slug, name=name,
                                  queen_url=queen_url, port=port), 0o644),
    ]
    out = FrameworkConfig(out).root
    out.mkdir(parents=True, exist_ok=True)
    top = "%s-node" % slug
    tarball = out / ("%s.tar.gz" % top)
    tmp = out / ("%s.tar.gz.tmp" % top)
    now = int(time.time())
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            directory = tarfile.TarInfo(top)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            directory.mtime = now
            tar.addfile(directory)
            for filename, text, mode in files:
                data = text.encode()
                info = tarfile.TarInfo("%s/%s" % (top, filename))
                info.size = len(data)
                info.mode = mode
                info.mtime = now
                tar.addfile(info, io.BytesIO(data))
        os.replace(tmp, tarball)
    except OSError as err:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SpawnNodeError("cannot write %s: %s" % (tarball, err))
    return {"tarball": tarball, "token": token, "slug": slug}


@traced_cli("cousin-spawn-node")
def spawn_node_main(argv=None):
    """Console entry point. Exit codes: 0 built, 2 validation or
    configuration error (nothing written). The token is never printed:
    it is in the archive, and the archive is the secret."""
    parser = argparse.ArgumentParser(
        prog="cousin-spawn-node",
        description="build the copy-over archive for a hive node")
    parser.add_argument("slug")
    parser.add_argument(
        "--root",
        help="the framework root: a directory containing templates/"
             " (typically the checkout itself). Falls back to"
             " FRAMEWORK_ROOT.")
    parser.add_argument("--queen-url", required=True,
                        help="the queen as the NODE will reach it")
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--token",
                        help="a token already minted on a remote queen;"
                             " without it one is minted in this root's"
                             " queen store")
    parser.add_argument("--home-chat",
                        help="a chat server the node's [tell-home: ...]"
                             " marker posts to; off when absent")
    parser.add_argument("--agent-cmd", default="",
                        help="the backend command line to bake into"
                             " node.env; empty means the placeholder brain")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT,
                        help="the node's own chat port (default %d)"
                             % _DEFAULT_PORT)
    parser.add_argument("--out", default=".",
                        help="where <slug>-node.tar.gz is written")
    args = parser.parse_args(argv)
    try:
        root = FrameworkConfig.resolve(args.root).root
    except MissingConfigError as err:
        print("cousin-spawn-node: %s" % err, file=sys.stderr)
        return 2
    try:
        result = build_node_archive(
            root, slug=args.slug, queen_url=args.queen_url,
            name=args.name, role=args.role, out=args.out,
            token=args.token, home_chat=args.home_chat, port=args.port,
            agent_cmd=args.agent_cmd)
    except SpawnNodeError as err:
        print("cousin-spawn-node: %s" % err, file=sys.stderr)
        return 2
    top = "%s-node" % result["slug"]
    print("built %s" % result["tarball"])
    print("it contains the node's bearer token: move it privately.")
    print("next, on the node machine:")
    print("  tar xzf %s.tar.gz && cd %s && ./install.sh" % (top, top))
    return 0
