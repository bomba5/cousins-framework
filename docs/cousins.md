# Cousins

What a [cousin](glossary.md#cousin) is, how to make one, and how to run, change and retire it.
Read this after the quick start, when you want to know what `cousin-spawn`
actually built and what you can do with it.

## What a cousin is

A cousin is an agent session (Claude Code, in the setup I use) with a home
directory that holds everything it is, and a **kind**, `[agent] runner` in
`cousin.toml`, that says how its agent loop runs. Every kind is a
[runner](glossary.md#runner), `cousin-runner`, started and kept up by `cousin-supervisor`: `sdk` (the
default) and `opencode` with no tmux at all; `tmux` drives the host's
interactive Claude Code in a tmux pane on the framework's own socket; `fake`
exercises the [lane](glossary.md#lane) itself. A cousin with no `[agent] runner` is refused by
name ([migrating](migrating.md#a-cousin-with-no-runner)). See
[runners](reference/runners.md) for the kinds themselves.

Every cousin's home directory:

```
cousins/wren/
  cousin.toml          slug, name, role, operator, [agent] (kind, account, model), loops...
  CLAUDE.md            its identity, rendered from the template once, then yours to edit
  STATUS.md            open loops, the thing a new session anchors on
  MEMORY.md            the memory index
  memory/              raw entries, distilled views, memory files
  notes/               notes it writes
  data/                decisions log, handoffs, checkpoints, inbox, event stream
  scripts/             its own scripts
  mcp-registry.toml    the tools it gets over MCP
  .mcp.json            tells the harness to start cousin-mcp
  .claude/settings.json  its harness hooks
```

No local cousin runs a chat server of its own: a runner cousin's chat
history and [inbox](glossary.md#inbox) are written directly, and the console reads its chat
store itself. A cousin with no `[agent] runner` has no chat: it is refused
by name. The agent answers with `cousin-reply`, and messages another
cousin with `cousin-chat send`. See [chat](chat.md) and [memory](memory.md)
for those two halves.

A slug is a cousin if `cousins/<slug>/cousin.toml` exists. There is no other
registry: the filesystem is the fleet.

## Spawning one

A cousin needs a running `cousin-supervisor` to start (the units in
[install](install.md) run one) and an account to run on: the host's own
Claude login (`host`, the default) or one from `config/accounts.toml`
([accounts](configuration.md#accountstoml)). Its runner works unattended:
what it may do is its `policy.toml` ([configuration](configuration.md#policytoml)).

Then:

```
cousin-spawn wren --name Wren \
    --role "keeps the house notes" \
    --voice "Short and plain. Answers first, explains after. Says when it does not know." \
    --operator ana \
    --start
#   -> created wren at <checkout>/cousins/wren
#      started wren
```

Every option:

| option | what it does |
|---|---|
| `slug` | lowercase, `^[a-z][a-z0-9_-]{1,31}$`; the directory and the chat route |
| `--root` | the framework root (the checkout); else `FRAMEWORK_ROOT`, else the current directory if it is one |
| `--name` | display name; default is the slug capitalised |
| `--role` | one line; required to create |
| `--role-paragraph` | a paragraph for the Identity section of CLAUDE.md; default is the role |
| `--voice` | how the cousin writes; required to create |
| `--operator` | the person it answers to; written to `[operator] name` and into its MCP registry |
| `--model` | `[agent] model` |
| `--effort` | `[agent] effort`: `low`, `medium`, `high`, `xhigh`, `max` (the `sdk` and `tmux` kinds) |
| `--heartbeat` | `[heartbeat] context_beat_seconds` (default 3600) |
| `--memory-scope` | `[memory] scope`: `private` (default) or `shared` (may propose memories to the [shared tier](glossary.md#shared-tier); the retired `both` is read as `shared`) |
| `--runner` | `[agent] runner`: `sdk`, `tmux`, `opencode` or `fake`; absent, `COUSIN_DEFAULT_RUNNER` applies, and unset means `sdk` |
| `--account` | `[agent] account`, one of `config/accounts.toml`'s (or `host`); absent, `COUSIN_DEFAULT_ACCOUNT` applies, else `host` |
| `--start` | start it after creating; on an existing cousin without `--role`/`--voice`, only start it. The start asks the running [supervisor](glossary.md#supervisor); with none the start fails, exit 1. A cousin with no `[agent] runner` is refused, exit 2 |
| `--sync-template` | create nothing; show how an existing cousin's CLAUDE.md framework part differs from the current template (see [the CLAUDE.md template](#the-claudemd-template)) |
| `--apply` | with `--sync-template`: write the sync |
| `--prune-retired` | with `--sync-template`: also remove a framework section the template retired, when the cousin's copy still matches the template's last version (`templates/retired-CLAUDE-sections.md`); one that differs is kept, with a note to review it by hand |
| `--repair-settings` | create nothing; rewrite an existing cousin's `.claude/settings.json` and the `cousin` entry in `.mcp.json` |

An option you leave out writes no key, so the default applies and can change
later without touching the cousin.

Exit codes: 0 created (and started), 1 created but the start failed (the home
is kept; fix the cause and run `cousin-spawn wren --start`), 2 nothing was
done (bad input, slug taken, a cousin with no runner).
A failed create removes whatever it made, so it never leaves a half-made home
squatting the slug. A directory under `cousins/` with no `cousin.toml` is
reported as an orphan; remove or finish it by hand.

The console's spawn dialog sends the same fields and then starts the cousin;
see [console](console.md#spawning-a-cousin). For a cousin on another machine,
see [remote cousins](remote-cousins.md).

The home is created mode 0700, whatever the umask: other users on the
host, and anything running as another uid, cannot list or read it. Every
cousin runs as the same user, so this does not keep one cousin out of
another's home. A home made by an older release keeps its mode;
[`cousin-doctor homes`](commands.md#maintenance) lists each one open to
group or other with the `chmod 700` that closes it.

What spawn writes besides the identity files:

- `mcp-registry.toml` and `.mcp.json`, so the harness starts `cousin-mcp` and
  the cousin gets its memory, send, job and schedule tools ([mcp](mcp.md)).
  With Claude Code you may still need `cousin-mcp approve wren` to record the
  approval in `~/.claude.json`.
- `.claude/settings.json` with the harness hooks: the session banner,
  the pre-compact checkpoint, and the job-tracking hook (see
  [session hooks](#session-hooks)). Keys it doesn't own are kept.

`cousin-spawn wren --repair-settings` rewrites those two for a cousin made by
an older release, or after you move the checkout. It is safe to repeat.

## The CLAUDE.md template

Every new cousin's `CLAUDE.md` comes from `templates/cousin-CLAUDE.template.md`.
Nothing else writes one. Spawn fills in five placeholders:

| placeholder | from |
|---|---|
| `{{NAME}}` | `--name` |
| `{{SLUG}}` | the slug |
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
  with the cousin's name, slug and role from `cousin.toml`. An edit you
  make there is replaced at the next start: put your own rules below the
  marker.
- A section above the marker that the template doesn't have is kept, at the
  end of the framework part. A section the template retired (3.49.0 moved
  the lane's mechanics out: chat handling, the memory commands, tools,
  session bookends, meetings; the generated contract in the system prompt
  carries them) is kept and reported until you prune it with
  `--prune-retired`, which removes it only while it still matches the
  template's last version (`templates/retired-CLAUDE-sections.md`). A copy
  with your own lines in it is kept, with a note to review it by hand.
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
cousin-spawn wren --sync-template --prune-retired           # the diff with the retired sections gone
cousin-spawn wren --sync-template --apply --prune-retired
```

## Starting, stopping, restarting

```
cousin-spawn wren --start          # start an existing cousin (no-op if running)
cousin-supervisor stop wren        # stop it and hold it down
cousin-supervisor start wren       # start it again (clears the hold)
```

`--start` (or the console) asks the running `cousin-supervisor` to start the
cousin's `runner:<slug>` child; with no supervisor up the start fails. A stop
holds the cousin down (`<home>/run/held`) across a supervisor restart until
the next start. A restart resumes the same session
(`data/runner-session.json`), so the conversation carries over; a flip
(below) is how a cousin starts fresh. A resumed sdk session keeps the system
prompt and the tool list it started with (the CLI's prompt snapshot), so when
either changed since then (the law, its identity files, your rules, the
registry or `.mcp.json`), the runner rolls it over after its first
[turn](glossary.md#turn) (which still runs with the old ones), and the next
session gets the new ones. A release's version number alone does not; a
release that changes the contract text or the shipped registry does, once. The console's card and inspector have
the same buttons. See [commands](commands.md#running-cousins) and
[runners](reference/runners.md).

A cousin with no `[agent] runner` is not started: `cousin-spawn --start`
exits 2 with the reason, and the console answers 409
([migrating](migrating.md#a-cousin-with-no-runner)).

To get rid of a cousin, use "dismiss" in the console: it stops it, archives
the home (without `.secrets/`) to `data/dismissed/<slug>-<timestamp>.tar.gz`
and removes it.

## Models and effort

A cousin's model and effort are `[agent] model` and `[agent] effort` in its
`cousin.toml`, read once when its runner starts:

```toml
# cousins/wren/cousin.toml
[agent]
runner = "sdk"
model = "claude-sonnet-5"
effort = "medium"
```

Effort is one of `low`, `medium`, `high`, `xhigh`, `max` (the `sdk` and `tmux`
kinds; an `opencode` model names its provider, `"<provider>/<model>"`).
Without a model the kind's own default applies. The install's defaults in
`config/harness.toml` are what the console's spawn dialog preselects:

```toml
# config/harness.toml
[agent]
default_model = "claude-opus-5"
default_effort = "high"
models = ["claude-opus-5", "claude-sonnet-5"]   # what the spawn dialog offers
```

A change needs a restart. The console's agent settings (the inspector, and the
chat header's effort select) write these keys; an `sdk` model is checked with
one smallest [turn](glossary.md#turn) before it is written. See
[`[agent] runner`](configuration.md#agent-runner). `[runtime] model` and
`effort` are 1.x keys and are not read
([removed keys](configuration.md#removed-in-200)).

## Auth: accounts

A cousin runs on an account: `[agent] account` in `cousin.toml`, one of
`config/accounts.toml`'s, or `host` (the host's own Claude login, the
default). An account is a Claude login, a Claude token, an Anthropic API key,
or an opencode data dir with its providers' keys; which kinds run on which
is in [accounts.toml](configuration.md#accountstoml), and `cousin-account`
manages them ([commands](commands.md)). `[runtime] auth` is a 1.x key and
is not read ([removed keys](configuration.md#removed-in-200)).

## Generations and the flip

A session doesn't last forever: the context fills. A flip ends one
generation and starts the next on a fresh session, which starts from a state
digest (STATUS.md's open loops, the handoff, memory) so it picks up where the
last one stopped. On a runner this is a [rollover](glossary.md#rollover).

```
cousin-flip wren --dry-run      # the checks only, nothing touched
cousin-flip wren                # flip now
```

`cousin-flip` puts (or joins) the pending `flip` row on the running
`cousin-runner` and waits for the handoff: the cousin is asked to write it,
the generation counter goes up, and the new session gets the digest. The
runner already mined every turn as it went. A stopped
cousin is refused, since a rollover needs a runner to carry it out. The full
step list is in [reference/lifecycle.md](reference/lifecycle.md).

Ways to trigger one:

- `cousin-flip` by hand, or the flip button in the console (now or in 1, 5 or
  15 minutes; a timed flip warns the cousin at T-5m, T-1m and T-30s).
- A daily flip: every cousin gets one, at the install's `default_flip_at` (04:00 unless `config/harness.toml` says otherwise). Set `[lifecycle] flip_at = "HH:MM"` in `cousin.toml` to move this one, or `"never"` to opt it out; `cousin-loops flips` shows each cousin's time and where it comes from. The loops
  daemon runs it once a day after that time, one cousin per tick; a cousin whose session started after that time is not flipped that day, nor one whose generation was idle (only upkeep since it started) and whose prompt and tools have not changed.
- Context pressure: the runner rolls over on its own at
  `[agent] rollover_at_percent` of the model's context (the `sdk` and
  `opencode` kinds; [configuration](configuration.md#agent-runner)).

The runner's system prompt includes the committed self-portrait: a description of the
cousin drafted from its real sources and reviewed by a person before it
counts. It holds the cousin's voice and how it works (Temperament, Working
Style, Voice) and nothing else: the role lives in `CLAUDE.md` and
`cousin.toml`, the rules in the law and L0 memory, the lane's mechanics in
the generated contract. A copy of any of those in the portrait went stale
beside its source. `synthesize` keeps each of a committed portrait's
sections as it is, placeholders included; a section it lacks is drafted
from the cousin's own part of `CLAUDE.md` (Who I am, How I work, Voice),
then the template's part (Temperament from the Identity role paragraph
only), and a section with no source is a review TODO. The runner hands the composed prompt to the agent CLI as a private
file (`data/run/system-prompt.md`, readable by the cousin's user only), never
on its command line, where every local user could read it
([runners](reference/runners.md#the-system-prompt-is-a-private-file)).

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
`data/lifecycle/wren/<timestamp>/`, rewrites the role in the CLAUDE.md title
line (and a `## Role` section if there is one) and in `cousin.toml`, then
flips: the cousin's bequest rides the rollover's own handoff request. A cousin
with no `[agent] runner` is refused.

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

The slug always stays where it is. The donor is never
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


The per-turn Stop checkpoint (`hooks/session_checkpoint.sh`) was retired in
3.47.0: it was one more copy of STATUS.md's open loops. The script stays as a
no-op until no home names it; `cousin-spawn <slug> --repair-settings` removes
it from a home's settings (an upgrade does not re-apply them).

A third, `python -m cousin_lib.job_hooks`, records every subagent and every
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
  {name = "activity", cmd = "cousin-memory activity 'session closed'"},
  "cousin-cycle inc --end",
]
```

```
cousin-session end
#   -> running session end (2 hook(s))
#        [ok]   activity
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
Set it at spawn with `--operator` or later in the inspector (the runner
needs a restart to pick it up).

It's optional. An install with no operator anywhere works; everything that
needs a person asks for one instead of inventing it:

- `cousin-reply` without `--user` fails and says to pass one or set
  `[operator]`.
- The console's chat falls back to the logged-in user; with no login either,
  the message box is disabled and says why.
- `--level operator` in `cousin-memory` is only ever written by hand with a
  `--cite`, so with no operator that level stays empty.
- The state digest leaves out its Operator Calibration section when there is
  no calibration yet; nothing stands in for it.
- An uncommitted self-portrait stays a candidate; nothing commits it for
  you. Shared-tier promotions need a reviewer in
  `config/shared-reviewers.json` and refuse without one.
- A crashed flip is reported, never recovered automatically.

## Hidden cousins

`hidden = true` under `[cousin]` in `cousin.toml` (or hide/unhide in the
inspector) takes a cousin out of the console's sidebar, Cousins page, meeting pool
and Jobs view (its jobs and artifacts) until someone turns on "show
hidden". That's all it does: a hidden cousin still
runs, fires its loops and can be messaged by any cousin that knows its slug.
It is a way to keep the page tidy, not access control. The template tells
every cousin to answer a cousin it doesn't recognise normally.
