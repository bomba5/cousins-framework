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
uses); `--repair-settings` rewrites an
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

`cousin-flip` ends the cousin's current generation and starts the next one on
a fresh session with a boot packet. `--dry-run` runs the checks only;
`--confirm` asks the new generation to post one line when it is oriented.

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

## Memory

`cousin-memory` is the cousin's memory tool. Subcommands:

| subcommand | does |
|---|---|
| `search QUERY [--top N] [--collection memory\|notes\|harness] [--json]` | keyword search, plus semantic when embeddings are configured |
| `remember TOPIC FACT [--level L] [--cite SRC]` | one fact into raw memory with its truth level |
| `decide TOPIC DECISION REASONING [--level L] [--cite SRC] [--stdin]` | log a decision (and a raw copy of it) |
| `obsolete TOPIC --why REASON [--force]` | retire a topic (L5): out of the distilled views, history kept |
| `recall [KEYWORD] [--last N]` | past decisions, filtered |
| `activity TEXT` | set the "what I'm doing now" line |
| `distill [--max-lines N]` | rebuild `memory/distilled/` from raw |
| `consolidate` | list recurring topics, then distill |
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

`cousin-reply` posts a reply from the cousin to a person on its own chat
server. The body comes from stdin or `-m`; `--image` attaches a picture and
`--video` a video. It
has no positional text argument.

```
cousin-reply --user ana <<'EOF'
Done. The notes are in notes/2026-09-18-budget.md.
EOF
```

`cousin-chat` sends a message to another cousin (or an external peer), or
lists who is addressable. Subcommands: `send SLUG TEXT [--from NAME]`, `list`.

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
[--interval S] [--ticks N]` (the daemon), `status`, `requests`, `fire SLUG
LOOP`. See [jobs and loops](jobs-and-loops.md).

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
use it to check the registry, call one tool, or record the harness's approval
of a cousin's `.mcp.json` with `approve SLUG`. See [mcp](mcp.md).

```
cousin-mcp --call memory '{"command": "search", "query": "backup"}'
```

## Maintenance

`cousin-backup` snapshots one cousin's databases (through `VACUUM INTO`),
`memory/` and core markdown into `<dest>/<slug>/<date>/`.

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
