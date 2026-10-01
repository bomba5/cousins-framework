# Plugins

A [plugin](glossary.md#plugin) adds something to an install that the framework does not ship: a
set of tools for a [cousin](glossary.md#cousin), a long-running service, a page in the console,
or any mix of the three. The framework knows how to run plugins; it ships
none. An install without plugins looks and behaves exactly as one without this
page: no tab, no settings section, no extra child.

Not to be confused with `plugins/opencode/` in the repository: that is the
`opencode` [runner](glossary.md#runner) kind's own policy pack (`cousin-policy.js`), which opencode
loads itself ([runners](reference/runners.md)). It has no `plugin.toml`, is
never declared in `config/plugins.toml`, and nothing on this page applies to
it.

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
`env` maps names to strings. `port` is an integer from 1 to 65535. `health`
is a path starting with `/`: `GET /api/plugins` probes it (1 second, 2xx is
healthy) to show the service's health; the supervisor does not act on it.
`title` is a non-empty string, `page` a path starting with `/`, and
`placement` is `"pane"` (the default) or `"chat"`; see
[the page](#what-happens) below.

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

Parsing is strict. An unknown key, a value of the wrong type, a `name` that
is not the plugin's key in `config/plugins.toml`, a placeholder that is not
in the table (or `{slug}` in a `[service]`, or `{service_url}` without one),
a `placement` other than `"pane"` or `"chat"`, `[console]` without
`[service]`, or a `[service] port` another plugin already has (the plugins
are read in name order, so the later name is the one skipped): the plugin is
skipped and the reason is named, in `cousin-supervisor status`, in
`GET /api/plugins`, and in the cousin's `mcp_config` event. One bad plugin
never stops the install or a cousin.

## Declaring it: config/plugins.toml

The install's list of plugins ([configuration](configuration.md#pluginstoml)).
Absent: no plugins.

```toml
[plugins.clock]
path = "plugins-local/clock"    # absolute, or relative to the framework root
enabled = true                  # optional, default true; false hides it everywhere
```

The file holds only `[plugins.<name>]` tables, each with only `path` and
`enabled`; `path` must be a directory holding `plugin.toml`. A plugin with
`enabled = false` is not loaded and not reported as a problem: a cousin that
still enables it sees it skipped as "not an enabled plugin".

## Enabling it on a cousin

In the cousin's `cousin.toml` ([configuration](configuration.md#plugins-a-cousins-plugins)):

```toml
[plugins]
enabled = ["clock"]
```

or the "plugins" list in the console's inspector, which writes the same key
(`enabled` is the table's only key). A name the install does not have, or
one it skipped, is listed with the reason in the inspector and in
`GET /api/cousins/<slug>/plugins`; the console refuses to write one.
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
these). The `tmux` kind does not get them: its pane reads `.mcp.json`
itself ([runners](reference/runners.md#known-gaps-on-tmux)); the console says
so on a tmux cousin. The plugin's tab and service work on every kind.

**The service.** The supervisor runs one child `plugin:<name>` per plugin with
a `[service]` that some cousin enables, before the runners and stopped after
them, restarted with the same backoff as every child, its output also in
`<root>/data/plugins/<name>/service.log`. Its environment is the
supervisor's own, plus the manifest's rendered `env`, plus `PLUGIN_PORT`,
`PLUGIN_DIR` and `FRAMEWORK_ROOT`. `cousin-supervisor reload` picks up a
newly enabled plugin, drops one no cousin enables any more, restarts one
whose command or env changed, and starts again one left `failing`.
`cousin-supervisor status` lists it. There is no `start` or `stop` of a
plugin by name: a service runs while some cousin enables it.

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
  on the normal chat view; fullscreen and a collapsed chat hide it (the page
  stays loaded) and an embedded chat (`?embed=1`) has none.

Without a plugin page there is neither row nor strip: the chat view is the one
an install without plugins has.

**The proxy.** `/plugins/<name>/<path>` on the console forwards GET, POST and
HEAD to `http://127.0.0.1:<port>/<path>`, the query kept, behind the console's
own login. The request's headers go along except the hop-by-hop ones, the
console's cookie and any `Authorization` header, and the proxy adds
`X-Forwarded-Prefix: /plugins/<name>`. The answer's status and headers come
back as the service sent them (hop-by-hop headers dropped), and its body
streams through as it comes: a `text/event-stream` answer stays open as long
as the service keeps it, with no timeout, and each event arrives when the
service sends it. Any other answer may pause at most 60 seconds between
reads. The request body is capped at 1 MB (413); a service that does not
connect and send its answer's headers within 5 seconds is a 502; a name with no
`[service]` is 404, and any other method 405. Only the declared port on
loopback is reachable. The details are in the
[console API](reference/console-api.md#plugins).

## What a plugin can reach

A plugin is code you run on the host, with the framework's own rights.
Install only plugins you trust, as you would any program. Concretely:

- **Its page acts as you.** The page is an iframe served from the console's
  own origin, with no sandbox: its scripts can call every console route with
  your session, as the console's own pages do. Headers its service sends,
  `Set-Cookie` included, reach your browser on that origin.
- **Its service sees the supervisor's environment.** The service is a child
  of the supervisor and inherits its environment, so any secret set there
  (an API key in the supervisor's unit, say) is visible to it. Keep secrets
  in account files, not in the supervisor's environment.
- **Its MCP server runs as the cousin does.** It is started for the cousin's
  agent, as the same Unix user, from the same host or container, and can
  read and write what the cousin can.
- **It is not sandboxed by the framework.** The framework checks the manifest
  and decides when to start each part. It does not limit what a part does
  once it runs.

## What the framework does not do

- Ship an installed plugin. `examples/plugins/hello/` is an example to copy,
  declared nowhere until you declare it; every other name on this page is an
  example too.
- Read a plugin's per-cousin settings, or anything in its directory but
  `plugin.toml`.
- Install a plugin's dependencies, or start a service no cousin enables.
- Reach a service anywhere but `127.0.0.1:<its port>`.

## Writing one

`examples/plugins/hello/` is a complete plugin with all three parts, standard
library only:

| file | what it is |
|---|---|
| `plugin.toml` | the manifest: `[mcp]`, `[service]` on port 8191 with `/healthz`, and a `[console]` tab |
| `server.py` | the service: `GET /hello?name=<name>` answers `{"greeting": "hello, <name>"}`, `/healthz` answers `ok`, `/page/<slug>` is the tab |
| `mcp_server.py` | the MCP server: one tool, `hello`, that asks the service for the greeting |

The manifest:

```toml
name = "hello"
description = "a greeting, as a tool and as a page"
version = "0.1.0"

[mcp]
command = "python3"
args = ["{plugin_dir}/mcp_server.py"]
env = { HELLO_URL = "{service_url}" }

[service]
command = "python3"
args = ["{plugin_dir}/server.py"]
port = 8191
health = "/healthz"

[console]
title = "Hello"
page = "/page/{slug}"
```

The service listens on `PLUGIN_PORT` on `127.0.0.1` only. The MCP server
speaks JSON-RPC 2.0 over stdin and stdout, one message per line, and answers
`initialize`, `tools/list` and `tools/call`; any MCP library does the same
job. The tool reaches the service through `HELLO_URL`, which the manifest
fills from `{service_url}`.

To try it on Wren:

1. declare it in `config/plugins.toml`:

   ```toml
   [plugins.hello]
   path = "examples/plugins/hello"
   ```

2. enable it on the cousin: the inspector's plugins list, or
   `[plugins] enabled = ["hello"]` in `cousins/wren/cousin.toml`;
3. `cousin-supervisor reload` (the console's save asks for it too), which
   starts `plugin:hello`; `cousin-supervisor status` lists it;
4. restart Wren, so its runner loads the `hello` tool.

Wren's chat view now has a "Hello" tab, and Wren can call `hello`. To write
your own, copy the directory somewhere outside the checkout, change `name`
(and its key in `config/plugins.toml`) and pick a free `port`.

The page's links and requests should be relative, or start with
`/plugins/<name>/`: the console serves the page under that prefix (the proxy
sends it as `X-Forwarded-Prefix`). The hello page fetches `../hello`, which
resolves to `/plugins/hello/hello`.
