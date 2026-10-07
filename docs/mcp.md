# MCP tools

A [cousin](glossary.md#cousin) can use the framework's CLIs as MCP tools instead of typing
them into a shell. Same commands, same effect, but the arguments travel
as JSON, so nothing in a decision or a message gets mangled by shell
quoting on the way. This page covers what a cousin gets, the registry
file that defines it, and how to add your own tools.

## What a cousin gets

Every cousin spawned by the framework comes with five tools:

| Tool | Runs | Commands |
|---|---|---|
| `memory` | `cousin-memory` | `search`, `decide`, `remember`, `why`, `obsolete`, `recall`, `activity` |
| `send` | `cousin-chat send` or `cousin-reply` | picked by the destination |
| `job` | `cousin-job` | `start`, `run`, `done`, `fail`, `list`, `show` |
| `schedule` | `cousin-schedule` | `add`, `list`, `cancel` |
| `meeting` | `cousin-meeting` | `say`, `pass`, `minutes`, `show` ([meetings](meetings.md)) |

A cousin's tools come from its own `mcp-registry.toml`, copied from the
install's at spawn. `why`, and `derived_from` on `remember` and `decide`,
are in the shipped `config/mcp-registry.toml.example` since 3.8.0, and
`scope` and `valid_until` on `remember` since 3.31.0, and `depth` on `why`
since 3.32.0; a cousin spawned before that, or from an install whose own
`config/mcp-registry.toml` predates them, has them only once its copy is
updated.

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
  wrong [thread](glossary.md#thread).

That removes the classic mistake of answering a peer cousin with
`cousin-reply` ([chat](chat.md#cousin-to-cousin)). The peer list is read
once, from `cousin-chat list`, when the MCP process starts.

`cousin-spawn --operator ana` puts `ana` in the new cousin's `operators`
list. For an existing cousin, edit the `operators = [...]` line in its
`mcp-registry.toml`. On the `sdk` and `opencode` kinds, `send` also
reaches the cousin's own `[operator] name` from `cousin.toml`, so a cousin
with an empty list still reaches its operator; the error names the known
peers and operators (`unknown destination 'x'; known peers: ...; known
operators: ...`). On the `tmux` kind (the stdio server) only the
`operators` list counts, and with it empty `send` reaches peers only and
its error says no operator is configured.

`job run` is how a cousin launches a long shell command as a tracked
job without a shell of its own: it takes `title`, `argv` (the command as
an array, one element per argument, never a shell string), and
optionally `desc` and `log` (a path relative to the cousin's home;
without it, the job's own log under `data/job-logs/`). It runs
`cousin-job start shell --json [--desc D] [--home-log L] -- TITLE
ARGV...`, so it's the same launcher. The title comes after `--`, so a
title like `--json` is only a title. `--home-log` confines the log to
the home: an absolute path, `~`, `..` or anything under `.secrets` is
refused before a row exists. From there it's the usual launcher: the command runs detached in its own process group, from the
cousin's home, its output streams into the row's log for the console's
Jobs view, and the row closes `done` or `failed` with the command's exit
code (or turns `lost` if its [runner](glossary.md#runner) dies without closing it,
[jobs and loops](jobs-and-loops.md)). The call returns at once with `{"job_id": ..., "log_path": ...}`
and never waits for the command. An empty `argv`, one with an element
that isn't a string, or a program starting with `-`, is refused before
anything runs. The row records
the command line as given, so a secret in `argv` ends up in the jobs
store: pass secrets some other way.

What's deliberately not there: spawn, [flip](glossary.md#flip), reincarnate, transplant,
loop control and shared-tier review. Those are yours, not the cousin's.

MCP only covers what the cousin does. Messages reach the cousin through
its [inbox](glossary.md#inbox), which its [runner](glossary.md#runner) reads; MCP adds no way in.

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

On the SDK [lane](glossary.md#lane), the [runner](glossary.md#runner) does not spawn `cousin-mcp` over stdio: the
same registry builds the tools inside the runner's own process
(`cousin_lib/runner/tools.py`). Schemas come from `mcp_server.build_schema`
either way, so a tool cannot exist on one transport and not the other.
Each registry command maps to a library function in `HANDLERS`; a
command the registry enables but `HANDLERS` has no function for stops
the runner at start, with the list of what's missing. Two tools are
the runner's own, not registry commands: `reply`, the sole writer of
`chat.db` on this lane, and `handoff`. The stdio server serves them only
to a `tmux`-kind cousin, whose pane reaches the framework that way; any
other cousin gets them in-process. Handlers are library calls in the
runner's process, with one exception: `job run` starts the `cousin-job
start shell` launcher in a fresh interpreter (the same code the runner
imported), from the cousin's home, because forking the multi-threaded
runner itself could hang the child.

`handoff` ends the generation: the runner asks for it at a [rollover](glossary.md#rollover) (see
`rollover_at_percent` in [`[agent] runner`](configuration.md#agent-runner)),
and it is called exactly once, with five fields: `position` (a paragraph,
where the work stands), `next_action` (the first thing the next generation
should do) and `status` (markdown, the section's body: replaces
`STATUS.md`'s bare `## Open loops` section, the live one every reader
reads, and the rest of the file is kept; the framework writes the heading,
so a leading "Open loops" heading in `status` is dropped and a `#` or `##`
heading inside becomes `###`) are required, `active_threads` (one
string per in-flight thread, written to `data/active-threads.md`) and
`learned` (facts not yet in memory, each `{topic, fact, level, cite}`,
remembered through the same path `cousin-memory remember` uses) are taken
when the model has them. A wrong shape (`active_threads` that is not a
list of strings, a `learned` that is not a list, or an item without a
`topic` and a `fact`) is a tool error before any write, so `STATUS.md`
does not move. It writes `STATUS.md`'s open loops, then
`data/active-threads.md` when given, then the memories, then
`data/handoff.md` last (the write order the module docstring calls the
ritual), and returns one line naming what it wrote and how many memories,
then any memory written at a lower level than asked (law 10: an uncited
`framework` or `tool` level is written as `conclusion`, with its
`demoted:` note) and any memory it could not write, with the reason.

A `kind = "job"` registry tool (its `gen`, `status` and `result`
commands, wrapping a slow shelled-out command as a tracked background
job) has no in-process handler: a cousin whose registry
enables one refuses to start on the runner, named in the same missing-
handlers list, exit 2. And a registry command's `argv` with a flag
baked in (`argv = ["list", "--json"]`, say) is not honoured in-process:
the in-process handler only sees the arguments the model actually
passed, so it returns the CLI's default (non-flagged) text unless the
model asks for that behaviour itself (`{"json": true}`).

### A runner cousin's own MCP servers

Per kind, an operator-added `<home>/.mcp.json` server beyond `cousin` is
handled differently:

- **sdk.** `cousin_lib/runner/mcp_config.py` reads the home's
  `.mcp.json` once, at the first [turn](glossary.md#turn), and keeps it for the runner's
  life; an edit lands at the next start. Each entry maps onto the SDK's
  server config as one of three shapes: stdio (`command`, `args`,
  `env`), `http` or `sse` (`url`, `headers`). `cousin` is reserved: an
  entry of that name is skipped, since the runner serves its own tools
  in-process. These servers reach the agent CLI in a private file
  (`data/run/mcp-config.json`, mode 0600), never inline on its command
  line, which every local user can read; only `cousin`, a name, is
  inline. `${VAR}` and `${VAR:-default}` pass through unexpanded for
  the agent CLI to expand from its own environment; a reference with no
  default to a variable unset in the runner's environment skips the
  entry (the CLI would refuse such a config), and a reference to one of
  the cousin's own credential variables skips the entry regardless, so
  a `.mcp.json` the model can edit can never route the cousin's
  credential to another server. A file that does not parse, or an entry
  of an unknown shape, is skipped with the reason, never fatal: the
  cousin still starts with `cousin`. `cousin`'s own tools load with
  `alwaysLoad` set, so they never sit behind the CLI's tool search; a
  user server's tools stay deferred there. The runner's event [stream](glossary.md#stream)
  carries an `mcp_config` event with server names, types, ignored keys
  and skip reasons, ordered by name, never a value.
- **tmux** (the tmux runner kind). Claude Code reads
  `<home>/.mcp.json` itself, the ordinary project-config way, including
  the `cousin` entry spawn wrote (see "How it's wired" above). An extra
  server an operator adds there works exactly as Claude Code has always
  handled it; the framework does nothing special for it.
- **opencode.** The runner renders opencode's own config with the
  `cousin` MCP server, a remote HTTP bridge
  (`cousin_lib/runner/mcp_http.py`) with a bearer token, plus a `local`
  entry for each MCP server of a [plugin](plugins.md) the cousin enables;
  it does not read the home's `.mcp.json` for this kind. Before a turn it checks
  opencode's effective config (every config source opencode itself
  merged) and refuses to run if any server besides those shows up
  there, so a stray opencode config file can't add a server the runner
  never rendered.
- **fake.** No MCP servers of any kind; it exists for tests.

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
#   -> registry: .../cousins/wren/mcp-registry.toml (5 tools, ceiling 12, timeout 120s, output cap 16000 chars)
#        memory    cousin-memory                activity, decide, obsolete, recall, remember, search
#        send      cousin-chat, cousin-reply    operator, peer (operators: ana)
#        job       cousin-job                   done, fail, list, run, show, start
#        meeting   cousin-meeting               minutes, pass, say, show
#        schedule  cousin-schedule              add, cancel, list
#        cousin-memory -> beside the interpreter
#        ...
#      mcp sdk: present, protocol versions 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25
#      selftest ok: 5 schema(s) built
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
  `optional = true`, in which case the element is dropped. An `array`
  property as a placeholder spreads into one element per item; it has
  to be a JSON array of its `items` type, and not empty unless it's
  optional (that's how `job run` passes a command line).
- `commands.<name>.options`: `property = "--flag"`. `true` gives the
  bare flag, a list repeats the flag once per item, anything else goes
  after the flag. Left out or `false`, there's no flag. The flags go at
  the end of the argv, or just before a literal `"--"` in it, so the
  operands after `--` stay last.
- `commands.<name>.stdin`: properties joined and fed on stdin, with
  `stdin_sep` on its own line between them. This is how `decide` gets
  its three chunks.
- `commands.<name>.command`: overrides the tool's command for one
  subcommand (that's how `send` uses two CLIs).

An `enum` is checked by the adapter before anything runs. Everything
else is checked by the CLI's own argument parser. The default `job`
tool uses that on purpose: its `kind` enum leaves out `shell`, because
`start` over MCP takes no command and a shell row would never close. A
shell job goes through `run` instead, which takes the command.

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
description = "The install's list of work in flight: list items, add one, move one to a new state, add a dated note, read an item's history."

[tools.tracker.properties]
title = { type = "string" }
domain = { type = "string", optional = true, description = "grouping key, e.g. infra" }
tag = { type = "array", items = "string", optional = true }
id = { type = "integer", description = "item id" }
state = { type = "string", enum = ["open", "active", "blocked", "done", "dropped"] }
add_note = { type = "string", optional = true, description = "a status line appended to the notes, dated and signed (update)" }
history = { type = "boolean", optional = true, description = "also every change to the item, oldest first (show)" }

[tools.tracker.commands.list]
argv = ["list", "--json"]
options = { domain = "--domain", state = "--state" }

[tools.tracker.commands.add]
argv = ["add", "{title}", "--json"]
options = { domain = "--domain", tag = "--tag" }

[tools.tracker.commands.state]
argv = ["state", "{id}", "{state}", "--json"]

[tools.tracker.commands.update]
argv = ["update", "{id}"]
options = { state = "--state", add_note = "--add-note" }

[tools.tracker.commands.show]
argv = ["show", "{id}"]
options = { history = "--history" }
```

The `update` command above offers `--add-note` and not `--notes` on
purpose: `--notes` replaces the whole text, so a cousin posting a status
line with it erases the spec and the earlier lines another wrote. The
old text is kept in the item's history either way, but `--add-note` is
the one to hand a cousin.

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
