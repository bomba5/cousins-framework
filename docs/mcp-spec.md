# MCP adapter specification

The cousin's CLI surface offered over the Model Context Protocol, and
unconfigured it does not exist: no process starts, no tool is listed,
nothing changes for a cousin whose home carries no registration. Every
tool it offers is one of the framework's own CLIs, run the way the
cousin already runs it - as the cousin, in the cousin's home, with the
cousin's environment - minus the shell. So the perimeter first:

- **What it is.** One process per cousin, speaking MCP over stdio,
  started by that cousin's own agent harness and dying with the
  session. It is not a service: nothing supervises it, nothing listens
  on a port, no other cousin can reach it.
- **Whose identity.** The process inherits the launch environment
  (`COUSIN_HOME`, `COUSIN_SLUG`, `FRAMEWORK_ROOT`) and has exactly the
  reach of the cousin's shell, no more and no less. Per-cousin identity
  in this framework is a convention carried by environment and home
  directory, not an operating-system boundary, and nothing here should
  be read as a security property the CLIs did not already have.
- **What it replaces.** The outbound and query half of the surface:
  what a cousin does (send, remember, track, schedule). It adds no
  inbound path; messages still arrive as text the chat server injects
  into the terminal. MCP is pull-only and this framework does not
  pretend otherwise.
- **What it does not buy.** New capability. Every tool is a CLI that
  exists, and the tool count is bounded by choosing coarse tools, not
  by leaving CLIs out.

## Why it exists

Three failures of the CLI-through-a-shell path, each structural:

1. **The shell mangles free text before argv exists.** A decision
   body, a reply or a prompt carrying backticks, `$(...)`, an
   unbalanced quote or a newline is expanded or split by the calling
   shell before the CLI sees it. Over MCP, arguments arrive as JSON and
   are handed to the CLI as an argv list. There is no shell on the path.
2. **Hand-written tool tables rot.** A tool list generated from a
   registry cannot drift from the registry.
3. **The wrong delivery path fails silently.** Replying to an operator
   and messaging a peer are two CLIs with different servers; the wrong
   one posts into the wrong place and reports success. One `send` tool
   resolves the destination server-side and never falls through.

## Shape

### The registry is configuration; the adapter is generic

The adapter has no built-in tool list. It reads a TOML registry naming
each tool, the CLI it invokes, the subcommands it exposes, and how each
JSON argument becomes argv or stdin. The framework ships a default at
`config/mcp-registry.toml.example`; an install may copy it to
`config/mcp-registry.toml` and edit that; every cousin gets its own
copy at `<home>/mcp-registry.toml` at spawn. Which CLIs are cousin
surface, operator class, or never-a-tool is a property of the
deployment, and nothing in the code encodes it.

Top-level keys: `ceiling` (default 12; more enabled tools is a
registry error, so nobody turns six coarse tools into one per
subcommand unnoticed), `timeout` (seconds per call, default 120),
`max_output` (characters of stdout returned per call, default 16000;
the rest is cut and the cut is stated in the result).

Per tool, `[tools.<name>]`:

- `command`: a console-script NAME or an absolute path. A bare name
  resolves to the directory of the running interpreter first, then to
  PATH. Beside-the-interpreter first is what makes two installs on one
  machine unambiguous: the CLIs of the install this `cousin-mcp`
  belongs to win over whatever is first on PATH. A name that resolves
  nowhere fails at call time as a tool error naming both places it
  looked, and `--selftest` fails on it up front.
- `description`, `enabled` (default true; a disabled tool is neither
  listed nor mentioned), `kind` (`command`, `send`, `job`).
- `[tools.<name>.properties]`: `<prop> = { type, optional, enum, items,
  description }`. Types `string`, `integer`, `number`, `boolean`,
  `array` map to JSON Schema; anything else is declared as a string.
- `[tools.<name>.commands.<cmd>]`: `argv` (elements are literals or
  exactly `{prop}`; a partial placeholder, a non-string element, or a
  placeholder naming no property is a registry error), `options`
  (`prop = "--flag"`: `true` emits the flag alone, a list repeats it, a
  scalar follows it), `stdin` (properties joined in order with
  `stdin_sep` between them, a trailing newline when a separator is
  used, verbatim when one property is named alone), optional
  `command` to override the tool's.

`kind = "send"` needs `commands.peer` and `commands.operator`, an
`operators` list (names that reach the operator path), `peers_from`
(the argv that lists peers, default `["cousin-chat", "list"]`; the
first token of each line is a slug, the line marked `(self)` is
dropped) and `errors_name_peers` (default true; false makes an
unknown-destination error give a COUNT of known peers, for an install
whose peer list carries a name a caller must not learn).

`kind = "job"` needs `commands.gen`, a property `id`, and optionally
`job_command` (default `cousin-job`). `gen` starts the command as a
tracked shell job and returns `{"job_id", "log_path"}`; `status`
returns the tracker's row; `result` returns the last twenty log lines
once the job is done, is a state (not an error) while it runs, and is
an error carrying the log tail when it failed. A handle that does not
resolve is never returned: when the tracker registers no id, `gen`
refuses and names the command to run from a shell. Inline image or
audio content on `result` is opt-in per call (`return_image`).

Validation errors are `RegistryError`; nothing is served.

### The default registry

| tool | CLI | commands |
|---|---|---|
| `memory` | `cousin-memory` | `search`, `decide` (via `--stdin` with `---` separators), `recall`, `activity` |
| `send` | `cousin-chat send` / `cousin-reply` | resolved by destination; see below |
| `job` | `cousin-job` | `start`, `done`, `fail`, `list`, `show` |
| `schedule` | `cousin-schedule` | `add`, `list`, `cancel` |

Operator-class verbs (spawn, flip, reincarnate, transplant, loop
control, shared-tier review) are absent and stay absent. A CLI that
runs a shell on its input is never registered: it would put caller
text one hop from a live shell.

### `send` resolves explicitly and never falls through

A destination that is a known peer slug (discovered once at process
start through `peers_from`) goes to `cousin-chat send`; a destination
that is a configured operator name goes to `cousin-reply --user` with
the body on stdin; anything else is a tool error naming what the
cousin knows. There is no default. A typo in a slug fails loudly
rather than posting into the wrong thread.

The shipped default configures no operator (`operators = []`); until
the cousin's registry names one, `send` reaches peers only, and the
error in that state says *no operator is configured for this cousin*
before it lists the peers it knows. `cousin-spawn --operator <name>`
fills the line at spawn; for an existing cousin, edit the one line in
`<home>/mcp-registry.toml`.

### Gating is per session start

The registry is read once when the process starts, and a harness reads
`tools/list` once at connect. A tool enabled mid-session appears at the
next session. No hot registration is promised.

## The call path

- **Argv is a list, never a string.** For every call the adapter builds
  a Python list and runs it with `subprocess.run(list)`, no shell. A
  test proves it with hostile content: backticks, `$(...)`, a newline
  and an unbalanced quote reach the CLI's argv and stdin byte-identical.
  A test that passes only clean input proves nothing.
- **Free text goes through stdin where the CLI has a stdin path**
  (`cousin-reply`, `cousin-memory decide --stdin`). Where it does not
  (`cousin-chat send`), the body travels as one argv element; both
  routes are shell-free.
- **The schema is advisory; the CLI's parser is the enforcement
  point.** `inputSchema` exists so the model chooses well. Two guards
  keep a registry honest at test time: every flag it declares must
  appear in that CLI's `--help`, and the tool and the CLI produce the
  same effect on the same home (parity, for `decide`, `search`, `job
  start`). One exception: a declared `enum` is enforced by the adapter
  before anything runs, because a registry leaves a value out of an
  enum on purpose (the default registry's `job start` has no `shell`
  kind: it takes no command, so a shell row would never close). The
  refusal names the allowed values and the property's description.
- **Results** carry the CLI's stdout as text, capped at `max_output`
  with the cut stated. A non-zero exit is a tool execution error
  (`isError`) carrying stderr, never a protocol error. A timeout is an
  error and is not retried. A call with no stdin body gets `/dev/null`,
  never the parent's stdin.
- **Client version record.** On every connect the server records the
  protocol version the client declared, to stderr and to
  `<home>/data/mcp-client.json` (a map version -> last seen, one entry
  per distinct version). When the client moves to a revision the
  installed SDK does not speak, that file is the evidence; `cousin-mcp
  --versions` prints what the SDK speaks.

## Protocol

The adapter speaks MCP through the reference Python SDK, installed as
the extra `cousin-framework[mcp]` (the 1.x SDK; the 2.x SDK changed the
server API and the extra excludes it). The default install pulls nothing;
that is what the zero-dependency policy is for. The SDK is imported
inside `serve()` only: registry parsing, schema building, argv
assembly, provisioning and approval import nothing but the standard
library and are tested without the extra. Serving without the SDK
exits 2 with the one line that fixes it: `pip install
"cousin-framework[mcp]"`. There is no alternative launcher: the SDK
lives in the same interpreter as the framework, so a cold cache or a
pruned tool store cannot make the tool surface vanish at some later
session start.

## Provisioning

`cousin-spawn` writes two files into every new home, each only if
absent (`docs/spawn-and-template-spec.md`):

- `<home>/mcp-registry.toml`: the install's default registry with
  `operators` filled from `--operator` (the same name goes to
  `cousin.toml [operator]`).
- `<home>/.mcp.json`: `{"mcpServers": {"cousin": {"type": "stdio",
  "command": "<bin>/cousin-mcp", "args": ["--registry",
  "<home>/mcp-registry.toml"], "env": {"COUSIN_HOME", "COUSIN_SLUG",
  "FRAMEWORK_ROOT"}}}}`. The env block carries the three variables the
  registered CLIs read; it is what makes the process a cousin.
  `<bin>` is the directory of the interpreter that ran spawn when a
  `cousin-mcp` sits there, so the registration runs the adapter of the
  install that wrote it; otherwise the bare name. PATH is never
  consulted at write time: on a machine with two installs, the first
  one on PATH is the one that must not be baked in.

A `.mcp.json` is a registration the harness has not necessarily
accepted. Approval is per project path in the harness's own settings
file, as `"cousin"` in that project's `enabledMcpjsonServers` with the
folder trusted. `cousin-mcp approve <slug>` records it: it edits
exactly the file `config/harness.toml settings_file` names (adds the
home under `projects` with `hasTrustDialogAccepted: true`, appends
`"cousin"` to `enabledMcpjsonServers`, removes it from
`disabledMcpjsonServers`, keeps everything else), and refuses with a
remediation when the seam is absent, when the named file does not
exist, when it is not JSON, when the slug is unknown, or when the home
has no `.mcp.json`. It never touches a file it was not pointed at. An
install that keeps its harness settings elsewhere makes the same edit
by hand; the refusal says what to add.

Rollout order: one cousin, then a second, then every spawn. The suite
(hostile argv, parity, flags-in-help, ceiling, provisioning, approval)
is green before the template carries it.

## Verifying it

```
cousin-mcp --registry config/mcp-registry.toml.example --selftest
cousin-mcp --list-tools                     # schemas as JSON, no SDK needed
cousin-mcp --call memory '{"command":"search","query":"x"}'
cousin-mcp --versions                       # what the installed SDK speaks
```

`--selftest` loads the registry, builds every schema, prints each tool
with its commands and where each command resolves (beside the
interpreter, on PATH with the path, or NOT FOUND), says whether the SDK
is present, and exits 1 when any command resolves nowhere: a tool list
whose commands cannot run is not ok.

## Limits, stated

- Inbound delivery is unchanged; the terminal injector stays the one
  site, which is what keeps a flip and the boot packet coherent.
- No hot registration.
- No operator scope. It would need a gate the cousin cannot write, and
  on a host where every cousin runs as one user there is no such place
  inside a home.
- The registry is trusted configuration, like `cousin.toml`. A cousin
  that can edit its own registry can register any CLI it can name; the
  gate against operator-class verbs is that they are not in the default
  and not in the template, not that the adapter refuses them.
- Approval is harness-specific. The `settings_file` seam and the edit
  `approve` makes match one harness's convention (a per-project entry
  with `enabledMcpjsonServers`); another harness needs its own step,
  and the framework says so instead of guessing.
