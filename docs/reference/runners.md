# Runners reference

What a [runner](../glossary.md#runner) is, the kinds the framework ships, how to pick one, and what
each kind does with every item of the runner contract. For the keys, read
[configuration](../configuration.md); for `cousin-runner` and its exit codes,
[commands](../commands.md).

## What a runner is

A runner is the process that drives a [cousin](../glossary.md#cousin)'s agent loop.
`cousin-runner --home <home>` (started for you by `cousin-supervisor`)
builds the runner `[agent] runner` in `cousin.toml` names, takes one lock per
cousin, and serves the cousin's [inbox](../glossary.md#inbox) (`data/inbox.db`): every chat message,
peer message, loop, schedule, [flip](../glossary.md#flip) and interrupt is a row there, and the
runner claims rows in priority order, runs a [turn](../glossary.md#turn) for them, and closes each
row `delivered` or `failed`. What happens in a turn goes to the cousin's event
[stream](../glossary.md#stream) (`data/stream/`), which the console's pane and `cousin-watch` read. The
framework's tools (`reply`, `handoff`, memory, jobs, ...) run inside the
runner's process against the live turn, whatever the kind.

## States and outcomes

A runner is always in one of seven states. Every change is a `state` event
in the cousin's stream, which the console shows.

| state | what it means for you | can move to |
|---|---|---|
| `idle` | no turn is running; the next row is claimed at once | `running`, `rolling_over`, `errored`, `stopped` |
| `running` | a turn is running | `idle`, `waiting_permission`, `rate_limited`, `rolling_over`, `errored`, `stopped` |
| `waiting_permission` | the agent CLI asked for a permission (`sdk` kind); the turn goes on, and the state is `running` again at the CLI's next message. A `policy.toml` `ask` passes through it and is enforced as a deny | `running`, `idle`, `errored`, `stopped` |
| `rate_limited` | the account hit its usage limit; the runner claims nothing until the limit's reset time, then goes `idle`. Queued rows wait, none is lost (`sdk` and `tmux`; opencode retries on its own) | `running`, `idle`, `errored`, `stopped` |
| `rolling_over` | the cousin is handing off to a new session (context pressure, the daily flip or an explicit flip); rows wait for it | `idle`, `errored`, `stopped` |
| `errored` | a turn or the connection failed. After a turn's failure the runner recovers to `idle` and runs the next row. When the account needs a login it stays `errored` and waits for one (`cousin-chat list` says LOGIN REQUIRED); when it cannot connect, `cousin-runner` exits and the [supervisor](../glossary.md#supervisor) restarts it | `idle`, `stopped` |
| `stopped` | the runner was stopped; nothing leaves this state, a start is a new runner | none |

Anything that reaches a cousin (chat, a reaction, a loop, a schedule, a
meeting line, a flip) is handed to its runner's inbox and answered with one
of three outcomes:

- `delivered`: the runner ran the row and closed it delivered.
- `queued`: the row is stored and waits for the runner. A runner that does
  not answer in time (busy, stopped, restarting) is `queued`, never
  `failed`: the row is durable, and a sender that retried would deliver it
  twice.
- `failed`: nothing was kept (the inbox could not be written), the cousin
  has no runner (it is refused, with the reason), or the runner ran the row
  and the turn failed.

At a turn boundary the runner claims the queued row with the lowest rank,
oldest first within a rank:

| rank | rows |
|---|---|
| 0 | a flip (the handoff) and an interrupt |
| 1 | chat from an operator or a person, a reaction, a chat hook, a boot row |
| 2 | a meeting line |
| 3 | chat from a peer |
| 4 | a schedule |
| 5 | a loop |
| 6 | a memory proposal |

An interrupt row claimed with no turn running is closed `failed` and never
runs as a turn.

The system prompt is composed, never copied, and never truncated: the
framework law, the framework contract (generated from the tool registry, so
it names exactly the tools the cousin has), the cousin's authored identity
(the authored parts of its `CLAUDE.md` and its committed self-portrait), and
the operator rules every cousin shares. The `sdk` kind passes
`setting_sources=[]`, so the agent CLI inherits no settings file: what the
cousin has is what the runner passes. The prompt holds nothing volatile (no
clock, counter or generation; those go in the first message's state digest)
and stays byte-identical across sessions, so a new session reads it from the
prompt cache. For the same reason a cousin's working directory, its home,
never changes.

## What folds into a running turn

A row that arrives while a turn runs is either folded into that turn (written
into it at once and closed by the same result) or kept for a turn of its own.
The rule is `base.FOLDED_KINDS`, the same on every runner that [folds](../glossary.md#fold) (`sdk`,
`opencode`, `fake`). The `tmux` kind folds nothing (`midturn_fold` is
DECLARED): the pane's CLI queues typed input to the turn's end or interrupts,
never folds, so every row, an operator's or a peer's included, is claimed at
idle and waits for the running turn to end. On that kind the operator's
urgent path is the interrupt, and a peer's STOP arrives after the turn it
meant to stop.

| row | while a turn runs |
|---|---|
| chat on an `operator:`, `person:` or `peer:` [thread](../glossary.md#thread) | folded into the running turn |
| a meeting line | its own turn: it is the cousin's turn in a round, answered once |
| a loop or a schedule | its own turn: the cousin's own timers, nobody waits on them |
| a flip, an interrupt, a reaction, a hook or a boot row | never folded: a flip is the handoff turn, an interrupt has its own path |

A peer folds like an operator: a turn has no length bound, and a peer's
message (a coordinator's STOP) that waited for the turn to end would arrive
after the work it meant to stop. The model knows who it answers from the
envelope header, `[peer:<slug>] chat from <Name> at ...`. `reply` never
answers a peer thread: named, it is refused with the hint to use `send`. A
peer folded into an operator's turn makes two live threads, and a `reply`
that names no thread is refused, never guessed; the refusal says which thread
takes `thread=` and which peer takes `send`. Folding changes nothing in the
claim order at a turn boundary: operator and person chat first, then a
meeting, then a peer. A peer row already queued when a meeting, loop or
memory-proposal turn starts folds into that turn, as operator and person rows
do.

On the `sdk` [lane](../glossary.md#lane) a fold is never written by the reader of the turn: each
turn has one writer task, and the fold's write (and an interrupt row's
control write) is handed to it in order while the reader goes on reading the
CLI's output. A reader that waited on the write could deadlock the turn: a
CLI whose output is full and unread stops reading its input. The turn's first
row is still written before anything is read, while the CLI is idle.

Every kind implements one protocol, `cousin_lib/runner/base.py` `Runner`
(`start`, `stop`, `state`, `enqueue`, `interrupt`, `rollover`, `events`,
`unsupported`), and one contract suite, `tests/runner/contract/suite.py`,
runs against each. A runner may DECLARE an item it cannot meet in
`unsupported()`; the suite then skips exactly that item's test, the runner
writes the list into the `runner` event at start, and the console shows it on
the cousin's card and its chat header. Nothing is skipped silently.

## A turn cut by a restart

A stop or a death can cut a turn in the middle. What happens to that turn's
message depends on which:

- **A death.** The next start's sweep finds the rows the dead runner had
  claimed and puts them back in the inbox, so the message is delivered
  again.
- **A stop.** The runner interrupts the turn, and an interrupted turn's rows
  are closed as delivered, so the message is not delivered again.

Either way the runner marks `data/runner-restart.json` (with `requeued:
true` after a death's sweep), and the next session's first line, on the
`system` thread from `runner`, says what happened. A resumed session is told
the runner restarted, or names the requested stop or restart that cut the
turn, and to continue where it was. A fresh session (the saved one could not
be resumed) has nothing of the cut turn in its transcript; the `sdk` kind
gives it a line only when that turn ran tools.

So that a restart does not run a push or a send twice, the `sdk` kind's
primary session keeps the live turn's tool calls in `data/turn-tools.jsonl`.
A turn's start rewrites the file with the turn's first inbox row id and its
first message (cut to 600 characters). Then each call adds a line when it
starts and one when its result comes. Read-only tools (`Read`, `Grep`,
`Glob`, `LS`, `WebFetch`, `WebSearch`, `TodoWrite`, `ToolSearch`,
`BashOutput`, `NotebookRead`) are left out. The file is never emptied at a
turn's result, since an interrupted turn ends in one too; the next start
empties it after using it.

The next start appends those calls to its line: the last 12, with how many
came before, each cut to 160 characters. A call is `finished`, `failed`, or
`STARTED, NO RESULT: it may or may not have happened` when it was cut in
flight. After a death, a fresh session gets:

```
[runner] a restart cut your last turn before this session began (at 2026-10-02T12:30:05+00:00: the last runner died with a row claimed). This is a new session, so you cannot see that turn.

Before the cut, that turn had already run these tool calls (most recent last). The message it was answering is delivered again after this line: do not repeat what already ran, and check the state of anything marked NO RESULT before running it again.
- Bash `git push origin docs/notes` (finished)
- mcp__cousin__send `The notes branch is up.` (STARTED, NO RESULT: it may or may not have happened)
```

After a stop, the list says instead that the message is not delivered
again, to continue its work if still due without repeating what already
ran, and quotes it: `The message was: "Push the notes branch and tell ana it
is up."`. After a death whose recorded turn has every row closed, the file
belongs to an older, finished turn (the runner died between claiming a row
and starting its turn), and no list is given.

The stream's `system` event with subtype `restart_note` says whether the
session was resumed and how many calls were recorded. Side sessions keep no
list and get no line. The `tmux` kind writes the same mark and has its own
restart line, with no list of calls; the `opencode` kind gives no line.

## The kinds

| kind | agent loop | account kinds | what it is for |
|---|---|---|---|
| `sdk` | the Claude Agent SDK (its bundled Claude Code CLI), in-process tools and hooks | `claude-login` (and `host`), `claude-token`, `anthropic-key` | the default lane: Claude models, on a login or an API key. Side sessions (`[agent.sessions]`) are this kind only |
| `fake` | none: a scripted turn that answers at once | none needed | tests, demos and the Docker exit checks: the whole lane (inbox, stream, [supervisor](../glossary.md#supervisor)) with no model |
| `opencode` | `opencode serve`, driven over HTTP and its event stream (`OpencodeRunner`) | `opencode` only | another provider's models on its own API key, or a local OpenAI-compatible model. Never a Claude subscription |
| `tmux` | the host's interactive Claude Code CLI in a tmux pane on the framework's own socket (`run/tmux.sock`), driven by `TmuxRunner`; the CLI's transcript is the source of truth, its hooks only wake the runner | `claude-login` (and `host`) only | the fallback if Agent SDK usage moves off subscription limits. It cannot fold a message into a running turn (`midturn_fold`) |

`sdk` and `tmux` on a `claude-login` or `claude-token` account run on a Claude
subscription, which Anthropic's terms do not clearly allow for a product like
this one: read [Claude logins and Anthropic's terms](../terms-risk.md) before
you use them that way.

A cousin moves between `sdk` and `tmux` with `cousin-migrate --to <kind>`,
keeping its session (`data/runner-session.json`, resumed with `claude
--resume`). The source stops held, claiming nothing new once the stop is
asked for. The switch's notice (a `system` `boot` row telling the model its
new kind) is queued before the target starts and ranked ahead of every row,
so it is the first turn after the switch, before any row queued earlier; a
rollback drops it if nobody took it.

No step is needed before `--to tmux`. Whether the account's CLI has trusted
the cousin's home is not known in advance: `~/.claude.json` (or the account's
config dir's) is rewritten by every live CLI, and CLI 2.1.282 recorded no
`hasTrustDialogAccepted` entry for a home even after the dialog was accepted
by hand. So the plan reports the trust line as `ok` ("not known in advance:
the pane asks once"), or says the entry is recorded when it is. On the first
start the pane may show the trust dialog (or the bypass one). The runner
types nothing into it: it writes `data/login-required.json` (`{"kind":
"tmux", "screen": "trust"}`) and emits an `auth` `login_required` event. The
switch's verify sees that, neither fails nor rolls back, prints that it is
waiting for the operator to accept the dialog, with the command (`tmux -S
<root>/run/tmux.sock attach -t tmux-<slug>`, or the console's pane view where
it shows that session), and waits up to 10 minutes (`TRUST_WAIT_S`). It
completes at the first turn start after the acceptance (the switch's notice).
If nobody accepts in time, verify fails as before, naming the dialog, and the
error says how to roll back. A rollback clears the tmux runner's
`data/login-required.json`, so the restored kind does not read LOGIN
REQUIRED. If the operator accepts after verify gave up, the target runs on
and takes the notice: the switch did complete. Nothing watches for that,
so `data/kind-switch.json` keeps `failed` until it is next read:
`cousin-migrate check <slug>` (it prints `kind switch: ... switched` and
a `late` note) or a rollback, which then rolls back a switch.

A cousin with no `[agent] runner` has no runner and is refused: nothing is
delivered to it and it does not start, and the refusal names the reason.

### The system prompt is a private file

The runner composes the cousin's system prompt (the law, the generated
contract, the authored identity and self-portrait, the operator rules;
`runner/prompt.py`) and never puts it on a command line or in an
environment variable: an argv is readable by every local user (`ps`,
`/proc/<pid>/cmdline`). The text is written to a file only the cousin's user
can read (mode 0600, in `data/run/`, mode 0700), replaced atomically before
every start, reconnect and [rollover](../glossary.md#rollover), and the CLI gets the path:

| kind | file | how the agent gets it |
|---|---|---|
| `sdk` | `data/run/system-prompt.md` | `--append-system-prompt-file`, beside the `claude_code` preset |
| `tmux` | `data/run/tmux-context.md` | `--append-system-prompt-file` on a fresh start, from the launcher |
| `opencode` | none | the `system` field of each prompt, over the loopback HTTP API with the server's password |

A changed identity reaches the next fresh session; a resumed SDK session
keeps the prompt it first recorded (the preset's `snapshot`).

## Picking one

- On Claude models, `runner = "sdk"`, on the account you want (`host`, a
  named login, a token or an API key; see [accounts.toml](../configuration.md#accountstoml)).
- On any other provider, or on a model you serve yourself, `runner =
  "opencode"` with a `kind = "opencode"` account (`providers` or `endpoint`)
  and `[agent] model = "<provider>/<model>"`, which is required. The keys are
  in [the opencode lane](../configuration.md#agent-on-the-opencode-lane). In
  Docker, the default image carries the `opencode` binary (the slim image,
  `compose.slim.yml`, does not; see [install](../install.md)); on a bare host, put the `opencode` binary on
  `PATH` or name it in `[agent] opencode_bin`.
- `runner = "fake"` only to exercise the lane itself.

The lanes do not mix: an `opencode` cousin on a Claude account, or an `sdk`
or `fake` cousin on an `opencode` account, is refused at start (exit 2), and
so is an `opencode`, `fake` or `tmux` cousin with `[agent.sessions]` mapping a
kind to `"own"` (side sessions are the `sdk` kind's).

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

**Another vendor's subscription.** An opencode account may
hold another vendor's OAuth login (`cousin-account login <name> --provider
<id> --method <label>`) as well as API keys or a local model.

**No Claude model on this lane.** Claude cousins run on the
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
| item | what the suite proves | `sdk` | `fake` | `opencode` | `tmux` |
|---|---|---|---|---|---|
| `enqueue_receipt` | `enqueue` answers a `Receipt` with an inbox id, outcome `queued` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `priority_order` | queued rows run in priority order: operator, then schedule, then loop | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `consume_after_start` | a row put while the runner is stopped runs after `start()` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `interrupt_ends_turn` | `interrupt()` ends the running turn; the runner is idle again | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `turn_events` | every turn emits `turn_start`, then `tool`, then `result` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `unsupported_list` | `unsupported()` names contract items only | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `midturn_fold` | an operator or peer message put mid-turn is closed by the same `result` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | DECLARED |
| `outcome_delivered` | a finished turn closes its row `delivered` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `outcome_failed` | a failed turn closes its row `failed`, the result an error | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `outcome_interrupted` | an interrupted turn's row is `delivered` (the model had it) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `failure_recovers` | after a failure (`errored`, then `idle`) the next row runs | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `stop_ends_turn` | `stop()` during a turn ends it within its timeout | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `loop_waits` | a loop row put mid-turn waits for a turn of its own | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `state_events` | every state transition is a `state` event, in order | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `events_after` | `events(after=n)` resumes exactly after event `n` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `interrupt_idle_false` | `interrupt()` with no turn running answers False | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `enqueue_type_error` | `enqueue` refuses anything but an `Item` (TypeError) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `rollover_shape` | `rollover()` answers `{ok, reason}` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `rollover_generation` | a rollover moves the generation and loses no row | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `interrupt_row` | an `interrupt` inbox row ends the live turn and is `delivered` | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| `interrupt_row_idle` | an `interrupt` row with no turn running is `failed`, never a turn | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
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
- There is no reply gate: `[agent] reply_gate` is the SDK runner's `Stop`
  hook (see [configuration](../configuration.md#agent-runner)), so a turn
  here can end on an operator or person thread without a `reply`.
- Usage is recorded in `data/usage.db` with lane `opencode`, one row per
  result, and announced as a `usage` event, as on the SDK lane: the tokens
  the provider reported to opencode, every answer of the turn summed once,
  and the cost opencode reported for them. Reasoning tokens count as output;
  the cost is opencode's figure (0 on a free model) and is shown as an
  estimate.

## Known gaps

What the opencode lane does not do yet. Each is stated, none is hidden, and
none is a contract item:

- **No transcript mining, memory proposals or review gate.**
  `extract.mine_turn`, the per-turn memory proposal and the review gate's
  per-turn check read or run beside the SDK's session store; opencode keeps
  its transcript in its own database in the account's data dir.
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
- **No background task events:** the `task_*` `system` events and the
  pane's bg tasks list are the SDK lane's; a subagent here is a job.
- **No `rate_limited` state:** opencode retries a rate-limited provider
  itself; its retry shows as a `system` event.
- **A local endpoint model has no context limit** unless its account sets
  `endpoint_context`: without it neither the runner's context-pressure
  [rollover](../glossary.md#rollover) nor opencode's own compaction happens, only the daily cadence.
- **`cousin-spawn --runner opencode` has no default model:** `--model
  <provider>/<model>` is required, and so is an `opencode` account
  (`--account`). Spawn checks both, and the model against the account's
  providers, before it writes anything, and refuses (exit 2) without them;
  `--effort` is refused too (this kind reads no effort).
- **Log in while the cousin is stopped.** `cousin-account login --provider`
  writes `auth.json` atomically but takes no lock against a running
  `opencode serve` refreshing an OAuth token in the same file.
- **Only x86-64 is pinned** for the opencode binary in the default image.
- **The guard binds opencode's configuration, not the model's shell.** The
  model's shell runs as the same user as the runner and the server. From it
  the model can read the server's environment (`/proc/$PPID/environ`, the
  server's password among it), call the server's API with that password
  (`PATCH /global/config` adds a plugin or a provider to the running server
  at once, measured on 1.18.31), read and write the account's data dir, and
  read any credential that user can read, another account's Claude login
  included on a bare host. The runner re-checks `auth.json`, the config
  sources and the effective config before every turn and gives up at the
  first mismatch. That catches a change still in place when a turn starts;
  it bounds nothing the model does from its shell: a change made and undone
  inside one turn, a detached process prompting the server between turns, a
  turn kept going by folded messages, and the rollover's turns all pass it.
  The containment is the container: a cousin's shell is the
  container ([operations](../operations.md#the-container)); on a bare host,
  run an opencode cousin as a user that can read nothing it should not.
- **What the model starts is found by a marker it can drop.** A stop, and
  the next start after a hard kill, kill every process carrying the start's
  `COUSIN_OPENCODE_START`, repeating until a pass kills nothing (at most five
  passes). A process escapes if it drops the variable (`env -i`, `exec -c`),
  makes its environment unreadable (`PR_SET_DUMPABLE` 0), or is started by
  another manager on its behalf (`tmux`, `systemd-run`, `at`).
- **`node_modules` in opencode's config dir is checked by name only.** The
  runner allows the directory (its plugin-library seed) and checks the lock's
  root names only that library, but does not look inside `node_modules`;
  opencode loads nothing from it unless a config names a plugin, which the
  effective-config check refuses.
- **`auth.json` is checked at every turn start, not during a turn.** A key
  or login changed without a 401 binds from the next turn, when the per-turn
  check reads the file again.
- **A subagent's events do not reset the turn's idle clock.** A `task`
  subagent runs in a child session whose events the runner drops before it
  notes the turn's last event; a long subagent relies on opencode updating
  the parent's tool part to stay inside the 600 s idle bound. Not measured
  for a subagent that runs longer than that.
- **The server's port is picked before the server binds it** (bind, close,
  hand the number over). A collision fails closed: the server exits or its
  password answers 401, and the runner restarts.
- **The runner's reads from its own server are unbounded** (the event
  stream's frames, a response body); the server is its own loopback child.
  The MCP side caps a request at 4 MB.
- **The model's shell keeps part of the server's environment.** opencode
  merges the plugin's `shell.env` answer over its own environment, so a
  variable can be overridden (the plugin sets `HOME` to the cousin's home and
  empties the XDG variables, the server's password, its config path and
  `COUSIN_POLICY_FILE`) but not removed: the shell still sees opencode's
  `OPENCODE_*` switches (flags, no value to hide) and
  `COUSIN_OPENCODE_START`, the start's marker, which is left on purpose (it
  is how a stop finds what the model started) and is not a secret. Emptying
  a variable does not hide it from a process that reads another's (see
  "The guard binds opencode's configuration" above).
  Processes opencode starts other
  than a shell (language servers, formatters) run with the server's own
  environment, `HOME` in the account's data dir.

## Lane differences on tmux

- The pane's CLI runs with `--dangerously-skip-permissions` (the
  trust and bypass dialogs are the operator's, answered once in the pane
  when it shows them; the runner never answers them), so the harness's own
  permission system is never
  consulted: `can_use_tool` does not fire in the pane, unlike the `sdk` and
  `opencode` lanes. `policy.toml` is enforced instead as static rules baked
  into the pane's own settings at start (`harness_settings.py`), not as a
  live veto.
- Only `deny_tools` reaches the pane, rendered into the CLI's own
  `permissions.deny` (`harness_settings._policy_deny`,
  `apply_project_settings`); see "Known gaps" below for what that leaves out.
- `midturn_fold` is DECLARED (`TmuxRunner.UNSUPPORTED`): the pane's CLI
  queues typed input to the turn's end or interrupts, never folds, so an
  operator's or a peer's message put while a turn runs is claimed only once
  the pane goes idle (see "What folds into a running turn" above).
- The composed system prompt reaches the pane only on a fresh start: the
  launcher passes the CLI `--append-system-prompt-file` with the path of
  `data/run/tmux-context.md` (a private file, see "The system prompt is a
  private file" above); a resume or a kind switch gets a short pointer
  instead, never the full block again (`runner/prompt.py`).
- There is no reply gate: `[agent] reply_gate` is the SDK runner's `Stop`
  hook, so a turn in the pane can end on an operator or person thread
  without a `reply`.

## Known gaps on tmux

What the tmux kind does not do yet, separate from the opencode lane's list
above. Each is stated, none is hidden, and none is a contract item except
`midturn_fold`, which is declared (see the contract table):

- **No plugin tools (2.1.0).** A [plugin](../plugins.md)'s `[mcp]` server
  reaches the `sdk` kind (in the SDK's options) and the `opencode` kind (a
  `local` entry in the rendered config), not the pane: Claude Code in the pane
  reads the home's `.mcp.json` itself, and nothing merges the plugin's server
  into it. The plugin's service and its console tab work as on any kind; the
  console's plugin settings say so on a tmux cousin.

- **`deny_bash_patterns` and `ask` do not reach the pane.** Both need a
  live PreToolUse veto to enforce, and under `--dangerously-skip-permissions`
  the CLI never consults one, so only `deny_tools`, rendered into
  `permissions.deny`, still holds (`harness_settings.py`,
  `_policy_deny`/`apply_project_settings`). A rule `policy.toml` can express
  in bash patterns or `ask` is silently absent on this kind, not enforced a
  different way.
- **`deny_tools`' prefix syntax is not the CLI's own.** `Policy._named`
  treats a name ending in `*` as a prefix match (`policy.py`): `Web*` denies
  any tool whose name starts with `Web`. The CLI's `permissions.deny` rule
  syntax has no such prefix form; an entry like `Web*` is written there
  verbatim and denies nothing.
- **A first line holding a dialog's phrase reads as that dialog.**
  `tmux_pane.attention_in` looks from the input box's top rule down when a
  box shows, so a typed first line that holds one of its needles ("Quick
  safety check", "Select login method", ...) classifies the screen as that
  dialog: the runner stops typing, and the console's pane gate
  (`console/pane.py`) opens to a person's keys, still only its closed key
  set and one Enter per request. The classifier would need to exclude the
  prompt line itself.
- **Attachments are not rendered into the pane.** `TmuxRunner._render`
  builds the typed envelope from a row's body and context only; it never
  reads `row["attachments"]`. The `sdk` and `opencode` lanes turn an
  attachment into an inline image block, a `Read <path>` marker for an
  oversized image, or a `[attachment: <name>]` line (`runner/envelope.py`);
  on tmux an attachment is silently dropped, with no marker line at all.
- **No recall.** Nothing on the tmux path computes recall or carries it in
  the typed envelope; the SDK lane's recall (its prompt hook, operator and
  person chat only) has no counterpart here.
- **A usage-limit screen is caught only through the transcript's limit
  entry, unmeasured live.** An `isApiErrorMessage` transcript entry whose
  text matches `transcript.LIMIT_WORDS` ("usage limit", "limit reached",
  "resets at") drives `TmuxRunner._limit_live`, requeuing the claimed row
  and moving the runner to `rate_limited`. The wording is the binary's own,
  but the live signature has never been measured against a real usage-limit
  hit; the tests exercise it on fixtures built from that wording.
- **A typed `/clear` changes the session.** The CLI starts a new session
  id; the runner keeps reading the recorded session's transcript and says so
  once with a `system` `session_changed` event naming both ids. The next
  start finds the hook's record on the new id, refuses to adopt the pane
  (`adopt_refused`, `session_mismatch`) and resumes the recorded session.
- **The tmux server's environment is shared across tmux cousins.** Every
  tmux-kind cousin's pane lives on one tmux server, one socket
  (`<framework root>/run/tmux.sock`, `TmuxRunner._make_pane`), each in its
  own session (`tmux-<home.name>`); the pane's own process environment is
  isolated per pane (`exec env -i`), but the server process itself, and
  the `run/` directory it lives in (chmod 0700 on every pane start), are one
  and the same for every tmux cousin on the host.
- **A trailing `;` in the typed first line is lost.** The first line goes
  to the pane with `tmux send-keys -l`, and tmux's own argument parser takes
  a trailing `;` (or `\;`) as its command separator, so a sender name that
  ends in one reaches the model without it. The body goes through a paste
  buffer and keeps it.
- **A numbered dialog could take the nonce's digits.** The screen is read
  before the first line is typed and again after it, but a dialog that
  opens in the window before the first key (the login menu's "2. Anthropic
  Console account" is one) would read the nonce's digits as a choice. The
  window is one tmux call wide; nothing closes it.
- **An unheld stop, then a kind change outside `cousin-migrate`, leaves
  claims behind.** An unheld stop keeps the pane and leaves
  `data/tmux-claims.json` for the next tmux start to settle. If
  `[agent] runner` is edited to `sdk` by hand before that start, the SDK
  kind's start sweep requeues the claimed rows without reading the pane's
  transcript, and a row the pane had already taken can be delivered twice.
  `cousin-migrate --to sdk` stops held, which settles them first.
- **A give-up holds for an hour, then the budget starts over.** A pane
  that dies before it has stayed up 20 s is a failed start, retried after
  1 s, doubling to 60 s; after 5 failed starts in a row, or more than 5 pane
  losses within 10 minutes, the runner gives up: `errored`, a
  `pane_failing` event, exit 2, which the supervisor leaves down as
  `failing` with the runner's reason (the console shows it), never
  restarted. The reason is kept in `data/run/tmux-giving-up.json`; a start
  within the hour (a supervisor or container restart) exits 2 again at once
  with no pane, and `cousin-supervisor start <slug>` or the console's start
  clears it. A start after the hour drops it and gets 5 more starts.
- **A pane whose pid was not read at its start has no second check.**
  Two failed `has-session` calls settle a pane as lost only when its CLI's
  pid is gone too; when tmux answered no pid right after the start
  (`_pane_pid` None), that check is skipped and a tmux hiccup is taken as
  a loss (the reopen then adopts the live pane).
- **A live turn found again by that adopt has already closed its rows.**
  The loss closed them `delivered`, cut by pane loss, before the adopt
  showed the turn still running; the turn goes on and ends normally, and
  the rows say "cut" although the model finished them. An untaken row that
  was really sitting in the CLI's queue was requeued by the loss and is
  typed again; when the CLI takes the copy under the old nonce, the runner
  says so as a `duplicate_delivery` event. Both need a pane whose pid read
  as none.
