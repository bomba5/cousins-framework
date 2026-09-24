# Commands

Every `cousin-*` command the package installs, one line on what it does and
one example each. Read this when you know roughly what you want and need the
name and the flags. Each command's `--help` is the final word.

Two things apply to almost all of them:

- Commands that work on the whole install take `--root <checkout>` or read
  `FRAMEWORK_ROOT`. Run inside the checkout and most of them find it anyway.
- Commands that work on one cousin read `COUSIN_HOME` (the cousin's home,
  `cousins/<slug>/`) or take `--home`. Inside a cousin's tmux session
  `COUSIN_HOME` is already set, so a cousin calls them bare.

The examples use an invented cousin, Wren, and an operator called ana.

```
export FRAMEWORK_ROOT=$HOME/cousins-framework
export COUSIN_HOME=$FRAMEWORK_ROOT/cousins/wren
```

## Running cousins

`cousin-spawn` creates a cousin from the template (home, `cousin.toml`,
`CLAUDE.md`, MCP registration, harness hooks) and can start it. With `--start`
alone on an existing cousin it starts it, and `--start --resume` resumes its
last session instead of opening a new one (what the start-at-boot unit
uses); `--runner sdk|fake|opencode` and `--account <name>` make it a runner cousin
(`[agent] runner` and `account`, defaulting to `COUSIN_DEFAULT_RUNNER` and
`COUSIN_DEFAULT_ACCOUNT`), which `--start` starts through `cousin-supervisor`;
`--repair-settings` rewrites an
existing cousin's hooks and `.mcp.json`; `--sync-template` shows how its
CLAUDE.md framework part differs from the template, and `--apply` writes it
(every start and flip does that by itself). See [cousins](cousins.md).

```
cousin-spawn wren --name Wren --role "keeps the house notes" \
    --voice "Short and plain. Says when it does not know." --operator ana --start
```

`cousin-auth` shows or switches how a cousin's agent logs in: `claude` (the
harness's own login, the default) or `api_key` (a per-cousin key).

```
cousin-auth wren --key-stdin < wren.key && cousin-auth wren api_key
```

`cousin-account` (operator-run) shows the accounts runner cousins run on,
from `config/accounts.toml` (see [configuration](configuration.md)). `list`
prints every account's name, kind and where its credentials live, never a
secret; `status <name>` asks the agent CLI whether that account is logged in,
with no model call, and exits 0 when it is, 4 when it is not (the line names
what to run). For an `opencode` account it runs nothing: it reads whether
the account's `auth.json` holds every provider it names (an `endpoint`
account is logged in by its configuration). `host` is the host's default login. `--root R` picks the
install; without it the root comes from `FRAMEWORK_ROOT`, then the checkout
you are in.

```
cousin-account list
cousin-account status fleet
```

`cousin-account login <name> [--via <slug>]` logs a `claude-login` account in
through the agent CLI's own login flow; `cousin-account token <name> [--via
<slug>]` mints a long-lived token for a `claude-token` account through the
CLI's `setup-token` and saves it only after the CLI reads it back as a token
login. Both are operator-run from a shell on the host: they refuse to run
inside a cousin (or under one), and they want a terminal (a usability check,
not a safeguard). With `--via` the sign-in URL arrives in
that cousin's chat (and on Telegram when the cousin has a bridge); sign in on
any device and reply in the same chat with the whole code the page shows
(`code#state`). Paste the code as your next message there; only a message
shaped like the code is taken, and it is never delivered to the cousin and
never kept in the chat history, which holds a `[login code received ...]` line
instead; for an hour after, a second or late code there is discarded the same
way. A code pasted through Telegram stays in Telegram's own message history:
it is single-use and tied to that login flow, but you may delete the message.
Do not answer a login notice you did not
start yourself. Without `--via` the URL is printed and the code is asked for
at the terminal. `--timeout` is the window for the code in seconds (600; it
must be positive). Exit 0 done, 4 the flow failed (the line says why, in
the CLI's own words when it gave any), 2 refused (inside a cousin, no
terminal, an unknown account, a `--via` cousin that is missing or has no
operator). `login host` re-logs the host's own login in `~/.claude` and says
so first. The login lane is every `claude-login` account (`host` and each
named one) and every `claude-token` account (a long-lived subscription token
from `claude setup-token`): putting a subscription's credentials into the
framework with `login` or `token` carries the same terms risk as running a
cousin on the login lane at all, and it is the user's.

```
cousin-account login fleet --via wren
cousin-account token nightly --via wren
```

For an `opencode` account, `cousin-account login <name> --provider <id>` puts
one provider's credential into the account's `auth.json`
(`<data_dir>/data/opencode/auth.json`). The provider must be one the account
names in `providers`. An API key never travels through chat, so `--via` is
refused here: the key is read from stdin (hidden when stdin is a terminal,
otherwise one line, so it can come on a pipe) or from `--key-file <path>`,
which is read as strictly as a secret file (a 0700 directory and a 0600
regular file of yours, no symlink, one line). It is written the way
`opencode auth login` writes it (`{"<id>": {"type": "api", "key": ...}}`),
merged with the providers already there, file 0600 in a 0700 directory,
through a temporary file and a rename; no opencode process runs, and the
key is never printed. Only the key is stored: a provider whose own opencode
login asks for more than the key (an Azure resource name, a Cloudflare
account id, a GitLab instance URL) needs the rest in its configuration. `--method <label>` instead runs `opencode auth login
--pure --provider <id> --method <label>` in a terminal under the account's
`HOME` and XDG directories, for an OAuth method (the labels are opencode's,
for example `ChatGPT Pro/Plus (headless)`). opencode 1.18.31 shows a URL and
one instruction line (a device code to enter, or "complete authorization in
your browser") and then waits; nothing is pasted back. Those are printed, or
with `--via <slug>` posted to that cousin's operator in its chat (no code
capture is armed), and the login counts once the account's `auth.json` holds
the provider, not when the screen says so. A method that asks for an API
key, or asks another question first, ends the flow and says so. A browser
method redirects to `localhost` on the host, so it only completes from a
browser on the host; a device-code method completes from any device.
Refused (exit 2): `anthropic` or any method named Claude or Anthropic by
OAuth (a Claude subscription; an Anthropic API key is fine), the provider
`opencode` (the hosted service the runner disables), a provider or method
that names the Claude-subscription bridge, an `endpoint` account, and these
flags on any other kind of account. The binary is `COUSIN_OPENCODE_BIN`
(an absolute path), else `opencode` on `PATH`. `--timeout` bounds the wait
for an OAuth login.

```
cousin-account login keyed --provider openai --key-file ~/keys/openai.key
pass show openai | cousin-account login keyed --provider openai
cousin-account login keyed --provider openai --method "ChatGPT Pro/Plus (headless)" --via wren
```

`cousin-flip` ends the cousin's current generation and starts the next one on
a fresh session with a boot packet. `--dry-run` runs the checks only;
`--confirm` asks the new generation to post one line when it is oriented. On
a runner cousin (`[agent] runner`), `cousin-flip` is a rollover: it puts (or
joins) the pending `flip` row on the running `cousin-runner` and waits for
the handoff, the same path context pressure or the daily cadence uses. It
refuses a stopped runner cousin (start it first): a rollover needs a runner
to carry it out.

```
cousin-flip wren --dry-run
```

`cousin-reincarnate` changes a cousin's role, keeps its memory, and flips it
(after asking it for a handoff).

```
cousin-reincarnate wren --new-role "keeps the house notes and the budget"
```

`cousin-transplant` moves memory or bodies between two cousins; modes are
`soul-donation`, `body-swap` and `merge`.

```
cousin-transplant --donor wren --recipient kestrel --mode merge
```

`cousin-self-portrait` drafts, shows, diffs and commits the cousin's reviewed
self-portrait. Subcommands: `synthesize`, `show`, `diff`, `commit`.

```
cousin-self-portrait synthesize && cousin-self-portrait diff
```

`cousin-session` runs the start or end hooks listed in `cousin.toml
[session]`. Subcommands: `start`, `end`, `status`; `--skip NAME` leaves one
out.

```
cousin-session end --skip sync-state
```

`cousin-cycle` keeps per-cousin session counters and breadcrumbs the boot
packet reads. Subcommands: `state [--json]`, `inc --start|--end|--action X`,
`reset`.

```
cousin-cycle inc --action "shipped the weekly report"
```

`cousin-runner` runs a cousin on the runner instead of a tmux session: the
inbox is the bus and the wake socket is the doorbell, no port. At start it
resumes the session saved in `data/runner-session.json`, falling back to a
fresh session carrying the state digest as its first message when it cannot
(no saved session, or the CLI does not recognise the saved one: a
`resume_failed` event either way, never a crash). It runs until
SIGTERM or SIGINT, then stops the runner with a 30 second timeout
(`runner.main.STOP_TIMEOUT_S`). `--once`
exits instead when the inbox is drained and no turn is running, on SIGTERM or
SIGINT, or when the runner gives up. `--runner sdk|fake|opencode` overrides the
cousin's `[agent] runner`. One runner per cousin: it holds a lock on
`<home>/run/runner.lock` for its life. It reads `policy.toml` at start; a
malformed policy is rc 2, and so is an MCP registry that does not parse or
names a command the runner has no in-process handler for. `--home` may be
relative: the runner makes it absolute and exports `COUSIN_HOME` (the home)
and `FRAMEWORK_ROOT` (the install above it, else the home's grandparent) into
its own environment, for the in-process tools and for the model's own
`cousin-*` commands. See [agent-loop-runner](design/agent-loop-runner.md).

An account that needs a login (or whose billing stopped it) never makes the
runner exit: it waits, says so in `data/login-required.json` (see
[configuration](configuration.md#datalogin-requiredjson)) and picks up the fix
without a restart. A missing secret file is such a login, not exit 2.

`--check-auth` answers whether the cousin's account is logged in, with
`claude auth status` under the account (no model call; presence, not
validity), and prints one line. `--validate` (only with `--check-auth`) then
runs ONE smallest model turn on a bare throwaway client under the account: no
tools, no MCP server, no hooks, no session store, one turn, a temporary
directory, a timeout (the SDK lane's turn: for an `opencode` account
`--check-auth` reports presence only and `--validate` is refused, rc 2). Both run before the lock, the runner and the
environment export, so they work beside a live runner and take nothing from
it. An install script can gate on `cousin-runner --home H --check-auth`.

| exit | meaning |
|---|---|
| 0 | stopped by SIGTERM or SIGINT, or `--once` drained the inbox, or `--check-auth` found the account logged in (and `--validate`'s turn answered) |
| 2 | configuration: no or an unknown `[agent] runner`, an unreadable cousin.toml, a key file open to others or malformed, a malformed `policy.toml`, an MCP registry that does not parse or names a command with no in-process handler, an account on the other lane (an `opencode` runner on a Claude account, an `sdk` runner on an `opencode` one), an `opencode` cousin with no `[agent] model` or whose config or environment names the Claude-subscription bridge |
| 3 | the runner gave up: its worker ended (it could not connect, or a reconnect failed; on opencode: the server did not start, or it does not report the runner's MCP server connected), or under `--once` it stayed `errored` for more than 10 seconds, or under `--once` a side session (`[agent.sessions]`) gave up and the batch had not drained more than 10 seconds later (the clock runs on across rebuilds that fail to connect; a rebuild that connects ends it); the long-running mode keeps rebuilding a side session and never exits for one |
| 4 | a person must log in: `--check-auth` found the account not logged in (or `--validate`'s turn did not answer), or `--once` found the runner, or any of its side sessions, waiting for a login. A supervisor must not restart on it |
| 5 | busy: another runner holds `<home>/run/runner.lock` (tried for about a second first, so a status probe of the lock never refuses a runner). Not a configuration problem: a supervisor retries after its backoff and never counts it |

```
cousin-runner --home cousins/wren
cousin-runner --home cousins/wren --once
cousin-runner --home cousins/wren --check-auth
cousin-runner --home cousins/wren --check-auth --validate
```

`cousin-watch <slug>` prints a runner cousin's reasoning stream in any
terminal: the same events the console's pane shows (its state changes, the
turns, its text, thinking and tool calls, their results), one line each, read
from the runner's primary stream under the cousin's own `data/stream/`, with
no console and no port. It starts at the newest 200 events (`--tail N` for
another number, `--tail 0` for the whole stream), or after event `N` of the
runner's current stream with `--after N`. Without `--follow` it prints those
and exits; with `--follow` (`-f`) it keeps printing as the runner appends,
following a restarted runner to its new stream, until interrupted. `--json`
prints each event as its JSON line; `--home` names the home instead of
finding it by slug. A tmux cousin has no stream (its view is its
tmux pane): exit 2, as for an unknown cousin.

```
cousin-watch wren -f
cousin-watch wren --json --after 120
```

`cousin-supervisor run` keeps an install's daemons up in one process: the
console, the loops daemon and one `cousin-runner` per runner cousin (every
cousin whose `cousin.toml` says `[agent] runner = "sdk"` or `"fake"`, unless
`[agent] auto_start = false`; tmux cousins are never its). It is a container's
init and a bare host's single unit. Each child's output goes to its stdout,
every line prefixed with the child's name (`console | ...`, `runner:wren |
...`). A child that exits is restarted after 1, 2, 4 ... up to 60 seconds; five
exits inside a minute mark it `failing` and leave it down, loudly. A runner's
exit 2 (configuration) is `failing` at once and its exit 4 (a login to do) is
never restarted. Exit 5 is busy, for a runner (another runner holds the
cousin's lock) and for the loops daemon (another loops daemon holds the
install's): the child waits in `backoff`, retried after 1, 2, 4 ... 60
seconds with one line per attempt, and a busy exit is never counted toward
`failing`, so the child starts as soon as the holder is gone, however long
it stayed. The console's own restart (exit 75) comes back at
once. On SIGTERM or SIGINT it stops the runners first (together, 35 seconds
each: the runner's own 30 second stop, `runner.main.STOP_TIMEOUT_S`, plus 5),
then the loops daemon, then the console, and exits 0. On
SIGHUP (or `reload`) it rescans `cousins/`: a new runner cousin is started, one
that is gone or left the runner lane is stopped, a `failing` child is started
again, nothing healthy is touched. `--no-console`, `--no-loops`,
`--console-host` (`127.0.0.1`), `--console-port` (8600) and `--loops-interval`
(30) shape what it runs; `--root R` picks the install, else `FRAMEWORK_ROOT`,
else the checkout you are in. One supervisor per install: it holds
`run/supervisor.lock`, and a second one exits 2.

The running supervisor answers on `run/supervisor.sock` (the `run/` directory
is private to its user). `status [--json]` lists every child with its state
(`running`, `backoff`, `failing`, `stopped`), pid, restarts and the reason;
`start <slug>` starts a runner cousin's child (also one with `auto_start =
false`) or clears a `failing` one; `stop <slug>` stops it once its turn is done
(`--no-wait` answers once it is signalled) and holds it down until `start`,
across supervisor restarts too (`<home>/run/held`, see
[configuration](configuration.md)). A slug is only ever a runner cousin; the
console and the loops daemon are `--name console` and `--name loops`, held
until `start` or the next supervisor start. `reload` is SIGHUP. These exit 0
done, 1 when no supervisor is running, 2 when it refused (the line says why).
`run/supervisor.json` holds the same status for readers that want a file.

```
cousin-supervisor run --console-host 0.0.0.0
cousin-supervisor status
cousin-supervisor stop wren && cousin-supervisor start wren
cousin-supervisor start --name loops
```

`cousin-migrate` moves one cousin from the tmux lane to the SDK runner, and
back. Nothing else ever does: an upgrade or a merge leaves every cousin on the
lane its `cousin.toml` names. `plan <slug> [--account NAME]` checks, writing
nothing, that the cousin is running on the tmux lane (a stopped one would be
started by the move), with no migration open, that its account is logged in,
that a `cousin-supervisor` runs, that the SDK is installed and that its
auto-memory imports without a conflict, then lists the steps; it exits 0
ready, 1 not. It also warns (`warn 2.0.0 ...`), without blocking, about every
key the cousin or the install still carries that 2.0.0 will reject, with the
line to follow (`cousin_lib/removed_keys.py`). `apply <slug> [--account NAME] --yes` runs them in order and
stops at the first that fails: `close` (the console's clean stop: the
handoff, the transcript mined), `import` (`cousin-memory import-auto
--apply`), `toml` (`[agent] runner = "sdk"`, and the account, once the tmux
session is still down), `start` (the review gate's cursor opens afresh, the
supervisor starts the runner, and the cousin's chat server is started, since
the supervisor runs none and peers reach the inbox through it; afterwards
`cousin-chat-watchdog` keeps it up) and `verify` (the runner stays up and holds its
lock for 10 seconds, and the chat server answers `/health`).
`data/migration.json` keeps the prior `cousin.toml`, its bytes and mode,
written before the first step, and each step's outcome. `rollback <slug>
--yes` undoes what ran: it stops the runner and waits until it lets go of
its lock, puts the file back, has the supervisor rescan, writes a fresh
boot packet, starts the tmux session unless it already runs, and releases
the supervisor's hold on the runner (`run/held`). It refuses a second rollback,
inbox rows still waiting and an inbox it cannot read (`--force` rolls back
anyway; rows stay in `data/inbox.db`). `check <slug> [--since ISO] [--json]`
is the week's measure, from the migration on by default: inbox rows not
done after an hour, tool calls with no recorded result, recorder hooks that
failed, an unreadable inbox, a chat server that does not answer; exit 0
clean, 1 not. The runbook, with the fleet's order and the rollback, is in
[migrating](migrating.md#from-the-tmux-lane-to-the-sdk-runner).

```
cousin-migrate plan wren --account team
cousin-migrate apply wren --account team --yes
cousin-migrate check wren
cousin-migrate rollback wren --yes
```

## Memory

`cousin-memory` is the cousin's memory tool. Subcommands:

| subcommand | does |
|---|---|
| `search QUERY [--top N] [--collection memory\|notes\|harness] [--json]` | keyword search, plus semantic when embeddings are configured |
| `remember TOPIC FACT [--level L] [--cite SRC]` | one fact into raw memory with its truth level |
| `decide TOPIC DECISION REASONING [--level L] [--cite SRC] [--stdin]` | log a decision (and a raw copy of it) |
| `obsolete TOPIC --why REASON [--force] [--entry ID]` | retire a topic (L5): out of the distilled views, history kept; with `--entry`, retire one of its claims by its id and keep the topic |
| `tensions [--json]` | topics whose live claims disagree: an authored topic with two or more live claims of different content, each claim's id, and how to settle it (retire one with `obsolete --entry`) |
| `history TOPIC` | a topic's claims, oldest first: each one's id, when it became valid, and `live` or when an obsolete mark retired it (valid time is derived from raw, never written back) |
| `review [--keep ID... \| --drop ID... [--why REASON]]` | the entries the review gate holds (more than `[memory] review_batch` written on authored topics since it last looked), or the operator's verdict on some: keep releases them into the memory views, drop retires them; a verdict is refused inside a cousin's own process tree |
| `recall [KEYWORD] [--last N]` | raw memory through the search index: with a keyword the best-ranked N entries, without one the newest N you wrote (the framework's own log left out); a decision prints with its `Why:` line |
| `activity TEXT` | set the "what I'm doing now" line |
| `distill [--max-lines N]` | rebuild `memory/distilled/` from raw |
| `consolidate` | list recurring topics, then distill |
| `import-auto [--apply [--sample N]] [--json] [--verify]` | fold the agent CLI's own auto-memory into `memory/imported/auto/` with provenance: a dry run unless `--apply`, idempotent, an edited copy never overwritten, a removed copy never imported again; `--apply` first replays your logged queries that reached that memory as a baseline, and `--verify` replays them again: exit 1 if any lost a memory, 2 if nothing was compared |
| `compact [--target index\|raw] [--budget B] [--hot-days D] [--dry-run]` | trim the MEMORY.md index, or fold old raw days into monthly archives |
| `reindex` | rebuild the search indexes from scratch |
| `propose-shared [--commit]` | nominate shareable memories for the shared tier |
| `trash [list]`, `trash restore ID` | list and restore memories removed in the console |

Truth levels for `--level` are `operator`, `framework`, `tool`,
`conclusion` (the default), `hypothesis` and `obsolete`. `--level operator`
means "ana said this" and needs `--cite`. See [memory](memory.md).

```
cousin-memory remember "backups" "the NAS snapshot runs at 02:00" \
    --level operator --cite "chat #412"
```

`cousin-shared` works the shared tier: list and read canonical files, propose
a file (body on stdin), and promote or reject a proposal as a reviewer.
Subcommands: `list`, `read`, `diff`, `propose`, `promote`, `reject`.

```
cousin-shared promote house-rules.md --proposer wren --by ana
```

`cousin-callback` is a cousin's library of moments worth calling back to.
Subcommands: `tag`, `list`, `search`.

```
cousin-callback tag "ana named the espresso machine Gustav" --category banter
```

`cousin-reason` writes and lists reasoning capsules: a conclusion with its
evidence and the alternatives you rejected. Subcommands: `capsule`, `list`.

```
cousin-reason capsule --conclusion "keep backups for 30 days" \
    --evidence "audits need a month" --rejected "7 days" --confidence high
```

`cousin-sync-state` renders the newest `## Open loops` section of STATUS.md
into `data/state.json`.

```
cousin-sync-state --home cousins/wren
```

## Chat

`cousin-reply` stores a reply from the cousin to a person in its own chat
history (no chat server needs to run). The body comes from stdin or `-m`; `--image` attaches a picture and
`--video` a video. It
has no positional text argument.

```
cousin-reply --user ana <<'EOF'
Done. The notes are in notes/2026-09-18-budget.md.
EOF
```

`cousin-chat` sends a message to another cousin (or an external peer), or
lists who is addressable. A runner cousin is written directly (its chat
history and inbox, no chat server needed); a tmux cousin is reached
through its chat server. Subcommands: `send SLUG TEXT [--from NAME]`, `list`.

```
cousin-chat send kestrel "the greenhouse report is ready" --from Wren
```

`cousin-chat-server` is the per-cousin chat server. `cousin-spawn --start`
and the console launch it for you; run it by hand for debugging.
`--no-terminal-delivery` stores messages without typing them into the tmux
session.

```
cousin-chat-server --home cousins/wren
```

`cousin-chat-import` imports a cousin's chat history from the previous
framework (stop its chat server first). A second run is refused unless
`--force`.

```
cousin-chat-import wren --old-home /srv/old/cousins/wren/files
```

`cousin-chat-watchdog` makes one pass over the fleet: starts a missing chat
server for a running cousin, reports a sick one, never kills anything.

```
cousin-chat-watchdog --dry-run
```

`cousin-telegram` bridges one cousin's chat to Telegram (long polling, off
until `[telegram]` in its `cousin.toml` is complete). See [telegram](telegram.md).

```
cousin-telegram --home cousins/wren
```

`cousin-meeting` runs meetings: a chat shared by the user and several running
cousins, in rounds ([meetings](meetings.md)). Subcommands: `list`, `show ID`,
`open TOPIC SLUG... [--facilitator SLUG] [--timeout S]`, `post ID TEXT`,
`skip ID`, `close ID`, `delete ID` (the user's side, `--user NAME`), `say ID TEXT`,
`pass ID`, `minutes ID TEXT` (the cousin's side, from `COUSIN_HOME`; `--stdin`
for long text).

```
cousin-meeting open "name the new sensor" wren kestrel --user ana
cousin-meeting say 3 "Kestrel"
```

## Media

`cousin-image`, `cousin-voice` and `cousin-video` generate media through the
provider in `config/media.toml`. `gen PROMPT` writes a file; `chat PROMPT
--user NAME [--caption TEXT]` generates and posts it to that person's chat.
See [media](media.md).

```
cousin-image chat "a violet sunrise over a ridge" --user ana --caption "morning"
cousin-voice gen "Standup in five minutes."
cousin-video gen "a slow pan across a misty ridge at dawn"
```

## Jobs, loops and schedules

`cousin-job` registers and tracks subagents and background jobs; the console's
Jobs page reads the same store. Subcommands: `start KIND TITLE [-- CMD...]`
(kinds `subagent`, `shell`, `build`, `other`), `done`, `fail`, `cancel`,
`list`, `show`, `tail [-f]`.

```
cousin-job start shell "rebuild the index" -- cousin-memory reindex
```

`cousin-loops` is the loops daemon and its controls. Subcommands: `run
[--interval S] [--ticks N]` (the daemon), `status`, `requests`, `flips` (each
cousin's daily flip time and where it comes from), `fire SLUG LOOP`. One
clock per install: `run` holds a lock on `<root>/run/loops.lock` for its life,
and a second `run` on the same root exits 5 (busy) with "another loops daemon
holds <path>" (two daemons would fire every one-shot, heartbeat and flip
twice). Under `cousin-supervisor` that exit leaves its loops child waiting in
`backoff`, not `failing`: it becomes the clock once the other daemon stops. See
[jobs and loops](jobs-and-loops.md).

```
cousin-loops fire wren context-heartbeat
```

`cousin-schedule` queues a one-shot prompt for a future time. Subcommands:
`add WHEN PROMPT` (`in 30m`, `tomorrow 06:30`, or an ISO date), `list
[--all]`, `cancel ID`, `tick`.

```
cousin-schedule add "tomorrow 09:00" "Check that the backup ran."
```

`cousin-tracker` is the install-wide list of work in flight. Subcommands:
`add`, `update`, `state ID open|active|blocked|done|dropped`, `list`, `show`,
`delete`.

```
cousin-tracker add "move the photo archive" --domain infra --tag q4
```

## Remote cousins

`cousin-hive` runs a standalone queen and manages node tokens. Subcommands:
`serve`, `mint`, `revoke`, `forget`, `nodes`, `send`, `recall`,
`import-legacy`. Most installs use the console as the queen instead. See
[remote cousins](remote-cousins.md).

```
cousin-hive mint kestrel --scope own,shared
```

`cousin-spawn-node` builds the tarball for a cousin that runs on another
machine; you copy it over and run its `install.sh` there.

```
cousin-spawn-node kestrel --queen-url http://192.0.2.10:8600 \
    --name Kestrel --role "watches the greenhouse" --out /tmp/build
```

## Console and tools

`cousin-console` serves the web console (default `127.0.0.1:8600`), or
creates a login with `adduser NAME`. See [console](console.md).

```
cousin-console --host 0.0.0.0 --port 8600
cousin-console adduser ana
```

`cousin-ui` is the old name of the console. It prints a note and runs
`cousin-console` with the same flags.

```
cousin-ui --port 8600
```

`cousin-mcp` is the MCP server a cousin's harness starts (stdio). By hand you
use it to check the registry, call one tool, find out why the server failed,
or record the harness's approval of a cousin's `.mcp.json` with
`approve SLUG`. See [mcp](mcp.md).

```
cousin-mcp --call memory '{"command": "search", "query": "backup"}'
cousin-mcp --last-connection      # why it failed, with the server's stderr
```

## Maintenance

`cousin-backup` snapshots one cousin's databases (through `VACUUM INTO`),
the runner's event stream (`data/stream/`) and state files (the resume
sessions, generation count and mining cursors), `memory/` and core markdown into
`<dest>/<slug>/<date>/`. The inbox is copied first, so a restore answers a
mid-turn message at least once and never loses it.

```
cousin-backup --home cousins/wren --dest /var/backups/cousins
```

`cousin-sweep` runs maintenance over every cousin in turn. Today that is
`compact --target index|raw|both`.

```
cousin-sweep compact --target both
```

`cousin-tool-surface` writes `data/tool-surface.md`, one line per command from
its `--help`. The boot packet quotes it so a new generation knows its tools.

```
cousin-tool-surface
```

`cousin-cache-audit` reads the harness transcripts and reports the prompt
cache hit rate and the files that probably broke it. Needs `transcripts_dir`
in `config/harness.toml`.

```
cousin-cache-audit --days 7 --diagnose
```

`cousin-version` prints the framework version (and commit in a git checkout),
or bumps it in `pyproject.toml`.

```
cousin-version bump patch
```

`cousin-gate` scans a tree you are about to publish for private addresses,
home paths, secret shapes, binaries and denylisted terms.

```
cousin-gate --root /tmp/publish --denylist ~/private/denylist.txt
```
