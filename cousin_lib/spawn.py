"""cousin-spawn: how a cousin comes to exist.

The creation sequence and its cleanup contract are specified in
docs/spawn-and-template-spec.md. The rule that shapes the code: a
failed create removes everything it made, a completed create is a real
cousin whatever happens afterwards.
"""
import argparse
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib

from cousin_lib.config import CousinConfig, FrameworkConfig
from cousin_lib.template import TemplateError, render_template

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class SpawnError(Exception):
    """Creation cannot proceed; the message says why."""


def _claimed_ports(root):
    """Every port in any cousin.toml under the root, whether or not it
    falls inside today's scan range - a hand-configured port outside
    the range must never become allocatable by a wider range later."""
    claimed = set()
    for cfg in FrameworkConfig(root).list_cousins():
        if cfg.chat_port:
            claimed.add(cfg.chat_port)
    return claimed


def _is_live(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def allocate_port(root, *, start=8090, end=8200, is_live=_is_live):
    """First port in [start, end] that is neither claimed nor live.
    Exhaustion is an error: a cousin without a working chat port is a
    spawn failure, not a degraded success."""
    claimed = _claimed_ports(root)
    for port in range(start, end + 1):
        if port in claimed or is_live(port):
            continue
        return port
    raise SpawnError(
        "no free chat port in %d-%d; widen the range or free a port"
        % (start, end)
    )


def _toml_quote(value):
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _write_cousin_toml(home, *, slug, name, role, port):
    """Write via a temporary file, re-parse, then rename into place: a
    config that cannot be read back is never persisted."""
    text = (
        "[cousin]\n"
        "slug = %s\n"
        "name = %s\n"
        "role = %s\n\n"
        "[chat]\n"
        "port = %d\n"
        "tmux_session = %s\n"
        % (_toml_quote(slug), _toml_quote(name), _toml_quote(role),
           port, _toml_quote(slug))
    )
    tomllib.loads(text)
    fd, tmp = tempfile.mkstemp(dir=home, suffix=".toml.tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, home / "cousin.toml")


def _write_identity_files(home, *, claude_md, name, role):
    (home / "CLAUDE.md").write_text(claude_md)
    (home / "STATUS.md").write_text("# %s - STATUS\n" % name)
    (home / "MEMORY.md").write_text("# %s - memory index\n" % name)


def create_cousin(root, *, slug, role, name=None, role_paragraph=None,
                  voice=None, port=None, template_path=None,
                  _is_live=_is_live):
    """The creation sequence from the spec: validate, allocate, create,
    write atomically, render - and on any failure after the home exists,
    remove everything this run created. Returns {slug, home, port}."""
    root = FrameworkConfig(root).root
    if not slug or not _SLUG_RE.match(slug):
        raise SpawnError(
            "invalid slug %r: use ^[a-z][a-z0-9_-]{1,31}$" % (slug,)
        )
    home = root / "cousins" / slug
    if (home / "cousin.toml").is_file():
        raise SpawnError("cousin %r already exists" % slug)
    if home.exists():
        raise SpawnError(
            "orphan directory squats the slug %r: %s has no cousin.toml; "
            "remove or complete it" % (slug, home)
        )
    template = template_path or (
        root / "templates" / "cousin-CLAUDE.template.md"
    )
    try:
        template_text = template.read_text()
    except OSError as err:
        raise SpawnError("cannot read template %s: %s" % (template, err))
    name = name or slug.capitalize()
    if port is None:
        port = allocate_port(root, is_live=_is_live)
    # Render BEFORE anything is written: an incomplete identity must
    # fail while the filesystem is still untouched.
    values = {
        "NAME": name,
        "SLUG": slug,
        "PORT": port,
        "ROLE_ONE_LINE": role,
        "ROLE_PARAGRAPH": role_paragraph or role,
    }
    if voice is not None:
        values["VOICE_GUIDE"] = voice
    try:
        claude_md = render_template(template_text, values)
    except TemplateError as err:
        raise SpawnError(str(err))
    try:
        for sub in ("memory", "data", "notes", "scripts"):
            (home / sub).mkdir(parents=True)
        _write_cousin_toml(home, slug=slug, name=name, role=role,
                           port=port)
        _write_identity_files(home, claude_md=claude_md, name=name,
                              role=role)
    except Exception as err:
        # The partial state is the one that squats a slug; a failed
        # create leaves nothing.
        shutil.rmtree(home, ignore_errors=True)
        raise SpawnError("create failed, home removed: %s" % err)
    return {"slug": slug, "home": home, "port": port}


def _default_chat_server(home):
    """Launch the cousin's chat server detached, logging to its data
    dir. The daemon owns its own lifetime; spawn only starts it."""
    log = open(home / "data" / "chat-server.log", "ab")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "cousin_lib.server.app",
             "--home", str(home)],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log.close()


def start_cousin(home, *, agent_cmd, tmux_bin="tmux", tmux_socket=None,
                 start_chat_server=_default_chat_server):
    """THE tmux-session-creation site - the only one in this codebase,
    by spec. Any future respawn machinery calls this function.

    agent_cmd is host configuration: what it means to 'run an agent'
    (binary, flags, trust model) differs per install and is never
    hardcoded here."""
    config = CousinConfig.load(home)
    cmd = [tmux_bin]
    if tmux_socket:
        cmd += ["-S", tmux_socket]
    cmd += [
        "new-session", "-d", "-s", config.tmux_session,
        "-c", str(home),
        "-e", "COUSIN_HOME=%s" % home,
        "/usr/bin/env", "COUSIN_HOME=%s" % home,
    ] + shlex.split(agent_cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                       check=False)
    if r.returncode != 0:
        raise SpawnError(
            "tmux new-session failed (rc=%d): %s"
            % (r.returncode, (r.stderr or "").strip()[:200])
        )
    start_chat_server(home)


def _read_agent_cmd(root):
    """The agent command comes from <root>/config/agent-cmd - one line,
    host configuration. Starting a cousin without it is an error with a
    remediation, not a guessed default binary."""
    path = root / "config" / "agent-cmd"
    try:
        cmd = path.read_text().strip()
    except OSError:
        cmd = ""
    if not cmd:
        raise SpawnError(
            "no agent command configured: write the command line that "
            "runs your agent into %s" % path
        )
    return cmd


def spawn_main(argv=None):
    """Console entry point. Exit codes are the interface: 0 created
    (and started, if asked), 1 create succeeded but --start failed
    (home kept, restartable), 2 validation or configuration error
    (nothing written)."""
    parser = argparse.ArgumentParser(prog="cousin-spawn")
    parser.add_argument("slug")
    parser.add_argument("--root", required=True)
    parser.add_argument("--name")
    parser.add_argument("--role", required=True)
    parser.add_argument("--role-paragraph")
    parser.add_argument("--voice", required=True,
                        help="the authored voice guide; a cousin is "
                             "never shipped without one")
    parser.add_argument("--port", type=int)
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args(argv)
    root = FrameworkConfig(args.root).root
    try:
        out = create_cousin(
            root, slug=args.slug, role=args.role, name=args.name,
            role_paragraph=args.role_paragraph, voice=args.voice,
            port=args.port,
        )
    except SpawnError as err:
        print("cousin-spawn: %s" % err, file=sys.stderr)
        return 2
    print("created %s at %s (chat port %d)"
          % (out["slug"], out["home"], out["port"]))
    if args.start:
        try:
            start_cousin(out["home"], agent_cmd=_read_agent_cmd(root))
        except SpawnError as err:
            print("cousin-spawn: created but start failed: %s\n"
                  "the home is kept; fix the cause and start it again"
                  % err, file=sys.stderr)
            return 1
        print("started %s" % out["slug"])
    return 0
