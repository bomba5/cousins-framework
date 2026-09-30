# Plugins

A [plugin](glossary.md#plugin) adds something to an install that the framework does not ship: a
set of tools for a [cousin](glossary.md#cousin), a long-running service, a page in the console,
or any mix of the three. The framework knows how to run plugins; it ships
none. An install without plugins looks and behaves exactly as one without this
page: no tab, no settings section, no extra child.

## What a plugin is

A directory holding `plugin.toml`, its manifest, and whatever the plugin needs
beside it (its code, its assets). Three parts, each optional:

| part | what the framework does with it |
|---|---|
| `[mcp]` | a stdio MCP server the [runner](glossary.md#runner) starts beside the cousin's own tools, for each cousin that enables the plugin |
| `[service]` | one process for the whole install, kept up by the [supervisor](glossary.md#supervisor) while at least one cousin enables the plugin |
| `[console]` | a page on that service, shown on the chat view of each cousin that enables the plugin: a tab over the pane, or a strip over the chat |

## The manifest

```toml
name = "clock"                  # must equal its key in config/plugins.toml
description = "the time, in a tab and as a tool"
version = "0.1.0"

[mcp]                           # optional: tools for a cousin that enables it
command = "/usr/bin/python3"
args = ["{plugin_dir}/mcp_server.py"]
env = { CLOCK_URL = "{service_url}" }

[service]                       # optional: one process for the install
command = "/usr/bin/python3"
args = ["{plugin_dir}/server.py"]
env = { CLOCK_BIND = "127.0.0.1" }
port = 8190                     # required with [service]: the loopback port it listens on
health = "/healthz"             # optional: a GET path, 2xx is healthy

[console]                       # optional, needs [service]: the page
title = "Clock"
page = "/page/{slug}"           # a path on the service
placement = "pane"              # optional: "pane" (a tab over the pane) or "chat" (a strip over the chat)
```

`name` matches `^[a-z][a-z0-9-]{0,31}$`; `cousin` is reserved. `description`
and `version` are free strings. `command` and `args` are an argv (no shell);
`env` maps names to strings. `placement` is `"pane"` (the default) or
`"chat"`; see [the page](#what-happens) below.

Placeholders, rendered by the framework in `command`, `args`, `env` values and
`page`:

| placeholder | value | where |
|---|---|---|
| `{plugin_dir}` | the plugin's directory, absolute | everywhere |
| `{root}` | the framework root | everywhere |
| `{service_url}` | `http://127.0.0.1:<port>` | everywhere, when there is a `[service]` |
| `{slug}` | the cousin's slug | `[mcp]`, `[console]` |
| `{home}` | the cousin's home | `[mcp]`, `[console]` |

On top of its own `env`, the MCP server gets `COUSIN_SLUG`, `COUSIN_HOME`,
`FRAMEWORK_ROOT` and `PLUGIN_DIR`; the service gets `PLUGIN_PORT`,
`PLUGIN_DIR` and `FRAMEWORK_ROOT`, on top of the supervisor's environment.

Parsing is strict. An unknown key, a value of the wrong type, a placeholder
that is not in the table (or `{slug}` in a `[service]`), a `placement` other
than `"pane"` or `"chat"`, `[console]` without
`[service]`, or two services on one port: the plugin is skipped and the reason
is named, in `cousin-supervisor status`, in `GET /api/plugins`, and in the
cousin's `mcp_config` event. One bad plugin never stops the install or a
cousin.

## Declaring it: config/plugins.toml

The install's list of plugins ([configuration](configuration.md#pluginstoml)).
Absent: no plugins.

```toml
[plugins.clock]
path = "plugins-local/clock"    # absolute, or relative to the framework root
enabled = true                  # optional, default true; false hides it everywhere
```

## Enabling it on a cousin

In the cousin's `cousin.toml` ([configuration](configuration.md#plugins-a-cousins-plugins)):

```toml
[plugins]
enabled = ["clock"]
```

or the "plugins" list in the console's inspector, which writes the same key.
The runner reads the list at start, so a change needs a restart of the cousin,
like the other `[agent]` settings; the console offers it. The console also
asks the supervisor to rescan, which starts the plugin's service when this is
its first cousin, and stops it when this was its last.

Whatever the plugin needs per cousin is its own business, in its own file
(say `<home>/clock.toml`): the framework never reads it.

## What happens

**Tools.** At start, the runner adds one stdio MCP server per enabled plugin
with `[mcp]`, named after the plugin, to the servers it loads from the home's
`.mcp.json`. A `.mcp.json` server of the same name wins, and the plugin's is
skipped with the reason. The `mcp_config` event on the cousin's event [stream](glossary.md#stream)
lists each plugin server with `"source": "plugin"`. On the `sdk` kind the
server is in the SDK's options; on `opencode` it is a `local` entry in the
rendered config (the only MCP servers that kind loads are the cousin's own and
these). The `tmux` kind does not get them in 2.1.0: its pane reads `.mcp.json`
itself ([runners](reference/runners.md#known-gaps-on-tmux)); the console says
so on a tmux cousin.

**The service.** The supervisor runs one child `plugin:<name>` per plugin with
a `[service]` that some cousin enables, before the runners and stopped after
them, restarted with the same backoff as every child, its output also in
`<root>/data/plugins/<name>/service.log`. `cousin-supervisor reload` picks up a
newly enabled plugin, drops one no cousin enables any more, and restarts one
whose command or env changed. `cousin-supervisor status` lists it.

**The page.** A plugin's `[console]` page is an iframe of
`/plugins/<name><page>`, the page rendered for that cousin. Where it shows is
its `placement`:

- `"pane"` (the default): a tab. The cousin's chat view gets a row of tabs over
  its pane, one per such plugin, beside the reasoning (or terminal) tab.
- `"chat"`: a strip on top of the chat, inside the chat column, like a video
  call: you see the page while you chat. The strip has a thin bar with the
  plugin's title, a "pop out" button that opens the page in its own window,
  and a collapse button that shrinks it to that bar; drag its bottom edge to
  set its height (at least 120 px, at most 70% of the column; a double-click
  resets it to 240 px). Height and collapsed state are kept in your browser, per
  cousin and plugin. The messages and the composer stay visible below it at
  every size, and a chat that was at its latest message stays there as the
  strip moves. Two such plugins on one cousin stack two strips. The strip is
  on the normal chat view; fullscreen hides it (the page stays loaded) and an
  embedded chat (`?embed=1`) has none.

Without a plugin page there is neither row nor strip: the chat view is the one
an install without plugins has.

**The proxy.** `/plugins/<name>/<path>` on the console forwards GET, POST and
HEAD to `http://127.0.0.1:<port>/<path>`, the query kept, behind the console's
own login. The answer streams through as it comes: an event stream stays open
and each event arrives when the service sends it. Status codes and the
content type pass through; hop-by-hop headers, the console's cookie and any
`Authorization` header do not; the request body is capped at 1 MB; a service
that is down answers 502. Only the declared port on loopback is reachable. The
details are in the [console API](reference/console-api.md#plugins).

A plugin's page is served from the console's own origin, so its scripts act
with your console session. Install only plugins you trust, as you would any
code you run on the host.

## What the framework does not do

- Ship a plugin. Every name on this page is an example.
- Read a plugin's per-cousin settings, or anything in its directory but
  `plugin.toml`.
- Install a plugin's dependencies, or start a service no cousin enables.
- Reach a service anywhere but `127.0.0.1:<its port>`.

## Writing one

A minimal plugin, "clock", with all three parts. `server.py`:

```python
import http.server, json, os, time

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body, ctype = b"ok", "text/plain"
        elif self.path == "/now":
            body, ctype = json.dumps({"now": time.time()}).encode(), "application/json"
        else:  # /page/<slug>, the tab
            body, ctype = b"<p>" + time.ctime().encode() + b"</p>", "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["PLUGIN_PORT"])),
                                Handler).serve_forever()
```

`mcp_server.py` speaks MCP over stdin and stdout (any MCP library will do);
its one tool can read `CLOCK_URL` + `/now`. Then:

1. put both files and the manifest above in `plugins-local/clock/`;
2. declare it in `config/plugins.toml`;
3. enable it on a cousin (the inspector, or `[plugins] enabled`);
4. `cousin-supervisor reload`, and restart the cousin.

The page's links and requests should be relative, or start with
`/plugins/clock/`: the console serves the page under that prefix (the proxy
sends it as `X-Forwarded-Prefix`).
