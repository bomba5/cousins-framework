# Runners reference

What a runner is, the kinds the framework ships, how to pick one, and what
each kind does with every item of the runner contract. For the keys, read
[configuration](../configuration.md); for `cousin-runner` and its exit codes,
[commands](../commands.md).

## What a runner is

A runner is the process that drives a cousin's agent loop in place of a tmux
pane. `cousin-runner --home <home>` (started for you by `cousin-supervisor`)
builds the runner `[agent] runner` in `cousin.toml` names, takes one lock per
cousin, and serves the cousin's inbox (`data/inbox.db`): every chat message,
peer message, loop, schedule, flip and interrupt is a row there, and the
runner claims rows in priority order, runs a turn for them, and closes each
row `delivered` or `failed`. What happens in a turn goes to the cousin's event
stream (`data/stream/`), which the console's pane and `cousin-watch` read. The
framework's tools (`reply`, `handoff`, memory, jobs, ...) run inside the
runner's process against the live turn, whatever the kind.

Every kind implements one protocol, `cousin_lib/runner/base.py` `Runner`
(`start`, `stop`, `state`, `enqueue`, `interrupt`, `rollover`, `events`,
`unsupported`), and one contract suite, `tests/runner/contract/suite.py`,
runs against each. A runner may DECLARE an item it cannot meet in
`unsupported()`; the suite then skips exactly that item's test, the runner
writes the list into the `runner` event at start, and the console shows it on
the cousin's card and its chat header. Nothing is skipped silently.

## The kinds

| kind | agent loop | account kinds | what it is for |
|---|---|---|---|
| `sdk` | the Claude Agent SDK (its bundled Claude Code CLI), in-process tools and hooks | `claude-login` (and `host`), `claude-token`, `anthropic-key` | the default lane: Claude models, on a login or an API key. Side sessions (`[agent.sessions]`) are this kind only |
| `fake` | none: a scripted turn that answers at once | none needed | tests, demos and the Docker exit checks: the whole lane (inbox, stream, supervisor) with no model |
| `opencode` | `opencode serve`, driven over HTTP and its event stream (`OpencodeRunner`) | `opencode` only | another provider's models on its own API key, or a local OpenAI-compatible model. Never a Claude subscription |

A cousin with no `[agent] runner` is a tmux cousin: it has no runner at all.

## Picking one

- On Claude models, `runner = "sdk"`, on the account you want (`host`, a
  named login, a token or an API key; see [accounts.toml](../configuration.md#accountstoml)).
- On any other provider, or on a model you serve yourself, `runner =
  "opencode"` with a `kind = "opencode"` account (`providers` or `endpoint`)
  and `[agent] model = "<provider>/<model>"`, which is required. The keys are
  in [the opencode lane](../configuration.md#agent-on-the-opencode-lane). In
  Docker, run the image's opencode variant (`compose.opencode.yml`, see
  [install](../install.md)); on a bare host, put the `opencode` binary on
  `PATH` or name it in `[agent] opencode_bin`.
- `runner = "fake"` only to exercise the lane itself.

The lanes do not mix: an `opencode` cousin on a Claude account, or an `sdk`
or `fake` cousin on an `opencode` account, is refused at start (exit 2), and
so is an `opencode` or `fake` cousin with `[agent.sessions]` mapping a kind
to `"own"`.

**The opencode lane never carries Claude subscription traffic.** No
`claude-login` or `claude-token` account runs on it, an Anthropic OAuth login
in its `auth.json` refuses the start, and `cousin-account login` refuses an
Anthropic OAuth method. The bridge guard (`cousin_lib/runner/opencode_guard.py`)
refuses, before anything starts, a config or an environment that would route
the lane through a Claude-subscription bridge: the bridge's plugin, package,
proxy and header names, a base URL on the bridge proxy's port 3456, and an
Anthropic provider (or `ANTHROPIC_BASE_URL`) pointed at a loopback address.
Removing a bridge a live install still carries is an operator step:
[migrating](../migrating.md).

**Another vendor's subscription is the user's risk (ruling P9-2).** An
opencode account may hold another vendor's OAuth login (`cousin-account
login <name> --provider <id> --method <label>`), which runs that
subscription through a client its vendor did not write. The terms risk of
that is the user's, as it is for the login lane on the SDK; an API key or a
local model carries none.

**No Claude model on this lane (ruling P9-1).** Claude cousins run on the
Agent SDK and nowhere else, so an opencode account that names the
`anthropic` provider, and an `endpoint_model`, `[agent] model` or
`small_model` whose id contains `claude` or `anthropic` (any case), are
refused at start (exit 2), and `cousin-account login` refuses the
`anthropic` provider by key as well as by OAuth. The test is by name: it
stops an honest mistake and a proxy that names its model after Claude, not
an OpenAI-compatible proxy that serves Claude under another id. What a local
endpoint really serves is the operator's to know; the bridge guard and this
test are a floor, not a proof.

## The contract table

Generated from each runner class's declarations by
`python3 -m cousin_lib.runner.contract_table --write`; a test fails when this
page is stale. IMPLEMENTED: the runner's own code meets the item. PLUGIN: a
plugin the runner loads meets it (`plugin_items()`). DECLARED: the runner
declares it unsupported (`unsupported()`). The suite runs every item that is
not DECLARED against every kind (`tests/runner/contract/test_<kind>.py`; the
`opencode` column against a fake `opencode serve`, and
`tests/runner/test_opencode_live.py` proves the fake against the real binary),
so an IMPLEMENTED or PLUGIN cell is one the suite enforces.

<!-- contract-table:begin (python3 -m cousin_lib.runner.contract_table --write) -->
| item | what the suite proves | `sdk` | `fake` | `opencode` |
|---|---|---|---|---|
| `enqueue_receipt` | `enqueue` answers a `Receipt` with an inbox id, outcome `queued` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `priority_order` | queued rows run in priority order: operator, then peer, then loop | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `consume_after_start` | a row put while the runner is stopped runs after `start()` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `interrupt_ends_turn` | `interrupt()` ends the running turn; the runner is idle again | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `turn_events` | every turn emits `turn_start`, then `tool`, then `result` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `unsupported_list` | `unsupported()` names contract items only | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `midturn_fold` | an operator message put mid-turn is closed by the same `result` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `outcome_delivered` | a finished turn closes its row `delivered` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `outcome_failed` | a failed turn closes its row `failed`, the result an error | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `outcome_interrupted` | an interrupted turn's row is `delivered` (the model had it) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `failure_recovers` | after a failure (`errored`, then `idle`) the next row runs | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `stop_ends_turn` | `stop()` during a turn ends it within its timeout | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `peer_waits` | a peer message put mid-turn waits for a turn of its own | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `state_events` | every state transition is a `state` event, in order | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `events_after` | `events(after=n)` resumes exactly after event `n` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `interrupt_idle_false` | `interrupt()` with no turn running answers False | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `enqueue_type_error` | `enqueue` refuses anything but an `Item` (TypeError) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `rollover_shape` | `rollover()` answers `{ok, reason}` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `rollover_generation` | a rollover moves the generation and loses no row | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `interrupt_row` | an `interrupt` inbox row ends the live turn and is `delivered` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `interrupt_row_idle` | an `interrupt` row with no turn running is `failed`, never a turn | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
<!-- contract-table:end -->

`midturn_fold` on `opencode` is measured, not assumed: a prompt sent while a
run is busy is folded into that run and answered before its one
`session.idle`. The opencode plugin pack (`plugins/opencode/`) meets no
contract item: it is the policy veto (`policy.toml` enforced before a tool
runs), which the contract does not cover.

## Lane differences on opencode

- The tools are the same registry, served to opencode as a remote MCP server
  the runner itself runs on loopback; the model sees them as `cousin_<tool>`
  (the SDK lane's `mcp__cousin__<tool>`), and the contract in the system
  prompt names them that way.
- The composed system prompt is appended to opencode's own agent prompt,
  which the model also sees.
- `policy.toml` is enforced by the plugin pack in opencode's
  `tool.execute.before`, with tool names mapped to the SDK lane's; the runner
  refuses to run turns until the plugin has acknowledged this start's policy
  file (see [policy.toml on the opencode lane](../configuration.md#policytoml-on-the-opencode-lane)).
- Tool calls (with their arguments), subagent jobs and checkpoints are
  recorded by the runner from opencode's event stream, not by hooks.

## Known gaps

What the opencode lane does not do yet. Each is stated, none is hidden, and
none is a contract item:

- **No usage records, transcript mining, memory proposals or review gate.**
  `usage.db`, `extract.mine_turn`, the per-turn memory proposal and the
  review gate's per-turn check read or run beside the SDK's session store;
  opencode keeps its transcript in its own database in the account's data
  dir. A turn's `result` event still carries opencode's token counts and cost.
- **Image attachments are sent as names** (`[image: <name>]` in the text),
  not as image parts.
- **`cousin-runner --check-auth --validate` is refused** for an `opencode`
  account (exit 2); `--check-auth` alone reports whether `auth.json` holds
  every provider the account names.
- **`apply_patch` is not mapped to `Edit` or `Write`.** The plugin maps
  opencode's tool names to the SDK lane's before matching `policy.toml`, but
  `apply_patch` (opencode's own file-editing tool) has no SDK name, so
  denying `Edit` and `Write` does not deny it: deny `apply_patch` by that
  name too.
- **A subagent's own tool calls are vetoed but not recorded** (they run in a
  child session the runner does not read), and the SDK lane's rule that a
  subagent's `reply` must name its thread is not enforced.
- **No side sessions** (`[agent.sessions]` with `"own"` is refused).
- **No `rate_limited` state:** opencode retries a rate-limited provider
  itself; its retry shows as a `system` event.
- **A local endpoint model has no context limit** unless its account sets
  `endpoint_context`: without it neither the runner's context-pressure
  rollover nor opencode's own compaction happens, only the daily cadence.
- **`cousin-spawn --runner opencode` writes no `[agent] model`** and does not
  check the account's lane when the cousin is created; the runner refuses the
  start (exit 2) until both are right.
- **Log in while the cousin is stopped.** `cousin-account login --provider`
  writes `auth.json` atomically but takes no lock against a running
  `opencode serve` refreshing an OAuth token in the same file.
- **Only x86-64 is pinned** in the image's opencode variant.
- **The model's shell keeps part of the server's environment.** opencode
  merges the plugin's `shell.env` answer over its own environment, so a
  variable can be overridden (the plugin sets `HOME` to the cousin's home and
  empties the XDG variables, the server's password and its config path) but
  not removed: the shell still sees opencode's `OPENCODE_*` switches and
  `COUSIN_POLICY_FILE` (neither a secret). Processes opencode starts other
  than a shell (language servers, formatters) run with the server's own
  environment, `HOME` in the account's data dir.
