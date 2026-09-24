"""The bridge guard (phase 9 R13): the opencode lane never carries Claude
subscription traffic.

The subscription bridge (an opencode plugin that starts a local
Anthropic-compatible proxy on the operator's Claude login) hooks in
through a plugin entry, a provider baseURL, headers and environment
variables. Removing its packages is necessary, not sufficient: one config
line would bring it back. refuse_bridge() looks at the config the runner
renders for opencode and the environment opencode will start with, and
raises BridgeRefused, naming where, never a value (a value may be a key).

Pure and stdlib-only: no file, no process, no network."""
import ipaddress
import re
from urllib.parse import urlsplit

# The bridge's names, anywhere in the config (a key or a string value) or
# the environment (a name or a value). Case-insensitive.
BRIDGE_MARKERS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"opencode-with-claude",           # the plugin
    r"meridian",                       # the proxy (@rynfar/meridian)
    r"rynfar",                         # its npm scope
    r"claude-max-proxy",               # the proxy's second binary
    r"CLAUDE_PROXY_",                  # its env: CLAUDE_PROXY_PORT, CLAUDE_PROXY_HOST
    r"MERIDIAN_",                      # its env: MERIDIAN_HOST, MERIDIAN_PROFILES, ...
    r"x-meridian",                     # its headers: x-meridian-source, ...
    r"://[^/?#\s]*:3456(?![0-9])",     # a URL on the proxy's default port
))
PORT_MARKER = BRIDGE_MARKERS[-1]
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")


class BridgeRefused(Exception):
    """The config or the environment names the subscription bridge."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def bridge_marker(text):
    """The first marker `text` carries, or None."""
    for marker in BRIDGE_MARKERS:
        if marker.search(text):
            return marker
    return None


def _refuse(marker, where):
    reason = "%s names the Claude-subscription bridge (marker %r); the opencode lane" \
             " never carries subscription traffic" % (where, marker.pattern)
    raise BridgeRefused(reason + move_hint(marker))


def move_hint(marker):
    """What a refusal on the port marker adds: R13's cost, said."""
    if marker is PORT_MARKER:
        return "; port 3456 is the bridge's proxy port: move a legitimate local proxy to" \
               " another port"
    return ""


def _walk(node, path, keys=True):
    """(path, text) for every string in the JSON and, with `keys`, every
    dict key (a header or an env name is a key)."""
    if isinstance(node, dict):
        for key, value in node.items():
            sub = "%s.%s" % (path, key) if path else str(key)
            if keys:
                yield sub, str(key)
            yield from _walk(value, sub, keys)
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            yield from _walk(value, "%s[%d]" % (path, i), keys)
    elif isinstance(node, str):
        yield path, node


def _loopback(url):
    """Does this URL point at this machine? Not a URL is not loopback."""
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower().rstrip(".")
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _is_anthropic(pid, provider):
    npm = provider.get("npm") if isinstance(provider, dict) else None
    return pid.lower() == "anthropic" or (isinstance(npm, str) and "anthropic" in npm.lower())


def _base_urls(node, path):
    """Every string under a key spelled baseURL (any case, `_` ignored)."""
    for sub, value in _walk(node, path, keys=False):
        if sub.rsplit(".", 1)[-1].replace("_", "").lower() == "baseurl":
            yield sub, value


def refuse_bridge(config, env):
    """None when neither the rendered opencode `config` (a dict, as it will
    be written) nor the environment `env` opencode starts with names the
    bridge; BridgeRefused(reason) otherwise. Refused: a marker in any key or
    string of the config (plugins, provider baseURLs, headers, MCP entries,
    anywhere) or in any variable's name or value, and an Anthropic
    provider (by id, or by its `@ai-sdk/anthropic` package) or
    ANTHROPIC_BASE_URL pointed at a loopback address."""
    for where, text in _walk(config or {}, ""):
        marker = bridge_marker(text)
        if marker:
            _refuse(marker, "config %s" % where)
    for name, value in (env or {}).items():
        marker = bridge_marker(str(name)) or bridge_marker(str(value))
        if marker:
            _refuse(marker, "environment variable %s" % name)
    providers = (config or {}).get("provider")
    if isinstance(providers, dict):
        for pid, provider in providers.items():
            if not _is_anthropic(str(pid), provider):
                continue
            for where, url in _base_urls(provider, "provider.%s" % pid):
                if _loopback(url):
                    raise BridgeRefused(
                        "config %s points the anthropic provider at a loopback address, the"
                        " shape of the Claude-subscription bridge; the opencode lane never"
                        " carries subscription traffic" % where)
    base = (env or {}).get("ANTHROPIC_BASE_URL")
    if isinstance(base, str) and _loopback(base):
        raise BridgeRefused(
            "environment variable ANTHROPIC_BASE_URL points at a loopback address, the shape"
            " of the Claude-subscription bridge; the opencode lane never carries subscription"
            " traffic")
    return None
