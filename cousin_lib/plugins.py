"""Plugins: optional, install-level extensions the framework knows how to
run but never ships (docs/plugins.md).

A plugin is a directory holding `plugin.toml`, its manifest. The install
declares the plugins it has in `config/plugins.toml` (absent: none):

    [plugins.clock]
    path = "/abs/dir"          # or relative to the framework root
    enabled = true             # optional; false hides it everywhere

and a cousin turns one on in its cousin.toml:

    [plugins]
    enabled = ["clock"]

The manifest may declare three parts, each optional:

- [mcp]: a stdio MCP server the runner adds, named after the plugin, for
  a cousin that enables it (command, args, env);
- [service]: one long-running process for the install, run by the
  supervisor as `plugin:<name>` while at least one cousin enables it
  (command, args, env, port, health);
- [console]: a page on that service, shown as a tab on the cousin's view
  (title, page); it needs [service].

Values may carry placeholders the framework renders: `{plugin_dir}`,
`{root}`, `{service_url}` (`http://127.0.0.1:<port>`), and in [mcp] and
[console] also `{slug}` and `{home}`. Any other `{name}` is refused.

Parsing is strict: an unknown key, a bad value or an unknown placeholder
names the problem and skips that plugin; one bad plugin never stops the
install or a cousin. The framework never reads a plugin's own per-cousin
settings. Stdlib only."""
import re
import tomllib
import urllib.parse
from pathlib import Path

CONFIG = Path("config") / "plugins.toml"
MANIFEST = "plugin.toml"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
HOST = "127.0.0.1"
PROXY_PREFIX = "/plugins/"
# Names a plugin cannot take: the runner's own MCP server.
RESERVED = frozenset({"cousin"})

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TOP_KEYS = {"name", "description", "version", "mcp", "service", "console"}
_ENTRY_KEYS = {"path", "enabled"}
_PART_KEYS = {"mcp": {"command", "args", "env"},
              "service": {"command", "args", "env", "port", "health"},
              "console": {"title", "page"}}
_INSTALL_VARS = ("plugin_dir", "root", "service_url")
_COUSIN_VARS = _INSTALL_VARS + ("slug", "home")
_PART_VARS = {"mcp": _COUSIN_VARS, "service": _INSTALL_VARS, "console": _COUSIN_VARS}


class PluginError(ValueError):
    """A manifest or an entry that cannot be used; the message says why."""


class Plugin:
    """One valid, enabled plugin, as its manifest declares it (nothing
    rendered): `name`, `description`, `version`, `dir`, and the three
    optional parts as dicts (`mcp`, `service`, `console`) or None."""

    def __init__(self, name, directory, manifest):
        self.name = name
        self.dir = Path(directory)
        self.description = manifest.get("description", "")
        self.version = manifest.get("version", "")
        self.mcp = manifest.get("mcp")
        self.service = manifest.get("service")
        self.console = manifest.get("console")

    def __repr__(self):
        return "Plugin(%r)" % self.name

    @property
    def port(self):
        return self.service["port"] if self.service else None

    @property
    def service_url(self):
        return "http://%s:%d" % (HOST, self.port) if self.service else None

    def values(self, root, *, slug=None, home=None):
        """The placeholder values for this plugin."""
        out = {"plugin_dir": str(self.dir), "root": str(root),
               "service_url": self.service_url or ""}
        if slug is not None:
            out["slug"] = str(slug)
        if home is not None:
            out["home"] = str(home)
        return out

    def mcp_server(self, root, *, slug, home):
        """{command, args, env} of the [mcp] server for one cousin, rendered,
        env plus COUSIN_SLUG, COUSIN_HOME, FRAMEWORK_ROOT and PLUGIN_DIR;
        None without [mcp]."""
        if not self.mcp:
            return None
        values = self.values(root, slug=slug, home=home)
        env = {k: render(v, values) for k, v in (self.mcp.get("env") or {}).items()}
        env.update(COUSIN_SLUG=str(slug), COUSIN_HOME=str(home), FRAMEWORK_ROOT=str(root),
                   PLUGIN_DIR=str(self.dir))
        return {"command": render(self.mcp["command"], values),
                "args": [render(a, values) for a in self.mcp.get("args") or []],
                "env": env}

    def service_command(self, root):
        """(argv, env) of the [service] process, rendered, env plus
        PLUGIN_PORT, PLUGIN_DIR and FRAMEWORK_ROOT; None without [service]."""
        if not self.service:
            return None
        values = self.values(root)
        argv = [render(self.service["command"], values)] + [
            render(a, values) for a in self.service.get("args") or []]
        env = {k: render(v, values) for k, v in (self.service.get("env") or {}).items()}
        env.update(PLUGIN_PORT=str(self.port), PLUGIN_DIR=str(self.dir),
                   FRAMEWORK_ROOT=str(root))
        return argv, env

    def console_tab(self, root, *, slug, home):
        """{name, title, url} of the cousin's tab: the rendered page under
        the console's proxy (/plugins/<name><page>); None without [console].
        A rendered value is URL-quoted where it lands in the page."""
        if not self.console:
            return None
        values = {k: urllib.parse.quote(v, safe="/")
                  for k, v in self.values(root, slug=slug, home=home).items()}
        page = render(self.console["page"], values)
        return {"name": self.name, "title": self.console["title"],
                "url": "%s%s%s" % (PROXY_PREFIX, self.name, page)}

    def row(self):
        """The install's view of it (GET /api/plugins), nothing rendered."""
        return {"name": self.name, "description": self.description, "version": self.version,
                "dir": str(self.dir), "mcp": bool(self.mcp), "service": bool(self.service),
                "console": bool(self.console), "port": self.port,
                "title": (self.console or {}).get("title")}


def render(text, values):
    """`text` with every `{name}` replaced from `values`; KeyError for a
    placeholder `values` lacks (the manifest check refuses those first)."""
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def placeholders(text):
    return _PLACEHOLDER.findall(text)


# ---------------------------------------------------------------- parsing

def _str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _str_map(value):
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str)
                                           for k, v in value.items())


def _strings_of(part, table):
    out = []
    for key, value in table.items():
        if isinstance(value, str):
            out.append((key, value))
        elif isinstance(value, list):
            out += [(key, v) for v in value if isinstance(v, str)]
        elif isinstance(value, dict):
            out += [("%s.%s" % (key, k), v) for k, v in value.items() if isinstance(v, str)]
    return out


def _check_part(part, table, has_service):
    if not isinstance(table, dict):
        raise PluginError("[%s] must be a table" % part)
    unknown = sorted(set(table) - _PART_KEYS[part])
    if unknown:
        raise PluginError("[%s] has unknown key%s %s" % (part, "s" if len(unknown) > 1 else "",
                                                          ", ".join(unknown)))
    if part in ("mcp", "service"):
        if not isinstance(table.get("command"), str) or not table["command"].strip():
            raise PluginError("[%s] needs a `command` string" % part)
        if "args" in table and not _str_list(table["args"]):
            raise PluginError("[%s] `args` must be a list of strings" % part)
        if "env" in table and not _str_map(table["env"]):
            raise PluginError("[%s] `env` must map names to strings" % part)
    if part == "service":
        port = table.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise PluginError("[service] needs `port`, the loopback port it listens on"
                              " (1-65535)")
        health = table.get("health")
        if health is not None and (not isinstance(health, str) or not health.startswith("/")):
            raise PluginError("[service] `health` must be a path starting with /")
    if part == "console":
        if not has_service:
            raise PluginError("[console] needs [service]: the page is served by it")
        if not isinstance(table.get("title"), str) or not table["title"].strip():
            raise PluginError("[console] needs a `title` string")
        page = table.get("page")
        if not isinstance(page, str) or not page.startswith("/"):
            raise PluginError("[console] needs `page`, a path starting with /")
    allowed = _PART_VARS[part]
    for key, value in _strings_of(part, table):
        for name in placeholders(value):
            if name not in allowed:
                raise PluginError("[%s] %s uses the unknown placeholder {%s} (known here: %s)"
                                  % (part, key, name, ", ".join("{%s}" % v for v in allowed)))
            if name == "service_url" and not has_service:
                raise PluginError("[%s] %s uses {service_url} but there is no [service]"
                                  % (part, key))


def check_manifest(data, name):
    """The manifest dict checked against the entry's `name`; PluginError."""
    if not isinstance(data, dict):
        raise PluginError("%s is not a table" % MANIFEST)
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise PluginError("%s has unknown key%s %s" % (MANIFEST, "s" if len(unknown) > 1 else "",
                                                        ", ".join(unknown)))
    if data.get("name") != name:
        raise PluginError("%s names %r, not %r (the key in %s)"
                          % (MANIFEST, data.get("name"), name, CONFIG))
    for key in ("description", "version"):
        if key in data and not isinstance(data[key], str):
            raise PluginError("`%s` must be a string" % key)
    for part in ("mcp", "service", "console"):
        if part in data:
            _check_part(part, data[part], "service" in data)
    return data


def read_manifest(directory, name):
    """The checked manifest of the plugin in `directory`; PluginError."""
    path = Path(directory) / MANIFEST
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PluginError("no %s in %s" % (MANIFEST, directory))
    except (OSError, UnicodeDecodeError) as err:
        raise PluginError("%s cannot be read: %s" % (path, err))
    except tomllib.TOMLDecodeError as err:
        raise PluginError("%s does not parse: %s" % (path, err))
    return check_manifest(data, name)


def _entries(root):
    """(entries {name: table}, problems) of config/plugins.toml."""
    path = Path(root) / CONFIG
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, []
    except tomllib.TOMLDecodeError as err:
        return {}, [{"name": None, "reason": "%s does not parse: %s" % (CONFIG, err)}]
    except (OSError, UnicodeDecodeError) as err:
        return {}, [{"name": None, "reason": "%s not read: %s" % (CONFIG, err)}]
    problems = []
    unknown = sorted(set(data) - {"plugins"})
    if unknown:
        problems.append({"name": None, "reason": "%s has unknown key%s %s (only [plugins.<name>]"
                         " tables)" % (CONFIG, "s" if len(unknown) > 1 else "",
                                       ", ".join(unknown))})
    table = data.get("plugins", {})
    if not isinstance(table, dict):
        problems.append({"name": None, "reason": "%s `plugins` must be a table" % CONFIG})
        table = {}
    return table, problems


def load(root):
    """(plugins, problems): every valid plugin config/plugins.toml declares
    and does not disable, {name: Plugin} in name order, and a problem
    {"name", "reason"} for each entry skipped (name None: the file). A
    plugin with `enabled = false` is neither loaded nor a problem."""
    root = Path(root)
    entries, problems = _entries(root)
    plugins, ports = {}, {}
    for name in sorted(entries):
        entry = entries[name]
        try:
            if not NAME.match(name):
                raise PluginError("the name must match %s" % NAME.pattern)
            if name in RESERVED:
                raise PluginError("%r is reserved" % name)
            if not isinstance(entry, dict):
                raise PluginError("[plugins.%s] must be a table" % name)
            unknown = sorted(set(entry) - _ENTRY_KEYS)
            if unknown:
                raise PluginError("[plugins.%s] has unknown key%s %s"
                                  % (name, "s" if len(unknown) > 1 else "", ", ".join(unknown)))
            enabled = entry.get("enabled", True)
            if not isinstance(enabled, bool):
                raise PluginError("[plugins.%s] enabled must be true or false" % name)
            if not enabled:
                continue
            raw = entry.get("path")
            if not isinstance(raw, str) or not raw.strip():
                raise PluginError("[plugins.%s] needs `path`, the plugin's directory" % name)
            directory = Path(raw) if Path(raw).is_absolute() else root / raw
            if not directory.is_dir():
                raise PluginError("path %s is not a directory" % directory)
            plugin = Plugin(name, directory.resolve(), read_manifest(directory, name))
            if plugin.port is not None:
                if plugin.port in ports:
                    raise PluginError("[service] port %d is already %s's"
                                      % (plugin.port, ports[plugin.port]))
                ports[plugin.port] = name
        except PluginError as err:
            problems.append({"name": name, "reason": str(err)})
            continue
        plugins[name] = plugin
    return plugins, problems


# ---------------------------------------------------------------- a cousin

def cousin_enabled(home):
    """(names, problem): the cousin.toml [plugins] enabled list as written
    (deduplicated, its order kept), and a reason when the table is not
    usable (then no names)."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return [], None
    table = data.get("plugins")
    if table is None:
        return [], None
    if not isinstance(table, dict):
        return [], "cousin.toml [plugins] must be a table"
    unknown = sorted(set(table) - {"enabled"})
    if unknown:
        return [], "cousin.toml [plugins] has unknown key%s %s" % (
            "s" if len(unknown) > 1 else "", ", ".join(unknown))
    names = table.get("enabled", [])
    if not _str_list(names):
        return [], "cousin.toml [plugins] enabled must be a list of plugin names"
    return list(dict.fromkeys(names)), None


def enabled_for(home, root, loaded=None):
    """The install's valid plugins this cousin enables, in name order.
    `loaded` is a load(root) result to reuse."""
    plugins, _ = loaded if loaded is not None else load(root)
    names, _ = cousin_enabled(home)
    return [plugins[n] for n in sorted(names) if n in plugins]


def cousin_report(home, root, loaded=None):
    """(plugins, skipped): enabled_for, plus a {"name", "reason"} for each
    name the cousin enables that does not load (not installed, disabled,
    or a problem the install has with it), and for an unusable table."""
    plugins, problems = loaded if loaded is not None else load(root)
    names, problem = cousin_enabled(home)
    skipped = [{"name": None, "reason": problem}] if problem else []
    why = {p["name"]: p["reason"] for p in problems if p["name"]}
    for name in sorted(names):
        if name not in plugins:
            skipped.append({"name": name, "reason": why.get(name) or
                            "not an enabled plugin in %s" % CONFIG})
    return [plugins[n] for n in sorted(names) if n in plugins], skipped


def mcp_servers(home, root, *, slug=None, loaded=None):
    """(servers, skipped) of the plugins this cousin enables: servers is
    {name: {command, args, env}} rendered, for each one with [mcp], in
    name order; skipped is cousin_report's. `slug` defaults to the home's
    directory name."""
    home = Path(home)
    plugins, skipped = cousin_report(home, root, loaded)
    servers = {}
    for plugin in plugins:
        server = plugin.mcp_server(root, slug=slug or home.name, home=home)
        if server is not None:
            servers[plugin.name] = server
    return servers, skipped


def console_tabs(home, root, *, slug=None, loaded=None):
    """[{name, title, url}] for the plugins this cousin enables that have a
    [console] page, in name order."""
    home = Path(home)
    return [tab for tab in (p.console_tab(root, slug=slug or home.name, home=home)
                            for p in enabled_for(home, root, loaded)) if tab]


def services_wanted(root, loaded=None):
    """{name: Plugin} of the plugins with [service] that at least one
    cousin under <root>/cousins enables."""
    plugins, _ = loaded if loaded is not None else load(root)
    base = Path(root) / "cousins"
    wanted = set()
    if base.is_dir() and any(p.service for p in plugins.values()):
        for home in sorted(base.iterdir()):
            if (home / "cousin.toml").is_file():
                wanted.update(cousin_enabled(home)[0])
    return {n: p for n, p in plugins.items() if p.service and n in wanted}
