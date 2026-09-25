# Cousins

What a [cousin](glossary.md#cousin) is, how to make one, and how to run, change and retire it.
Read this after the quick start, when you want to know what `cousin-spawn`
actually built and what you can do with it.

## What a cousin is

A cousin is an agent session (Claude Code, in the setup I use) with a home
directory that holds everything it is, and a **kind**, `[agent] runner` in
`cousin.toml`, that says how its agent loop runs: `sdk` and `opencode` run
under `cousin-runner`, with no tmux session at all; `tmux` also runs under
`cousin-runner`, but drives the host's interactive Claude Code in a tmux pane
on the framework's own socket; a cousin with no `[agent] runner` at all is on
the **legacy tmux [lane](glossary.md#lane)**, its agent running in its own tmux session, typed
into by its chat server (the console calls this lane `tmux-legacy`). This
page mostly describes the legacy lane, which
most of `cousin-spawn`'s output still assumes; where a [runner](glossary.md#runner) cousin (any of
`sdk`, `opencode` or `tmux`) works differently, it says so. See
[runners](reference/runners.md) for the kinds themselves and
[agent-loop-runner](design/agent-loop-runner.md) for the design.

Every cousin's home directory:

```
cousins/wren/
  cousin.toml          slug, name, role, chat port, operator, runtime, loops...
  CLAUDE.md            its identity, rendered from the template once, then yours to edit
  STATUS.md            open loops, the thing a new session anchors on
  MEMORY.md            the memory index
  memory/              raw entries, distilled views, memory files
  notes/               notes it writes
  data/                decisions log, handoffs, checkpoints, boot packets, pid files
  scripts/             its own scripts
  mcp-registry.toml    the tools it gets over MCP
  .mcp.json            tells the harness to start cousin-mcp
  .claude/settings.json  its harness hooks
```

A legacy-lane cousin has its own chat server on its own port (8090 to 8200
by default). People and other cousins talk to it through that server, which
types incoming messages into the tmux session. A runner cousin needs no chat
server: its chat history and [inbox](glossary.md#inbox) are written directly, and the console
reads its chat store itself (`cousin-migrate` still starts one for a cousin
it moves off the legacy lane). The agent answers with `cousin-reply`, and messages another
cousin with `cousin-chat send`. See [chat](chat.md) and [memory](memory.md)
for those two halves.

A slug is a cousin if `cousins/<slug>/cousin.toml` exists. There is no other
registry: the filesystem is the fleet.

## Spawning one

Before a cousin can start you need `config/agent-cmd`: one line, the command
that runs your agent. It can carry three placeholders the framework fills in
at every start:

```
printf '%s\n' "$HOME/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}" \
    > config/agent-cmd
```

`--dangerously-skip-permissions` is what lets a cousin work unattended, and it
means the cousin can do anything your account can; see
[install](install.md#4-claude-code).

`{session_id}` gets a fresh id per session (and is saved to `cousin.toml`),
`{model}` and `{effort}` come from the cousin's `[runtime]` or the install
defaults (see [models and effort](#models-and-effort)). What the agent is
allowed to do is your choice, expressed in that line; the framework doesn't
pick a binary or a permission mode for you. [install](install.md) has the
full setup.

Then:

```
cousin-spawn wren --name Wren \
    --role "keeps the house notes" \
    --voice "Short and plain. Answers first, explains after. Says when it does not know." \
    --operator ana \
    --start
#   -> created wren at <checkout>/cousins/wren (chat port 8090)
#      started wren
```

Every option:

| option | what it does |
|---|---|
| `slug` | lowercase, `^[a-z][a-z0-9_-]{1,31}$`; the directory, the tmux session name, the chat route |
| `--root` | the framework root (the checkout); else `FRAMEWORK_ROOT`, else the current directory if it is one |
| `--name` | display name; default is the slug capitalised |
| `--role` | one line; required to create |
| `--role-paragraph` | a paragraph for the Identity section of CLAUDE.md; default is the role |
| `--voice` | how the cousin writes; required to create |
| `--port` | chat port; default is the first free one in 8090-8200 not claimed by any cousin |
| `--operator` | the person it answers to; written to `[operator] name` and into its MCP registry |
| `--model` | `[runtime] model` |
| `--effort` | `[runtime] effort`: `low`, `medium`, `high`, `xhigh`, `max` |
| `--heartbeat` | `[heartbeat] context_beat_seconds` (default 3600) |
| `--memory-scope` | `[memory] scope`: `private` (default) or `shared` (may propose memories to the [shared tier](glossary.md#shared-tier); the retired `both` is read as `shared`) |
| `--runner` | `[agent] runner`: `sdk`, `fake`, `opencode` or `tmux` puts the cousin on `cousin-runner`, started by `cousin-supervisor` instead of `cousin-spawn --start`'s own tmux session (the `tmux` kind still uses tmux, but a pane on the framework's own socket, driven by the runner, not the legacy lane's session); absent, `COUSIN_DEFAULT_RUNNER` applies, and unset means the legacy tmux lane |
| `--account` | `[agent] account`, one of `config/accounts.toml`'s (or `host`); a runner cousin only; absent, `COUSIN_DEFAULT_ACCOUNT` applies to a runner cousin |
| `--start` | start it after creating; on an existing cousin without `--role`/`--voice`, only start it. A runner cousin is started by asking the running `cousin-supervisor` (no `config/agent-cmd`, no tmux); with no [supervisor](glossary.md#supervisor) the start fails, exit 1 |
| `--resume` | with `--start` on an existing cousin: resume its last session (`config/harness.toml [agent.resume]`) instead of a new one; falls back to a new session when that isn't possible. What the start-at-boot unit uses |
| `--sync-template` | create nothing; show how an existing cousin's CLAUDE.md framework part differs from the current template (see [the CLAUDE.md template](#the-claudemd-template)) |
| `--apply` | with `--sync-template`: write the sync |
| `--repair-settings` | create nothing; rewrite an existing cousin's `.claude/settings.json` and the `cousin` entry in `.mcp.json` |

An option you leave out writes no key, so the default applies and can change
later without touching the cousin.

Exit codes: 0 created (and started), 1 created but the start failed (the home
is kept; fix the cause and run `cousin-spawn wren --start`), 2 nothing was
done (bad input, slug taken, no `config/agent-cmd`, tmux or the agent missing).
A failed create removes whatever it made, so it never leaves a half-made home
squatting the slug. A directory under `cousins/` with no `cousin.toml` is
reported as an orphan; remove or finish it by hand.

The console's spawn dialog sends the same fields and then starts the cousin;
see [console](console.md#spawning-a-cousin). For a cousin on another machine,
see [remote cousins](remote-cousins.md).

What spawn writes besides the identity files:

- `mcp-registry.toml` and `.mcp.json`, so the harness starts `cousin-mcp` and
  the cousin gets its memory, send, job and schedule tools ([mcp](mcp.md)).
  With Claude Code you may still need `cousin-mcp approve wren` to record the
  approval in `~/.claude.json`.
- `.claude/settings.json` with the harness hooks: the session banner,
  pre-compact and stop checkpoints, and the job-tracking hook (see
  [session hooks](#session-hooks)). Keys it doesn't own are kept.

`cousin-spawn wren --repair-settings` rewrites those two for a cousin made by
an older release, or after you move the checkout. It is safe to repeat.

## The CLAUDE.md template

Every new cousin's `CLAUDE.md` comes from `templates/cousin-CLAUDE.template.md`.
Nothing else writes one. Spawn fills in six placeholders:

| placeholder | from |
|---|---|
| `{{NAME}}` | `--name` |
| `{{SLUG}}` | the slug |
| `{{PORT}}` | the chat port |
| `{{ROLE_ONE_LINE}}` | `--role` (the title line) |
| `{{ROLE_PARAGRAPH}}` | `--role-paragraph`, else the role |
| `{{VOICE_GUIDE}}` | `--voice` (the `## Voice` section) |

HTML comments in the template are notes for whoever edits the template and
are stripped. If any `{{` is left after rendering, spawn fails before writing
anything. That's why `--voice` is required: a cousin with an empty voice
section will make one up the first time it boots without its other identity
layers.

The template carries what every cousin needs from day one: who it is, how to
tell a message from a person apart from one from another cousin (and which
command answers each), the tools, memory and decision logging, session
bookends, hard rules, the voice, and a note that the console renders Markdown
and Mermaid. It names no person: it points at `[operator]` in `cousin.toml`.
The last line is a marker; put anything specific to this cousin below it.

Below the marker, `CLAUDE.md` is yours: edit it in any editor or in the
console's inspector (which backs up the old version to
`data/claude-md-backups/`). A running session doesn't reread it: the change
lands at the next start or [flip](glossary.md#flip). `examples/wren/` is a complete rendered
cousin you can read.

Above the marker is the framework part, and it follows the template. Every
start and flip syncs it before the agent reads the file, so a template change
reaches every cousin, not only the ones spawned after it:

- `## Identity` and `## Voice` stay the cousin's own (the template renders
  them from text given at spawn and kept nowhere else).
- Every other framework section gets the current template text, filled in
  with the cousin's name, slug, port and role from `cousin.toml`. An edit you
  make there is replaced at the next start: put your own rules below the
  marker.
- A section above the marker that the template doesn't have is kept, at the
  end of the framework part.
- Below the marker nothing changes, except a leftover copy of a framework
  section that is word for word the template's, which is removed.
- The old file goes to `data/claude-md-backups/` whenever the sync changes
  it.
- The cousin's `mcp-registry.toml` is brought up to the shipped registry at
  the same time, additively: a table the shipped one has and the cousin's
  lacks is appended, and a key inside a table they share is added to it.
  A value the cousin already has is never changed, so an edited description
  or `argv` survives; a shipped edit to an existing value does not arrive.

To see what the next start would change, or to apply it now:

```
cousin-spawn wren --sync-template           # the diff, nothing written
cousin-spawn wren --sync-template --apply
```

## Starting, stopping, restarting

```
cousin-spawn wren --start          # start an existing cousin (no-op if running)
```

This is the legacy lane's start. It creates the tmux session named in
`[chat] tmux_session`, with the home as working directory and `COUSIN_HOME`
set, runs `config/agent-cmd` in it behind a small launcher that applies the
auth mode, and starts the chat server if nothing answers on its port. The
chat server's pid goes in `data/chat-server.pid` and its log in
`data/chat-server.log`.

A runner cousin (`[agent] runner` is `sdk`, `fake`, `opencode` or `tmux`)
starts and stops a different way: `--start` (or the console) asks the running
`cousin-supervisor` to start its `runner:<slug>` child instead, and it needs
`cousin-supervisor run` up first (`NoSupervisor` otherwise). None of
`config/agent-cmd`, the tmux commands above or `data/chat-server.pid` apply
to it. Stop and restart for it are `cousin-supervisor stop|start <slug>`, or
the console; a stop holds it down (`<home>/run/held`) across a supervisor
restart until the next start. See [commands](commands.md#running-cousins)
and [runners](reference/runners.md).

Stop and restart for the legacy lane are in the console (card and inspector
buttons). Stop is a clean stop: the cousin writes its handoff and saves what
it learned, the transcript is mined, and the next start boots a fresh
session on a packet built from all that
([lifecycle](reference/lifecycle.md#a-clean-stop)). There's no stop command
yet; an immediate stop by hand is:

```
tmux kill-session -t wren
kill "$(cat cousins/wren/data/chat-server.pid)"
```

Restart is an immediate stop, a short pause, start. The conversation in the
session is gone; the next session starts fresh from CLAUDE.md and whatever
the cousin wrote to disk. To keep the thread across a restart, flip instead
(below), or switch auth modes, which resumes the same session.

The chat server has no supervisor of its own. `cousin-chat-watchdog`, run from
a timer, starts a missing one for any running cousin. See
[operations](operations.md).

To get rid of a cousin, use "dismiss" in the console: it stops it, archives
the home (without `.secrets/`) to `data/dismissed/<slug>-<timestamp>.tar.gz`
and removes it.

## Models and effort

`{model}` and `{effort}` in `config/agent-cmd` render from the cousin's own
`cousin.toml`, else the install default:

```toml
# cousins/wren/cousin.toml
[runtime]
model = "claude-sonnet-5"
effort = "medium"
```

```toml
# config/harness.toml
[agent]
default_model = "claude-opus-5"
default_effort = "high"
models = ["claude-opus-5", "claude-sonnet-5"]   # what the spawn dialog offers
```

If the command has a placeholder that neither file fills, the start fails and
names both files; nothing guesses a model for you. Effort is one of `low`,
`medium`, `high`, `xhigh`, `max`. Without `models` the spawn dialog offers a
short built-in list.

Both are read at start, so a change needs a restart or a flip. The chat
header's effort select and the spawn dialog write these keys; for the model
after spawn, edit `cousin.toml`.

A runner cousin has no `config/agent-cmd` and no `{model}`/`{effort}`
placeholders: it reads `[agent] model` and `[agent] effort` in `cousin.toml`
directly (its `[runtime]` is the legacy lane's and is not read by
`cousin-runner`). `cousin-spawn --model`/`--effort` write there instead, for
a cousin created with `--runner`. See [`[agent] runner`](configuration.md#agent-runner).

## Auth: login or API key

This is the legacy lane's auth switch: a runner cousin (`sdk`, `opencode` or
`tmux`) instead names an account in `config/accounts.toml` (see
[accounts.toml](configuration.md#accountstoml)); `[runtime] auth` is not read
for it.

Each cousin's agent logs in one of two ways, set in `cousin.toml [runtime]
auth` and applied at every start (so flips and restarts keep it):

- `claude` (the default): the harness's own login. The key variable and the
  config-dir variable are removed from the agent's environment, so an
  `ANTHROPIC_API_KEY` exported somewhere upstream can't quietly move the
  cousin onto metered billing.
- `api_key`: a key from the cousin's own `<home>/.secrets/api-key.env` (one
  line, `ANTHROPIC_API_KEY=<key>`; the file must be mode 600 and the
  directory 700, or the start is refused). The launcher reads it at exec
  time and hands it over in the environment only, so it never appears in an
  argv, tmux's included, or in a log.

```
cousin-auth wren                          # current mode and key state
#   -> wren: auth claude (modes: claude, api_key)
#      key: not set (.../cousins/wren/.secrets/api-key.env)
cousin-auth wren --key-stdin < wren.key   # write the key file (a tty prompts without echo)
cousin-auth wren api_key                  # switch; a running agent restarts on the same session
cousin-auth wren claude --no-restart      # switch back, apply at the next start
```

Why the key mode needs more than the key: Claude Code with its own login
present and `ANTHROPIC_API_KEY` set bills the login. So `api_key` mode also
points the agent at an isolated config directory,
`data/harness-api-key-config/` under the root, via `CLAUDE_CONFIG_DIR`. It
links everything in `~/.claude` except the login and account files, plus a
copy of `~/.claude.json` with the account keys removed. A start refuses if
that directory holds a login. Transcripts are linked through, so both modes
share the same sessions. The names involved (variables, files, account keys)
are in `config/harness.toml [auth.api_key]`; copy
`config/harness.toml.claude-code.example` and they're filled in. Without that
table, `api_key` mode is refused.

A switch checks everything first (key file, isolated directory, whether a
resume is possible) and changes nothing if one fails. It restarts a running
agent with the resume rule in `config/harness.toml [agent.resume]` (for Claude
Code, `--session-id {session_id}` becomes `--resume {session_id}`), so the
conversation carries over. If the pane shows the agent mid-turn
(`busy_patterns`), it refuses unless you pass `--force`.

The first `api_key` start may stop at "Do you want to use this API key?".
Answer it once in the pane; the console flags the card as needing attention.
The inspector has the same controls as `cousin-auth`: a mode select and a key
field that shows only "key set" and the last four characters afterwards.

## Generations and the flip

A session doesn't last forever: the transcript grows and the context fills.
A flip ends one generation and starts the next on a fresh session, handing it
a boot packet so it picks up where the last one stopped.

```
cousin-flip wren --dry-run      # the checks only, nothing touched
cousin-flip wren                # flip now
cousin-flip wren --confirm      # the new generation posts one line when it's oriented
```

What happens, in short: the cousin is asked to update STATUS.md and write
`data/handoff.md`, with up to five minutes to do it (if it doesn't, the
framework writes an emergency handoff from the pane). The old generation is
archived under `data/generations/gen-NNNN/`, the counter goes up, a new boot
packet is assembled into `data/boot-packet-gen-NNNN.md`, the tmux session is
killed and started again on a new session id, and the packet is typed in.
By default the cousin is told not to announce the flip. The full step list
and what goes in the packet are in
[reference/lifecycle.md](reference/lifecycle.md).

Ways to trigger one:

- `cousin-flip` by hand, or the flip button in the console (now or in 1, 5 or
  15 minutes; a timed flip warns the cousin at T-5m, T-1m and T-30s).
- A daily flip: every cousin gets one, at the install's `default_flip_at` (04:00 unless `config/harness.toml` says otherwise). Set `[lifecycle] flip_at = "HH:MM"` in `cousin.toml` to move this one, or `"never"` to opt it out; `cousin-loops flips` shows each cousin's time and where it comes from. The loops
  daemon runs it once a day after that time, one cousin per tick.
- The transcript-size guard: with `flip_when_transcript_mb` in
  `config/harness.toml`, the loops daemon schedules a flip when a cousin's
  transcript grows past it.

If a flip crashes halfway it leaves a marker. Nothing recovers it on its own;
the console shows "stale marker" and you look at it.

A runner cousin's flip is a [rollover](glossary.md#rollover) instead: `cousin-flip` puts (or joins)
the pending `flip` row on the running `cousin-runner` and waits for the
handoff. None of the tmux steps above run (no marker, no pane, no pending
boot, no transcript mining: the runner already mined every [turn](glossary.md#turn) as it went),
and it refuses a stopped runner cousin, since a rollover needs a runner to
carry it out. See [`[agent] runner`](configuration.md#agent-runner) and
[commands](commands.md#running-cousins).

The boot packet includes the committed self-portrait: a description of the
cousin drafted from its real sources and reviewed by a person before it
counts.

```
cousin-self-portrait synthesize   # draft a candidate
cousin-self-portrait diff         # compare it with the committed one
cousin-self-portrait commit       # promote it; only the committed version boots
```

## Reincarnate and transplant

Editing a cousin's role in CLAUDE.md does nothing to the running session, and
killing the session loses what it was holding. These two do the change
properly: snapshot first, let the cousin write a handoff, change the files,
flip.

```
cousin-reincarnate wren --new-role "keeps the house notes and the budget"
```

Reincarnate snapshots the continuity files (MEMORY.md, STATUS.md, CLAUDE.md,
`cousin.toml`, the self-portrait, `memory/`) to
`data/lifecycle/wren/<timestamp>/`, asks the cousin through its chat server
for a bequest and waits up to `--timeout` seconds (default 300) for
`data/handoff.md` to change (a runner cousin skips this separate ask: its
bequest rides the flip's own rollover handoff request instead), rewrites the
role in the CLAUDE.md title line (and a `## Role` section if there is one)
and in `cousin.toml`, then flips. A cousin that doesn't answer is recorded,
not fatal: the flip asks again and writes an emergency handoff if needed.

```
cousin-transplant --donor wren --recipient kestrel --mode merge
```

Transplant works on two cousins, snapshots both, applies the mode, and flips
both (donor first):

| mode | the recipient keeps | the recipient gets |
|---|---|---|
| `soul-donation` | its CLAUDE.md, self-portrait, name and role | the donor's MEMORY.md and `memory/` |
| `body-swap` | its memory | the donor's CLAUDE.md, self-portrait, name and role (the donor gets the recipient's) |
| `merge` | everything | the donor's MEMORY.md appended under `## Memories inherited from <Donor> (<date>)`, and the donor's `memory/raw` merged in |

Slug, port and tmux session always stay where they are. The donor is never
deleted. Merge is the messy one: two timelines in one MEMORY.md, which the
distiller has to weigh.

Every step of both goes to `data/lifecycle/audit.jsonl`, which names the
snapshot paths. To undo, stop the cousin, copy the snapshot back over the home
(`memory/` whole), and flip it; for a transplant, do that for both. Nothing
stops the loops daemon from delivering a heartbeat in the middle, so do this
when the cousin is quiet.

## Session hooks

Two kinds of hooks run around a session.

The harness hooks are three shell scripts under `hooks/` that spawn wires
into `.claude/settings.json`. They read the home (first argument or
`COUSIN_HOME`), write only under `data/`, and never fail:

| hook | runs on | writes |
|---|---|---|
| `hooks/session_init.sh` | SessionStart | nothing; prints a banner with the slug, the identity files and which checkpoints exist |
| `hooks/pre_compact.sh` | PreCompact | `data/pre-compact-checkpoint.md`: current activity, last five decisions |
| `hooks/session_checkpoint.sh` | Stop | `data/session-checkpoint.md`: activity, open and in-progress STATUS items, last five decisions |

A fourth, `python -m cousin_lib.job_hooks`, records every subagent and every
background shell as a job on the Jobs page. Errors go to
`data/job-hooks.log`. On another harness, wire the scripts by hand; they only
need a POSIX `sh`.

A cousin on the `tmux` runner kind carries one more:
`cousin_lib.runner.tmux_hook`, wired on UserPromptSubmit, Stop, Notification
and SessionStart. It only pokes the runner's wake socket and, on
SessionStart, records the pane's session id; it decides nothing itself. The
boundary is the uid: the socket is 0600 in a `run/` created 0700, and on
Linux every datagram carries its sender's credentials (SO_PASSCRED), so one
from another uid wakes nothing. Nothing closes an inbox row or ends a turn
on a hook alone; the runner only ever confirms that against the pane's own
transcript. A hook is a wake-up, never an instruction.

The bookends are your own list of commands in `cousin.toml`, run by
`cousin-session start` and `cousin-session end` (the template tells the
cousin to call them):

```toml
[session]
start_hooks = ["cousin-cycle inc --start"]
end_hooks = [
  {name = "sync-state", cmd = "cousin-sync-state"},
  "cousin-cycle inc --end",
]
```

```
cousin-session end
#   -> running session end (2 hook(s))
#        [ok]   sync-state
#        [ok]   step-2
#      session end done: 2 ok, 0 failed, 0 skipped
cousin-session status             # last run and both lists, as JSON
```

Each entry is a command string (named `step-N` by position) or a table with
`cmd` and `name`. They run in order through `sh -c` in the home, with
`COUSIN_HOME`, `COUSIN_SLUG` and `SESSION_PHASE` (`start` or `end`) set. A
failing hook is reported with its exit code and the rest still run; the
command then exits 1. A hook running past 300 seconds is killed (rc 124).
`--skip NAME` leaves one out. Every run is recorded in `data/session.json`.
No `[session]` table means nothing runs, which is fine.

## The operator setting and zero-operator installs

`[operator] name` in `cousin.toml` is the person the cousin answers to. It
decides where `cousin-reply` goes when you don't pass `--user`, which chat
[thread](glossary.md#thread) the console opens, and who `send` in the MCP tools can reach by name.
Set it at spawn with `--operator` or later in the inspector (the chat server
needs a restart to pick it up).

It's optional. An install with no operator anywhere works; everything that
needs a person asks for one instead of inventing it:

- `cousin-reply` without `--user` fails and says to pass one or set
  `[operator]`.
- The console's chat falls back to the logged-in user; with no login either,
  the message box is disabled and says why.
- `--level operator` in `cousin-memory` is only ever written by hand with a
  `--cite`, so with no operator that level stays empty.
- The boot packet notes the missing operator calibration in its header.
- An uncommitted self-portrait stays a candidate; nothing commits it for
  you. Shared-tier promotions need a reviewer in
  `config/shared-reviewers.json` and refuse without one.
- A crashed flip is reported, never recovered automatically.

## Hidden cousins

`hidden = true` under `[cousin]` in `cousin.toml` (or hide/unhide in the
inspector) takes a cousin out of the console's sidebar and Cousins page until
someone turns on "show hidden". That's all it does: a hidden cousin still
runs, fires its loops and can be messaged by any cousin that knows its slug.
It is a way to keep the page tidy, not access control. The template tells
every cousin to answer a cousin it doesn't recognise normally.
