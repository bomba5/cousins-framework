# MCP tools

A cousin can use the framework's CLIs as MCP tools instead of typing
them into a shell. Same commands, same effect, but the arguments travel
as JSON, so nothing in a decision or a message gets mangled by shell
quoting on the way. This page covers what a cousin gets, the registry
file that defines it, and how to add your own tools.

## What a cousin gets

Every cousin spawned by the framework comes with five tools:

| Tool | Runs | Commands |
|---|---|---|
| `memory` | `cousin-memory` | `search`, `decide`, `remember`, `recall`, `activity` |
| `send` | `cousin-chat send` or `cousin-reply` | picked by the destination |
| `job` | `cousin-job` | `start`, `done`, `fail`, `list`, `show` |
| `schedule` | `cousin-schedule` | `add`, `list`, `cancel` |
| `meeting` | `cousin-meeting` | `say`, `pass`, `minutes`, `show` ([meetings](meetings.md)) |

A call names the command and its arguments:

```json
{"command": "decide", "topic": "port range", "decision": "cap at 8200",
 "reasoning": "leaves room for `hive` and $(whatever)", "level": "conclusion"}
```

The adapter turns that into `cousin-memory decide --stdin --level
conclusion` with the three texts on stdin. There's never a shell in
between, so backticks, `$(...)`, quotes and newlines reach the CLI
exactly as written.

`send` is the one that does more than pass arguments through. It takes
`to` and `text` (and optionally `image` or `video`):

- `to` is a peer cousin's slug: it runs `cousin-chat send <slug> <text>`.
- `to` is one of the names in the registry's `operators` list: it runs
  `cousin-reply --user <name>` with the text on stdin, plus `--image`
  or `--video` if given.
- Anything else is an error that lists who the cousin can reach. There's
  no default, so a typo in a slug fails instead of posting into the
  wrong thread.

That removes the classic mistake of answering a peer cousin with
`cousin-reply` ([chat](chat.md#cousin-to-cousin)). The peer list is read
once, from `cousin-chat list`, when the MCP process starts.

`cousin-spawn --operator ana` puts `ana` in the new cousin's `operators`
list. Without it, `send` reaches peers only, and its error says no
operator is configured. For an existing cousin, edit the `operators =
[...]` line in its `mcp-registry.toml`.

What's deliberately not there: spawn, flip, reincarnate, transplant,
loop control and shared-tier review. Those are yours, not the cousin's.

MCP only covers what the cousin does. Messages still reach the cousin
as lines typed into its pane by the chat server; MCP adds no way in.

## How it's wired

Spawn writes two files into every new cousin home:

- `mcp-registry.toml`: a copy of the install's default registry, with
  the operator filled in.
- `.mcp.json`: tells Claude Code to start `cousin-mcp` over stdio with
  `--registry <home>/mcp-registry.toml` and the cousin's
  `COUSIN_HOME`, `COUSIN_SLUG` and `FRAMEWORK_ROOT` in its environment.
  The command is the `cousin-mcp` next to the Python that ran spawn, by
  absolute path, so two installs on one machine don't get mixed up.

Spawn also writes `.claude/settings.json` in the home, which enables the
`cousin` server for that project, so Claude Code picks the tools up at
the next session start without asking. The adapter is one process per
cousin, started and stopped by the cousin's own Claude Code session. It
listens on no port and has exactly the access the cousin's shell has.

Serving needs the MCP Python SDK, which is an optional extra:

```
pip install -e ".[mcp]"       # from the checkout, in its venv
```

Without it, `cousin-mcp` exits 2 and prints that line. Everything else
(`--selftest`, `--list-tools`, `--call`) works without the SDK.

For a cousin that predates this, or whose `.mcp.json` points at an old
path:

```
cousin-spawn wren --repair-settings
```

That rewrites the `cousin` entry of `.mcp.json` (keeping any other
servers) and the settings file. If your harness keeps approvals in a
global file instead, `cousin-mcp approve` records one there: it marks the
home trusted and adds `cousin` to `enabledMcpjsonServers` in the file
that `config/harness.toml` names as `settings_file` (`~/.claude.json` in
the Claude Code preset), and touches nothing else:

```
cousin-mcp approve wren --root "$PWD"
```

### The in-process transport

On the SDK lane, the runner does not spawn `cousin-mcp` over stdio: the
same registry builds the tools inside the runner's own process
(`cousin_lib/runner/tools.py`). Schemas come from `mcp_server.build_schema`
either way, so a tool cannot exist on one transport and not the other.
Each registry command maps to a library function in `HANDLERS`; a
command the registry enables but `HANDLERS` has no function for stops
the runner at start, with the list of what's missing. Two tools exist
only in-process and never over stdio: `reply`, the sole writer of
`chat.db` on this lane, and `handoff`, which writes the next
generation's `data/handoff-manual.md`.

## Checking it

```
cousin-mcp --selftest                  # the registry, every schema, where each command resolves
cousin-mcp --list-tools                # the schemas the model sees, as JSON
cousin-mcp --call memory '{"command": "search", "query": "descaling"}'
cousin-mcp --versions                  # MCP protocol versions the installed SDK speaks
```

Run them with the cousin's environment (`COUSIN_HOME`, `FRAMEWORK_ROOT`)
to see exactly what the cousin sees:

```
export COUSIN_HOME=$PWD/cousins/wren FRAMEWORK_ROOT=$PWD
cousin-mcp --selftest
#   -> registry: .../cousins/wren/mcp-registry.toml (4 tools, ceiling 12, timeout 120s, output cap 16000 chars)
#        memory    cousin-memory                activity, decide, recall, remember, search
#        send      cousin-chat, cousin-reply    operator, peer (operators: ana)
#        job       cousin-job                   done, fail, list, show, start
#        schedule  cousin-schedule              add, cancel, list
#        cousin-memory -> beside the interpreter
#        ...
#      mcp sdk: present, protocol versions 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25
#      selftest ok: 4 schema(s) built
```

`--selftest` exits 1 if any command can't be found, or if any tool did
not validate. Without `--registry`, `cousin-mcp` uses the cousin's own
`mcp-registry.toml`, else `config/mcp-registry.toml`, else
`config/mcp-registry.toml.example`.

When the server does not come up at all, the harness reports only
`CONNECTION_CLOSED`; the reason is the server's stderr, which it writes
to its own per-session log under `mcp_logs_dir`. `cousin-mcp
--last-connection` reads that log and prints the outcome, the stderr and
the file, exiting 0 connected, 1 failed, 2 nothing recorded. The boot
packet carries the same line when the last recorded connection failed,
so a cousin is told rather than left to notice its tools are missing.

Serving is lenient per tool. A tool that does not validate is skipped,
with a line on stderr naming it and why, and the rest of the registry
still serves. One file carries every tool, so a strict load meant one
bad table cost the cousin its whole MCP surface, with the reason visible
only in the harness's own log. What is wrong with the file itself,
unparseable TOML or the tool ceiling, is still fatal: no subset of such
a file is trustworthy. Use `--selftest` to see skipped tools on purpose;
it fails when there are any.

Every connect also records the MCP protocol version the client asked
for in `data/mcp-client.json` in the cousin home. If Claude Code moves
to a protocol the installed SDK doesn't speak, that file and
`--versions` show it.

## The registry

The registry is a TOML file with no magic in the adapter: whatever it
lists is what the cousin gets. The shipped default is
`config/mcp-registry.toml.example`. There are three copies that matter:

- `config/mcp-registry.toml.example`: shipped, don't edit it.
- `config/mcp-registry.toml`: your install's default, if you copy the
  example and edit it. Spawn copies this one (or the example) into new
  cousins.
- `<home>/mcp-registry.toml`: the cousin's own, what it actually uses.
  Changing the install default doesn't touch existing cousins.

The registry is read once when the adapter starts, and Claude Code asks
for the tool list once per session. A change shows up at the cousin's
next session start.

Top-level settings:

```toml
ceiling = 12          # more enabled tools than this is an error
timeout = 120         # seconds per call; a timeout is an error, not retried
max_output = 16000    # characters of stdout returned; the cut is stated
```

The ceiling is there so a handful of coarse tools doesn't quietly turn
into one tool per subcommand.

A tool looks like this:

```toml
[tools.schedule]
command = "cousin-schedule"
description = "One-shot future prompts to yourself: add, list, cancel."

[tools.schedule.properties]
when = { type = "string", description = "e.g. 'in 30m' or 'tomorrow 09:00' (add)" }
prompt = { type = "string", description = "(add)" }
id = { type = "integer", description = "(cancel)" }
all = { type = "boolean", description = "include fired entries (list)" }

[tools.schedule.commands.add]
argv = ["add", "{when}", "{prompt}"]

[tools.schedule.commands.list]
argv = ["list"]
options = { all = "--all" }

[tools.schedule.commands.cancel]
argv = ["cancel", "{id}"]
```

- `command`: a console script name or an absolute path. A bare name is
  looked up next to the Python running `cousin-mcp` first, then on
  PATH.
- `description`: what the model reads. `enabled = false` hides the tool
  completely.
- `properties`: the arguments, with `type` (`string`, `integer`,
  `number`, `boolean`, `array`; for arrays `items = "string"`),
  `description`, and optionally `enum` and `optional = true`. The
  adapter adds a `command` property listing the tool's commands.
- `commands.<name>.argv`: literal strings, or exactly `{property}` for a
  value. A placeholder has to be the whole element. A call that leaves
  out a placeholder's property is an error, unless the property is
  `optional = true`, in which case the element is dropped.
- `commands.<name>.options`: `property = "--flag"`. `true` gives the
  bare flag, a list repeats the flag once per item, anything else goes
  after the flag. Left out or `false`, there's no flag.
- `commands.<name>.stdin`: properties joined and fed on stdin, with
  `stdin_sep` on its own line between them. This is how `decide` gets
  its three chunks.
- `commands.<name>.command`: overrides the tool's command for one
  subcommand (that's how `send` uses two CLIs).

An `enum` is checked by the adapter before anything runs. Everything
else is checked by the CLI's own argument parser. The default `job`
tool uses that on purpose: its `kind` enum leaves out `shell`, because
`start` over MCP takes no command and a shell row would never close.

A non-zero exit comes back as a tool error with the CLI's stderr, not
as a protocol error. A call with nothing for stdin gets `/dev/null`.

Two special kinds exist besides the plain `command` kind: `kind =
"send"` (the resolver above; it needs `commands.peer`,
`commands.operator` and an `operators` list, and takes `peers_from` and
`errors_name_peers = false` to report only a count of known peers in
errors), and `kind = "job"`, for wrapping a slow command as a tracked
background job with `gen`, `status` and `result` commands. The default
registry doesn't use the job kind; `mcp_server.py` documents it.

## Adding a tool

Say you want cousins to use the tracker. Add this to the cousin's
`mcp-registry.toml` (or to `config/mcp-registry.toml` for future
cousins):

```toml
[tools.tracker]
command = "cousin-tracker"
description = "The install's list of work in flight: list items, add one, move one to a new state."

[tools.tracker.properties]
title = { type = "string" }
domain = { type = "string", optional = true, description = "grouping key, e.g. infra" }
tag = { type = "array", items = "string", optional = true }
id = { type = "integer", description = "item id" }
state = { type = "string", enum = ["open", "active", "blocked", "done", "dropped"] }

[tools.tracker.commands.list]
argv = ["list", "--json"]
options = { domain = "--domain", state = "--state" }

[tools.tracker.commands.add]
argv = ["add", "{title}", "--json"]
options = { domain = "--domain", tag = "--tag" }

[tools.tracker.commands.state]
argv = ["state", "{id}", "{state}", "--json"]
```

Then check it and try a call before the cousin does:

```
cousin-mcp --selftest
cousin-mcp --call tracker '{"command": "add", "title": "renew the cert", "tag": ["ops"]}'
```

The cousin gets the new tool at its next session start (or after a
flip).

A few rules for what to add:

- Prefer one coarse tool with a few commands over many small tools.
- Only register CLIs a cousin should run on its own. Operator actions
  stay out.
- Never register anything that runs a shell on its input. That puts the
  model's text one step from a live shell, which is the thing this
  whole adapter avoids.
- The registry is trusted configuration, like `cousin.toml`. A cousin
  that can edit its own registry can register any command it can name;
  nothing in the adapter stops that.
