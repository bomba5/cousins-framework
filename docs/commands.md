# Commands

Every `cousin-*` command the package installs, one line on what it does and
one example each. Read this when you know roughly what you want and need the
name and the flags. Each command's `--help` is the final word.

Two things apply to almost all of them:

- Commands that work on the whole install take `--root <checkout>` or read
  `FRAMEWORK_ROOT`. Run inside the checkout and they find it on their own;
  run elsewhere without either, they exit 2 naming what to set.
- Commands that work on one [cousin](glossary.md#cousin) read `COUSIN_HOME` (the cousin's home,
  `cousins/<slug>/`) or take `--home`. Inside a running cousin, whatever its
  kind, `COUSIN_HOME` is already set (exported into the
  [runner](glossary.md#runner)'s own process, and from there into what the
  model runs), so a cousin calls them bare.

The examples use an invented cousin, Wren, and an operator called ana.

```
export FRAMEWORK_ROOT=$HOME/cousins-framework
export COUSIN_HOME=$FRAMEWORK_ROOT/cousins/wren
```

## Which commands you need

Each command has one class, so you know which ones to learn first:

- **core**: you use it from day one to run, keep and upgrade a cousin.
  [Getting started](getting-started.md) uses only these.
- **optional**: it belongs to a feature you can leave off
  ([what is optional](getting-started.md#what-is-optional)).
- **cousin**: a cousin runs it from its own session; you rarely type it.
- **internal**: the framework runs it (the
  [supervisor](glossary.md#supervisor), a timer, a session hook).
- **developer**: for working on the framework itself.

| command | class | what it is for |
|---|---|---|
| `cousin-account` | core | the accounts cousins run on, and whether each is logged in |
| `cousin-backup` | core | snapshot one cousin's databases and memory |
| `cousin-console` | core | the web console; `adduser` makes a login |
| `cousin-doctor` | core | what in the install to fix by hand, with the fix; changes nothing |
| `cousin-flip` | core | end a cousin's session and start it fresh ([flip](glossary.md#flip)) |
| `cousin-health` | core | what has been failing, and for how long |
| `cousin-loops` | core | the scheduler the supervisor runs; `flips` shows when each cousin flips |
| `cousin-mcp` | core | a cousin's tools over MCP; `approve` records the harness's approval |
| `cousin-memory` | core | search and write a cousin's memory |
| `cousin-shared` | core | the [shared tier](glossary.md#shared-tier); `templates` compares the law and house rules with what a release ships |
| `cousin-spawn` | core | make a cousin, and start it |
| `cousin-supervisor` | core | the one process that runs the console, the loops daemon and every cousin |
| `cousin-tool-surface` | core | write the list of commands a cousin reads instead of probing each `--help` |
| `cousin-upgrade` | core | plan an upgrade, bring the homes to it, switch the code and restart |
| `cousin-version` | core | the version and commit you run |
| `cousin-watch` | core | a cousin's reasoning, live |
| `cousin-cache-audit` | optional | the prompt cache hit rate, from the harness transcripts |
| `cousin-chat-import` | optional | import a cousin's chat history from an older install |
| `cousin-hive` | optional | the queen for cousins on other machines, and its node tokens |
| `cousin-image` | optional | generate an image ([media](media.md)) |
| `cousin-meeting` | optional | a chat with several cousins at once, in rounds |
| `cousin-migrate` | optional | move a cousin from an older release, or to another runner kind |
| `cousin-reincarnate` | optional | give a cousin a new role and keep its memory |
| `cousin-spawn-node` | optional | build a cousin that runs on another machine |
| `cousin-telegram` | optional | a cousin's chat on Telegram |
| `cousin-transplant` | optional | move memory or bodies between two cousins |
| `cousin-video` | optional | generate a video ([media](media.md)) |
| `cousin-voice` | optional | generate speech ([media](media.md)) |
| `cousin-callback` | cousin | a cousin's library of moments worth calling back to |
| `cousin-chat` | cousin | message another cousin, list who can be reached |
| `cousin-job` | cousin | register and track background jobs; the console's Jobs page shows them |
| `cousin-reason` | cousin | write and list reasoning capsules |
| `cousin-reply` | cousin | a cousin's answer to a person, in its chat history |
| `cousin-schedule` | cousin | a one-shot prompt at a future time |
| `cousin-self-portrait` | cousin | draft the cousin's self-portrait; a person commits it |
| `cousin-tracker` | cousin | the install's list of work in flight; the console's Tracker page shows it |
| `cousin-cycle` | internal | per-cousin session counters the boot packet reads |
| `cousin-runner` | internal | drives one cousin; the supervisor starts one per cousin |
| `cousin-session` | internal | a cousin's session start and end hooks |
| `cousin-sweep` | internal | the weekly memory compaction, from its timer |
| `cousin-sync-state` | internal | renders STATUS.md's open loops into `data/state.json` |
| `cousin-gate` | developer | scan a tree for private data before you publish it |

## Running cousins

`cousin-spawn` creates a cousin from the template (home, mode 0700, `cousin.toml`,
`CLAUDE.md`, MCP registration, harness hooks) and can start it. With `--start`
alone on an existing cousin it starts it; `--runner sdk|fake|opencode|tmux` and `--account <name>` name its runner kind
and account (`[agent] runner` and `account`, defaulting to `COUSIN_DEFAULT_RUNNER`,
else `sdk`, and `COUSIN_DEFAULT_ACCOUNT`; its `--model` and `--effort` go to `[agent]`
too, where the runner reads them, and only on a [lane](glossary.md#lane) that reads them; `--runner opencode` is refused before anything is written when the opencode binary is not found); `--start`
starts it through `cousin-supervisor`; `--role-paragraph` is the longer role
text in `CLAUDE.md` (else the `--role` line); `--heartbeat SECONDS` sets
`[heartbeat] context_beat_seconds`; `--memory-scope {private,shared}` sets
`[memory] scope` (`shared` may propose to the [shared tier](glossary.md#shared-tier); default `private`);
`--repair-settings` rewrites an
existing cousin's hooks and `.mcp.json`; `--sync-template` shows how its
CLAUDE.md framework part differs from the template, and `--apply` writes it
(every start and [flip](glossary.md#flip) does that by itself). See [cousins](cousins.md).

A cousin with no `[agent] runner` is refused by
name, with one line and before anything runs, by `cousin-spawn --start` (exit
2), by a stop (the
console's answers 409), by `cousin-flip` and by `cousin-reincarnate`. A
[worker](glossary.md#worker) (`[cousin] type = "worker"`) has no session: its stop is a no-op that
says so.

```
cousin-spawn wren --name Wren --role "keeps the house notes" \
    --voice "Short and plain. Says when it does not know." --operator ana --start
```

`cousin-account` (operator-run) shows the accounts runner cousins run on,
from `config/accounts.toml` (see [configuration](configuration.md)). `list`
prints every account's name, kind and where its credentials live, never a
secret; `status <name>` asks the agent CLI whether that account is logged in,
with no model call, and exits 0 when it is, 4 when it is not (the line names
what to run). For an `opencode` account it runs nothing: it reads whether
the account's `auth.json` holds every provider it names, except `opencode`,
whose free models need no key (an `endpoint`
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
start yourself. A good login with `--via` also deletes that cousin's
`data/login-required.json` when the file names this account, so a runner
waiting for the login retries at once (see
[configuration](configuration.md#datalogin-requiredjson)). Without `--via` the URL is printed and the code is asked for
at the terminal. `--timeout` is the window for the code in seconds (600; it
must be positive). Exit 0 done, 4 the flow failed (the line says why, in
the CLI's own words when it gave any), 2 refused (inside a cousin, no
terminal, an unknown account, a `--via` cousin that is missing or has no
operator). `login host` re-logs the host's own login in `~/.claude` and says
so first. The login lane is every `claude-login` account (`host` and each
named one) and every `claude-token` account (a long-lived subscription token
from `claude setup-token`): putting a subscription's credentials into the
framework with `login` or `token` carries the same terms risk as running a
cousin on the login lane at all, and it is the user's (see
[Claude logins and Anthropic's terms](terms-risk.md)).

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
the provider, not when the screen says so; a good one wakes the `--via`
cousin as `login --via` does. A method that asks for an API
key, or asks another question first, ends the flow and says so. A browser
method redirects to `localhost` on the host, so it only completes from a
browser on the host; a device-code method completes from any device.
Refused (exit 2): the provider `anthropic`, by key or by OAuth, and any
method named Claude or Anthropic (Claude cousins run on the Agent SDK and
nowhere else), a provider the account does not name
(`opencode`, the hosted service the runner disables, included), a provider or method
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
a fresh session with a boot packet. `--dry-run` runs the checks only. On
a runner cousin (`[agent] runner`), `cousin-flip` is a [rollover](glossary.md#rollover): it puts (or
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

`cousin-runner` runs a cousin on its runner: the
[inbox](glossary.md#inbox) is the bus and the wake socket is the doorbell, no port. At start it
resumes the session saved in `data/runner-session.json`, falling back to a
fresh session carrying the state digest as its first message when it cannot
(no saved session, or the CLI does not recognise the saved one: a
`resume_failed` event either way, never a crash). A stop cuts the [turn](glossary.md#turn) in
flight, and the agent CLI records that as the user's stop ("stop what you
are doing and wait for the user"), so when the last runner stopped (or died)
mid-turn, a resumed session's first line is the runner's own: the runner
restarted, that was not the operator, continue where you were. A stop that
was asked for (the console's stop, `cousin-supervisor stop`) is named as
that stop instead, and the console's restart as a requested restart, with
the same "continue where you were"
(`data/runner-restart.json` marks it). A fresh start drops the mark,
unless the cut turn ran tools: then the `sdk` kind gives the fresh session
a line too. On the `sdk` kind either line lists the tool calls the cut turn
had made, from `data/turn-tools.jsonl`, as finished, failed or started with
no result, and says whether its message comes again: after a death it does,
after a stop it does not (see
[runners](reference/runners.md#a-turn-cut-by-a-restart)). It runs until
SIGTERM or SIGINT, then stops the runner with a 30 second timeout
(`runner.main.STOP_TIMEOUT_S`). `--once`
exits instead when the inbox is drained and no turn is running, on SIGTERM or
SIGINT, or when the runner gives up. `--runner sdk|fake|opencode|tmux` overrides the
cousin's `[agent] runner`. `--reap-pane` is the `tmux` kind's cleanup: with no
runner holding the cousin's lock, it kills that cousin's pane (exit 0 whether
or not one was there) instead of leaving an unsupervised turn running; with a
runner holding the lock it refuses and says to stop the runner instead, which
kills the pane itself when it was told to hold it. One runner per cousin: it holds a lock on
`<home>/run/runner.lock` for its life (a missing `run/` is made 0700 first). It reads `policy.toml` at start; a
malformed policy is rc 2, and so is an MCP registry that does not parse or
names a command the runner has no in-process handler for. `--home` may be
relative: the runner makes it absolute and exports `COUSIN_HOME` (the home)
and `FRAMEWORK_ROOT` (the install above it, else the home's grandparent) into
its own environment, for the in-process tools and for the model's own
`cousin-*` commands. See [runners](reference/runners.md).

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
| 2 | configuration: no or an unknown `[agent] runner`, an unreadable cousin.toml, a key file open to others or malformed, a malformed `policy.toml`, an MCP registry that does not parse or names a command with no in-process handler, an account on the other lane (an `opencode` runner on a Claude account, an `sdk` runner on an `opencode` one), an `opencode` cousin with no `[agent] model` or whose config or environment names the Claude-subscription bridge; or a `tmux` runner that gave up on its pane (5 failed starts in a row, or more than 5 pane losses in 10 minutes; the reason in `data/run/tmux-giving-up.json`, which holds the next start down for an hour or until `cousin-supervisor start`) |
| 3 | the runner gave up: its worker ended (it could not connect, or a reconnect failed; on opencode: the server did not start, or it does not report the runner's MCP server connected), or under `--once` it stayed `errored` for more than 10 seconds, or under `--once` a side session (`[agent.sessions]`) gave up and the batch had not drained more than 10 seconds later (the clock runs on across rebuilds that fail to connect; a rebuild that connects ends it); the long-running mode keeps rebuilding a side session and never exits for one |
| 4 | a person must log in: `--check-auth` found the account not logged in (or `--validate`'s turn did not answer), or `--once` found the runner, or any of its side sessions, waiting for a login. A [supervisor](glossary.md#supervisor) must not restart on it |
| 5 | busy: another runner holds `<home>/run/runner.lock` (tried for about a second first; a status probe only queries the lock and takes nothing, so it never refuses a runner). Not a configuration problem: a supervisor retries after its backoff and never counts it |

```
cousin-runner --home cousins/wren
cousin-runner --home cousins/wren --once
cousin-runner --home cousins/wren --check-auth
cousin-runner --home cousins/wren --check-auth --validate
```

`cousin-watch <slug>` prints a runner cousin's reasoning [stream](glossary.md#stream) in any
terminal: the same events the console's pane shows (its state changes, the
turns, its text, thinking and tool calls, their results), one line each, read
from the runner's primary stream under the cousin's own `data/stream/`, with
no console and no port. It starts at the newest 200 events (`--tail N` for
another number, `--tail 0` for the whole stream), or after event `N` of the
runner's current stream with `--after N`. Without `--follow` it prints those
and exits; with `--follow` (`-f`) it keeps printing as the runner appends,
following a restarted runner to its new stream, until interrupted. `--json`
prints each event as its JSON line; `--home` names the home instead of
finding it by slug. This works for every runner kind, `tmux` included (see
[cousins](cousins.md)); a cousin with no `[agent] runner` has no stream:
exit 2, as for an unknown cousin.

```
cousin-watch wren -f
cousin-watch wren --json --after 120
```

`cousin-supervisor run` keeps an install's daemons up in one process: the
console, the loops daemon, one `cousin-runner` per runner cousin (every
cousin whose `cousin.toml` says `[agent] runner` is `sdk`, `fake`, `opencode`
or `tmux`, unless `[agent] auto_start = false`; a cousin with no
`[agent] runner` at all is never its) and, for a runner
cousin whose `[telegram]` is enabled and complete, its Telegram bridge
(`telegram:<slug>`, started after the runner, stopped and held with it; see
[telegram](telegram.md)), and each [plugin](plugins.md) service some cousin
enables (`plugin:<name>`, started before the runners and stopped after them;
`reload` adds, removes or restarts it, and `status` names a plugin that does
not load under `plugin`). It is a container's
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
once. On SIGTERM or SIGINT it stops the bridges first (together, 10 seconds
each), then the runners (together, 35 seconds
each: the runner's own 30 second stop, `runner.main.STOP_TIMEOUT_S`, plus 5),
then the plugin services (10 seconds each), then the loops daemon, then the
console, and exits 0. On
SIGHUP (or `reload`) it rescans `cousins/`: a new runner cousin is started, one
that is gone or left the runner lane is stopped, a bridge is added or removed
as `[telegram] enabled` changed and restarted when its token or operators did,
a `failing` child is started again, nothing else healthy is touched. `--no-console`, `--no-loops`,
`--console-host` (`127.0.0.1`), `--console-port` (8600) and `--loops-interval`
(30) shape what it runs; `--root R` picks the install, else `FRAMEWORK_ROOT`,
else the checkout you are in. One supervisor per install: it holds
`run/supervisor.lock`, and a second one exits 2. It sets umask 077 before
it writes or starts anything, and every child inherits it, so what the
install creates from then on is readable by its own user only. That closes
new files to other users on the host (and to a different uid, such as a
container's user reading a bind mount); every cousin runs as this one user,
so it does not separate cousins from each other.

The running supervisor answers on `run/supervisor.sock` (the `run/` directory
is private to its user). `status [--json]` lists every child with its state
(`running`, `backoff`, `failing`, `stopped`), pid, restarts and the reason,
then under `refused` every cousin it will not run because it has no `[agent]
runner` (with the line that says why and the way out; a [worker](glossary.md#worker) is not listed),
and under `config` every key 2.0.0 no longer reads that a cousin or the install
still carries (named, never fatal; `cousin-migrate tidy` removes them);
`start <slug>` starts a runner cousin's child (also one with `auto_start =
false`) or clears a `failing` one; `stop <slug>` stops it once its turn is done
(`--no-wait` answers once it is signalled) and holds it down until `start`,
across supervisor restarts too (`<home>/run/held`, see
[configuration](configuration.md)). A slug is only ever a runner cousin; the
console and the loops daemon are `--name console` and `--name loops`, held
until `start` or the next supervisor start. `reload` is SIGHUP. These exit 0
done, 1 when no supervisor is running, 2 when it refused (the line says why).
`run/supervisor.json` holds the same status, without `refused` and `config`,
for readers that want a file.

```
cousin-supervisor run --console-host 0.0.0.0
cousin-supervisor status
cousin-supervisor stop wren && cousin-supervisor start wren
cousin-supervisor start --name loops
```

`cousin-migrate` switches a runner cousin between the `sdk` and `tmux` kinds,
measures a cousin, and removes the keys 2.0.0 no longer reads. It does not
give a runner to a cousin that has none: 2.0.0 keeps no conversion (move each
cousin on the last 1.x release first, or by hand: [migrating](migrating.md#a-cousin-with-no-runner)).
`plan`, `apply` and `rollback` without `--to` exit 2 before doing anything:
for a cousin with no `[agent] runner` with the one refusal line every entry
point gives it, for a runner cousin with "name a kind with --to (sdk, tmux)".
Nothing else ever changes a cousin's kind: an upgrade or a merge leaves every
cousin on the kind its `cousin.toml` names.

`plan`, `apply` and `rollback` take `--to {sdk,tmux}`: this switches a runner
cousin between the `sdk` and `tmux` kinds (keeping its session, resumed with
`claude --resume`). Switching to `tmux` may need the operator to
accept the CLI's trust or bypass-permissions dialog once in the pane the
first time; `plan`/`apply` say where to look
(`tmux -S <root>/run/tmux.sock attach -t tmux-<slug>`, or the console's pane
view) and wait up to 10 minutes for it. `data/kind-switch.json` records a
switch; a `rollback --to <kind>` undoes it. See [runners](reference/runners.md)
for the kinds and [migrating](migrating.md). `check <slug> [--since ISO]
[--json] [--validate]` is the measure: inbox rows not done after an hour,
tool calls with no recorded result, recorder hooks that failed, an
unreadable inbox, the runner's CLI; `--validate` runs one smallest model
turn with the model, effort and account the runner carries. Exit 0 clean,
1 not.

`tidy <slug>|--all [--yes]` removes the keys 2.0.0 no longer reads (the list
and what each did is in [configuration](configuration.md#removed-in-200)):
`<slug>` from that cousin's `cousin.toml`, `--all` from every cousin's and
from the install's `config/harness.toml` and `config/hive.toml`, plus
`config/agent-cmd`. Without `--yes` it lists each key with its line and
writes nothing; with `--yes` it copies each file's prior bytes beside it
(`<home>/data/cousin.toml.pre-2.0.0`, `config/harness.toml.pre-2.0.0`,
never over an earlier copy), removes only those lines (comments, order and
line endings stay; a table left empty goes; a file it cannot edit line by
line is left whole and named), and moves `config/agent-cmd` aside. It also
stops a 1.x chat server still running for the cousin (found by
`data/chat-server.pid` or its `[chat] port`, and signalled only when its
command line is a chat server for that home), and removes the pid file. A
cousin with no `[agent] runner` is refused: `tidy` is not a conversion. `plan
--to` and `check` name the same keys (`warn 2.0.0` lines). Exit 0 done or nothing
to do, 1 when `--all` met a refused cousin or a file it left whole, 2 when
`<slug>` is refused.

```
cousin-migrate plan wren --to tmux
cousin-migrate apply wren --to tmux --yes
cousin-migrate check wren
cousin-migrate rollback wren --to sdk --yes
cousin-migrate tidy --all
cousin-migrate tidy --all --yes
```

## Memory

`cousin-memory` is the cousin's memory tool. Subcommands:

| subcommand | does |
|---|---|
| `search QUERY [--top N] [--collection memory\|notes\|harness] [--json]` | keyword search, plus semantic when embeddings are configured |
| `remember TOPIC FACT [--level L] [--cite SRC] [--derived-from ID]...` | one fact into raw memory with its truth level; `--derived-from` (repeatable) names the entry ids it was built from |
| `decide TOPIC DECISION REASONING [--level L] [--cite SRC] [--derived-from ID]... [--stdin]` | log a decision (and a raw copy of it); `--derived-from` as for `remember` |
| `why ID [--json]` | one raw entry by its id, what it was built from and what was built from it, and the mark that retired it, if any: one hop each way. Exit 1 for an id no raw entry has |
| `obsolete TOPIC --why REASON [--force] [--entry ID]` | retire a topic (L5): out of the [distilled](glossary.md#distilled) views, history kept; with `--entry`, retire one of its claims by its id and keep the topic |
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
| `propose-shared [--commit]` | nominate shareable memories for the [shared tier](glossary.md#shared-tier) |
| `trash [list]`, `trash restore ID` | list and restore memories removed in the console |
| `export --out PATH` | the home's memory as one tar.gz with a `MANIFEST.json` (size and sha256 per file): raw days, digests and archives, knowledge files, distilled, dreams, decisions, every file byte for byte; indexes left out. Never overwrites PATH. See [export and import](memory.md#export-and-import) |
| `import PATH [--home H] [--merge] [--yes] [--json]` | bring an export into a home: exit 2 on any sha256 mismatch, into a home that has raw memory without `--merge`, or for an archive of the same name with other bytes; `--merge` appends, as their bytes, the lines the home doesn't hold; a dry run unless `--yes`; distills afterwards |

Truth levels for `--level` are `operator`, `framework`, `tool`,
`conclusion` (the default), `hypothesis` and `obsolete`. `--level operator`
means "ana said this" and needs `--cite`. See [memory](memory.md).

```
cousin-memory remember "backups" "the NAS snapshot runs at 02:00" \
    --level operator --cite "chat #412"
```

`cousin-shared` works the shared tier: list and read canonical files, propose
a file (body on stdin), and promote or reject a proposal as a reviewer.
Subcommands: `list`, `read`, `diff`, `propose`, `promote`, `reject`,
`templates`.

```
cousin-shared promote house-rules.md --proposer wren --by ana
```

`cousin-shared templates` compares the files the install seeded once
(`config/law.md` and the [house rules](house-rules.md) in `shared/`) with the
templates the installed version ships, one line per file: `same`, `differs`,
`missing in install`, or `not shipped any more`. `-v`/`--full` prints a
unified diff under each file that differs, from the install's text to the
shipped one. It reads only: taking a change is yours to do by hand. Exit 0
when every file matches, 1 otherwise.

```
cousin-shared templates --full
```

`cousin-callback` is a cousin's library of moments worth calling back to.
Subcommands: `tag`, `list`, `search`.

```
cousin-callback tag "ana named the espresso machine Gustav" --category banter
```

`cousin-reason` writes and lists reasoning capsules: a conclusion with its
evidence and the alternatives you rejected. Subcommands: `capsule`, `list`.

`capsule` also takes `--truth-level` (default `conclusion`, the same levels
as `cousin-memory`'s `--level`) and `--topic`.

```
cousin-reason capsule --conclusion "keep backups for 30 days" \
    --evidence "audits need a month" --rejected "7 days" --confidence high
```

`cousin-sync-state` renders the live `## Open loops` section of STATUS.md
into `data/state.json`: the bare heading the handoff writes; a suffixed
`## Open loops (...)` heading is history and is not read.

```
cousin-sync-state --home cousins/wren
```

## Chat

`cousin-reply` stores a reply from the cousin to a person in its own chat
history, in-process. The body comes from stdin or `-m`; `--image` attaches a picture and
`--video` a video; `--reply-to ID` quotes the chat message with that id. It
has no positional text argument.

```
cousin-reply --user ana <<'EOF'
Done. The notes are in notes/2026-09-18-budget.md.
EOF
```

`cousin-chat` sends a message to another cousin (or an external peer), or
lists who is addressable. A local runner cousin (`[agent] runner` set, `tmux`
kind included) is written directly, in-process: its chat history and inbox.
A local cousin with no runner kind is refused with the one line
`lane_refusal` gives, and nothing is sent (exit 1). `list` prints each
cousin's kind (`kind=sdk`, `none` for no runner, `worker` for a
[worker](glossary.md#worker)). Subcommands: `send SLUG TEXT [--from NAME]`,
`list`. To a local cousin, `--from` is the sending cousin's own name or
slug; any other name (the operator's, another cousin's) is refused with
exit 2. To an external peer it may be a free-form display name, which the
peer's own install checks.

```
cousin-chat send kestrel "the greenhouse report is ready" --from Wren
```

`cousin-chat-import` imports a cousin's chat history from the previous
framework. Stop the cousin first: it refuses while the cousin's runner holds
its lock. A second run is refused unless `--force`.

```
cousin-chat-import wren --old-home /srv/old/cousins/wren/files
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
for long text). `list --state open|closing|closed` filters by state;
`--json`, before the subcommand, prints JSON.

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
`list`, `show`, `tail [-f]`. `start` also takes the form `start KIND [options]
-- TITLE [CMD...]`, every option before the `--` and the title after it, so a
title that looks like a flag is only a title. Its options are `--desc`,
`--json`, and one of `--log PATH` or `--home-log REL`, a log path relative to
the cousin's home and confined to it (absolute, `~`, `..` and `.secrets` are
refused).

```
cousin-job start shell "rebuild the index" -- cousin-memory reindex
cousin-job start shell --home-log logs/reindex.log -- "rebuild the index" cousin-memory reindex
```

A cousin's `job` tool does the same with `run` (`title`, `argv` as an array,
optional `desc` and `log`); see [MCP tools](mcp.md#what-a-cousin-gets).

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

`cousin-health` prints what has been failing and for how long: the loops
daemon's per-component record (`data/health.json`), failing components
first with their count of consecutive failures, since when and the last
error, then a count of the ok ones, plus any supervisor child that is not
`running`. `--all` lists the ok components too, `--json` prints everything.
Exit 0 when nothing is failing, 1 when something is, 2 on bad usage. See
[operations](operations.md#health).

```
cousin-health
cousin-health --json
```

`cousin-schedule` queues a one-shot prompt for a future time. Subcommands:
`add WHEN PROMPT` (`in 30m`, `tomorrow 06:30`, or an ISO date), `list
[--all]`, `cancel ID`, `tick`. A cousin holds at most 20 pending; `add`
refuses the 21st (exit 2).

```
cousin-schedule add "tomorrow 09:00" "Check that the backup ran."
```

`cousin-tracker` is the install-wide list of work in flight. Subcommands:
`add`, `update`, `state ID open|active|blocked|done|dropped`, `list`, `show`,
`delete`. Post a status update with `update ID --add-note TEXT`, which
appends a dated, signed line to the notes; `update ID --notes TEXT`
replaces the whole notes text, spec and earlier lines included. Every
change is kept in the item's history (`show ID --history`, oldest first),
so a replaced text can be read back. See
[the tracker](jobs-and-loops.md#the-tracker).

```
cousin-tracker add "move the photo archive" --domain infra --tag q4
cousin-tracker update 1 --add-note "half copied, rest tonight"
cousin-tracker show 1 --history
```

## Remote cousins

`cousin-hive` runs a standalone queen and manages node tokens. Subcommands:
`serve`, `mint`, `revoke`, `forget`, `nodes`, `send`, `recall`,
`import-legacy`. `serve` takes `--host` (0.0.0.0), `--port` (8101) and
`--checkin-seconds`; `send` and `recall` take `--queen URL` and the token
from `--token-file PATH` (`-` for stdin) or `HIVE_TOKEN` (`--token T` is
deprecated: it puts the token on the command line);
`import-legacy` takes the old queen's `--tokens` file. Most installs use the
console as the queen instead. See
[remote cousins](remote-cousins.md#cousin-hive) for every flag.

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
creates a login with `adduser NAME`. `--secure-cookie` marks the session
cookie Secure, for a console behind TLS; `--tmux-bin` names the tmux binary
its pane view runs for a `tmux`-kind cousin (default `tmux`). See
[console](console.md).

```
cousin-console --host 0.0.0.0 --port 8600
cousin-console adduser ana
```

`cousin-mcp` is the MCP server a cousin's harness starts (stdio). By hand you
use it to check the registry (`--selftest`, or `--list-tools` for the raw
schemas), call one tool (`--call TOOL JSON`), find out why the server failed
(`--last-connection`), print the SDK's supported protocol versions
(`--versions`), or record the harness's approval of a cousin's `.mcp.json`
with `approve SLUG`. `--registry` points it at a registry TOML other than the
cousin home's (else the install's `config/mcp-registry.toml[.example]`). See
[mcp](mcp.md).

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

`cousin-doctor` checks the install for what to fix by hand and prints the
fix; it changes nothing. `cousin-doctor` runs every check, `cousin-doctor
homes` one; `--cousin SLUG` limits each check to that cousin; `--json` prints
the results as JSON. Exit 0 all ok, 1 something to fix, 2 bad usage or no
such cousin. The checks:

- `homes`: every cousin home (a directory under `cousins/` with a
  `cousin.toml`) whose mode has a group or other bit, each with the line that
  closes it, `chmod 700 <home>`. `cousin-spawn` makes new homes 0700; this
  finds the ones an older release made. Every cousin runs as one user, so a
  0700 home is closed to other users on the host and to anything running as
  another uid (a container's user, a second account), not to the other
  cousins. When a directory above the homes is already closed to group and
  other (a 0700 `$HOME`), the check names it: the homes are out of other
  users' reach today, and the chmod keeps them so if that directory opens.

```
cousin-doctor
#   WARN  homes: 1 of 2 cousin homes open to group or other
#         to fix, run:
#           chmod 700 /srv/cousins-framework/cousins/wren    # now 0755
#         scope: every cousin runs as this one user, so 0700 homes close them ...
```

- `identity`: each line of a cousin's identity text that contradicts a fact
  the framework owns, with the file, the line number, the line, the fact and
  where it comes from, and the fix. The identity text is the authored part of
  `CLAUDE.md` (the title line, the Identity and Voice sections and everything
  below the template marker, less any paragraph that is the template's word for
  word: the part the runner puts in the system prompt), `self-portrait.md`, and
  the live open-loops section of `STATUS.md`. Four rules:
  - `lane`: on a runner lane (`[agent] runner` is sdk, opencode or tmux), a
    `cousin-*` command that a served tool replaces: `cousin-reply` is the
    `reply` tool, and the rest come from the cousin's MCP registry (each
    enabled tool command's CLI and subcommand, so `cousin-chat send` is `send`
    and `cousin-memory decide` is the `memory` tool's `decide`). A command whose
    tool the registry does not serve is the fallback and is not reported.
  - `billing`: a phrase claiming a subscription ("on a personal subscription",
    "no company key") on an API-key account, or an API key ("on the company
    API key", "billed per token") on a subscription login. The account is the
    one the runner uses: `[agent] account` and `config/accounts.toml`, or
    `[agent] api_key_file`.
  - `peer`: "Wren cannot message me" (or reach, contact, ...) about a
    registered cousin whose registry serves `send`, when both are peer-visible
    and this cousin takes delivery on a runner lane.
  - `tool`: an `mcp__cousin__<name>` the cousin's registry does not serve.

  A line is not reported when the phrase is negated or dated in its sentence
  (not, never, no longer, used to, was, replied, logged), names a fallback or
  a condition, or claims both ways. A `lane` line about a command's syntax
  (its `--help`, arguments, flags, usage, a CLI's shape) is not reported
  either; "verify" or "shape" in their ordinary sense do not make a line one.
  A line that names two commands to state a distinction (only, vs, not, but
  ...) or says what "reaches you via" a command is reported with a fix that
  keeps the distinction: "rewrite the distinction in tool terms:
  `cousin-reply` -> the `mcp__cousin__reply` tool, `cousin-chat send` -> the
  `mcp__cousin__send` tool". When the line gives a command to the operator or
  to peers ("`cousin-reply` = operator only", "for peer cousins"), the fix
  keeps the audience: "rewrite the distinction in tool terms: operator: the
  `mcp__cousin__reply` tool; peers: the `mcp__cousin__send` tool". A fact
  that cannot be read (an unknown account, a registry that does not parse)
  skips its rule with a note.

  A long line is shown cut to 200 characters around the matched phrase, with
  `...` on each side cut; `--json` gives the phrase itself as `match`. The
  summary counts findings per rule; when one claim is repeated (the same rule
  and phrase in one cousin, say in `CLAUDE.md` and in the self-portrait), it
  also counts the distinct claims: `billing 4 (1 distinct)`.

```
cousin-doctor identity --cousin wren
#   WARN  identity: 1 line contradicts the framework in 1 of 1 cousin (lane 1)
#         cousins/wren/CLAUDE.md:5 [lane] Log choices with `cousin-memory decide --stdin`.
#           fact: wren runs on the sdk runner (cousin.toml [agent] runner), where ...
#           fix:  use the `mcp__cousin__memory` tool's `decide` command instead of `cousin-memory decide`
```

`cousin-tool-surface` writes `data/tool-surface.md`, one line per command from
its `--help`, a list a cousin can read instead of re-discovering its tools.
`--root` picks the install (else `FRAMEWORK_ROOT`); `--bin` is a directory of
installed `cousin-*` wrappers to run instead of each entry point through this
interpreter.

```
cousin-tool-surface
```

`cousin-cache-audit` reads the harness transcripts and reports the prompt
cache hit rate and the files that probably broke it. Needs `transcripts_dir`
in `config/harness.toml`. `--days` is the window (default 7, 0 for all);
`--home` picks the cousin; `--json` prints the report as JSON; `--diagnose`
lists each suspect invalidator with its turn pair.

```
cousin-cache-audit --days 7 --diagnose
```

`cousin-version` prints the framework version (and commit in a git checkout),
or bumps it in `pyproject.toml`: the checkout you run it in (a git worktree
included), else the one running; `--pyproject PATH` reads or bumps another.

```
cousin-version bump patch
```

`cousin-upgrade --dry-run` plans moving the install to another release and
changes nothing: no file is written, the code is not switched, nothing
restarts. The target is `--to REF` (a tag or any ref), else the newest
`v<major>.<minor>.<patch>` tag in version order; a target older than the
running version is refused unless `--to` names it. Every shipped text is read
from git at the two refs, so the plan holds before the checkout moves. It
reports the running version and the target, the CHANGELOG headings and bold
leads in between, whether pyproject's dependencies changed (then `pip install
-e .` again), the seeded files against the target's templates (as
`cousin-shared templates`), and for each home: what the registry sync would
add, which framework values it would migrate (an exact value an earlier
release shipped; a value the operator edited is never changed), which entries
the target retired (reported, never removed), whether `.mcp.json` would be
rewritten, whether a `policy.toml` is there (operator policy, never touched),
and the CLAUDE.md diff against the target's template. A home with no registry
is listed as such. A home's starting point is the release
`data/template-sync.json` records as its last registry sync, else the running
version. Then the code switch `--switch` would do (the fetch, the checkout,
the reinstall, the check) with how to roll it back, and last the restarts, in
order: the loops daemon, the console, each runner (from `cousin-supervisor
status`, or from the configuration when no supervisor answers), the caller's
own runner last and detached, each with the release it says it runs now and
how its restart will be checked. A dirty checkout is reported, not refused.
`--json` prints the same
plan as JSON, `-v`/`--full` prints each diff, `--root` picks the install and
`--checkout` the git checkout to read releases from (default: the one
running), `--home SLUG` (repeatable) limits the homes. Exit 0 the plan was
computed, 1 it could not be (no release tag, not a git checkout), 2 refused.

`cousin-upgrade --apply-homes` applies the homes' part of the same plan: each
home's `mcp-registry.toml` and `.mcp.json` are brought to the target. The
code is not switched and nothing restarts (that is `--switch`). It prints the homes' plan and asks `apply to N homes? [y/N]`; `--yes` skips
the question, and without a terminal to ask at, `--yes` is required (else
exit 2, nothing written). Per home:

- The registry is copied to `data/mcp-registry.toml.pre-<version>` before
  the first write; a copy already there is kept and the new one gets a `.2`
  (`.3`, ...) suffix.
- The structural sync adds the tables and keys the release has and the home
  lacks, and migrates framework values (an exact value an earlier release
  shipped). A value the home or the operator set is never changed. The
  entries the release retired are reported and kept; `--prune-retired`
  removes them (and only them).
- The file is replaced atomically, then must parse strictly and build every
  tool definition. If it does not, the old bytes go back and the home is
  reported failed with the reason.
- `.mcp.json` is refreshed when it would change (its `cousin` entry, as
  `cousin-spawn <slug> --repair-settings` does; other servers are kept).
  CLAUDE.md is reported only; `cousin-spawn <slug> --sync-template` shows its
  diff and `--apply` writes it.
- `data/template-sync.json` records `{"to": "<version>", "ref": "<commit>",
  "at": <epoch seconds>, "registry": "applied" | "in-step" | "failed: <why>"}`,
  a failed home also `"from"`, the commit it was planned from. The next plan
  starts the home there: an applied home plans as in step, a failed one is
  planned again.

A home with no registry is skipped. The report says per home: applied (keys
added, values migrated, retired entries reported or pruned, the backup), in
step, skipped or failed. `--home SLUG` limits the set, `--json` prints the
plan and the results. Exit 0 all done, 1 a home is left for a person (a
failed registry, a `.mcp.json` that could not be refreshed), 2 refused (no
`--yes` and no terminal, the question answered no, an unknown `--home`).

`cousin-upgrade --switch` moves the code to the target and restarts every
process on it. It runs `git fetch --tags` first (`--no-fetch` skips it), then
computes the plan, prints the switch and the restarts, and asks `switch <checkout>
to <version> and restart N processes? [y/N]` (`--yes` skips the question and is
required without a terminal). Refused before anything moves (exit 2): tracked
changes in the checkout; a dependency change without `--deps`; a venv whose
python does not import `cousin_lib` from the checkout being upgraded. Then:

1. `data/upgrade.json` (under the root) records where it started: the
   version, the commit and the branch HEAD was on.
2. `git checkout --detach <target commit>`. The branch HEAD was on is not
   moved.
3. `<python> -m pip install --no-deps -e <checkout>` with the python of the
   venv the command runs in (`--deps` lets pip install the dependencies too).
4. A fresh interpreter of that venv must import `cousin_lib` at the target
   version from the checkout.
5. The restarts, in order (`--no-restart` stops after step 4): the loops
   daemon, the console, each runner, each through the supervisor (`stop`, then
   `start`; a runner's Telegram bridge stops and starts with it). Each one
   must come back `running` on a new pid that says it runs the target
   (version, commit and checkout, from `run/versions/<name>.json`, which the
   console, the loops daemon and each runner write at their start) within
   `--verify-timeout` (90 s). A target older than that file is checked by
   its new pid only.

A process that already says it runs the target is not restarted, so a second
run does only what is left. A runner mid-turn is waited for, up to
`--idle-timeout` (600 s); still busy, it is left `pending`. A runner held down
by a stop, or `failing`, is left as it is: it runs the new code when it is
started. With no supervisor, or for a process that is not its child (its own
unit), the restart is `by hand`. The caller's own runner (a cousin running
the command) is restarted last and detached, once its turn is over: a
`systemd-run --user` unit when there is a user manager, else a process in its
own session (output in `data/upgrade-restart.log`), running `cousin-upgrade
--restart --only runner:<slug>` with absolute paths and `FRAMEWORK_ROOT` and
the venv's `bin` on `PATH` set explicitly, since a transient unit has
neither. The first restart that fails stops the run; the rest are `not
reached`, and the report says how to roll back: `cousin-upgrade --switch --to
<from commit> --yes`, or by hand `git -C <checkout> checkout <branch>`, the
same pip line, then the restarts. Every step and every restart is recorded in
`data/upgrade.json` as it goes. From inside a cousin, run it as a background
job: the waits can outlast a tool call.

`cousin-upgrade --restart` does the restarts alone, on what the checkout
holds now (its HEAD and version): what a switch left `pending` or `by hand`,
or a runner it stopped and did not get back. `--only NAME` (repeatable:
`loops`, `console`, `runner:<slug>`) limits it. It asks like `--switch`.

Exit for `--switch` and `--restart`: 0 all done (the caller's detached
restart counts as done; `data/upgrade.json` records how it went), 1 a step or
a restart failed, or something is left for a person (`pending`, `by hand`),
2 refused.

```
cousin-upgrade --dry-run
cousin-upgrade --dry-run --to v3.17.0 --full
cousin-upgrade --apply-homes --to v3.18.0 --home wren --yes
cousin-upgrade --switch --to v3.18.0
cousin-upgrade --restart --only runner:wren --yes
```

`cousin-gate` scans a tree you are about to publish for private addresses,
home paths, secret shapes, binaries and denylisted terms. `--git-visible`
scans only what git would publish (tracked files, and untracked ones
`.gitignore` doesn't exclude) instead of the whole tree; use it on a checkout
that also hosts a live install. `--mode triage` prints a one-line manifest
per hit instead of the gate's file:line report. Exit 0 clean, 1 a hit (gate
mode only).

`--commits RANGE` scans what a push publishes besides the tree: every
commit message in the range (the same names, addresses, paths and secret
shapes), any `Co-authored-by` trailer, and, with `--expect-author 'NAME
<EMAIL>'`, any author or committer who is somebody else. Run it before a
push; exit 2 when git refuses the range.

```
cousin-gate --root /tmp/publish --denylist ~/private/denylist.txt
cousin-gate --root . --denylist denylist.txt --git-visible
cousin-gate --root . --denylist denylist.txt --commits origin/main..HEAD \
    --expect-author 'Ana Example <ana@example.invalid>'
```

## Removed in 3.0.0

| removed | instead |
|---|---|
| `cousin-auth` (and the console's auth control) | a runner cousin authenticates through its `[agent] account`; `cousin-account` and the console's accounts page manage the accounts ([configuration](configuration.md#accountstoml)) |
| `cousin-ui` (a retired alias of `cousin-console`) | `cousin-console`, same flags |
| `cousin-spawn --resume` | none: a runner resumes its own session (`data/runner-session.json`) at every start; `cousin-spawn <slug> --start` starts it |
| `cousin-migrate plan`/`apply` `--account` and `--validate`, `rollback --force` | none: they belonged to the legacy migration, which 2.0.0 does not run; a `--to` switch runs on the cousin's own `[agent] account` |
| `cousin-flip --confirm` (and `confirm` in `POST /api/cousins/<slug>/flip`) | none: a rollover's new generation was never asked to announce itself; the console's flip events and `cousin-flip`'s JSON say when it is done |
| `cousin-reincarnate --timeout` (and the console's bequest wait: `timeout` in `POST /api/cousins/<slug>/reincarnate`, `timeout` and `timeout_range` in `GET /api/lifecycle/modes`) | none: the bequest rides the flip's own handoff request, under the runner's handoff deadline |
| the `chat server:` line of `cousin-migrate check` (and `chat`, `chat_ok` in its `--json`) | none: no cousin runs a chat server of its own |

## Removed in 2.0.0

No local cousin runs a chat server of its own in 2.0.0 (a remote hive
node runs its own small chat endpoint, see
[remote cousins](remote-cousins.md)): the console answers chat
in-process over the cousin's own store, the runner's inbox carries delivery,
and the hive carries a node's chat through the queen.

| removed | instead |
|---|---|
| `cousin-chat-server` | none: the console, `cousin-chat`, `cousin-reply` and the Telegram bridge call the chat API in-process |
| `cousin-chat-watchdog` (and its timer) | none: there is no chat server to keep up |
| `cousin-spawn --port` | none: a cousin has no chat port |
