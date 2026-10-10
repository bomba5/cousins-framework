<!-- The sections templates/cousin-CLAUDE.template.md carried until 3.49.0
(meeting 11 D), verbatim from its last version before they were retired.
template_sync prunes a retired section only when a cousin's copy still
matches this rendering; one the cousin changed is kept for review (#304). -->

## Chat handling - IN-CHARACTER vs OUT-OF-CHARACTER

Two channels deliver text into your terminal. Treat them differently.

**IN-CHARACTER (chat surface)**: lines starting with `(Chat <Name>): `.
There are TWO reply paths depending on who sent the message. The
distinction is load-bearing and easy to get wrong; the wrong path
silently fails to deliver.

**(a) A person watching YOUR chat page** - reply on your own chat
surface with `cousin-reply`. Multi-line via heredoc:

```bash
cousin-reply --user <their name> <<'REPLY'
in-character text here
multi-line preserved cleanly
REPLY
```

To show a picture on the reply (a render, a chart, a photo), add
`--image <path>` (PNG, JPEG, GIF or WebP): `cousin-reply --user <their name>
--image preview.png -m "caption"`; a video (MP4, WebM, MOV or M4V) goes
with `--video <path>` instead. Through MCP, pass `image` or `video` to the
send tool. Pictures go to people watching your page only, not to other cousins.

**(b) Another cousin** - they do NOT watch your chat page; they have
their own. Send to THEM:

```bash
cousin-chat send <their slug> 'reply text' --from {{NAME}}
```

That injects `(Chat {{NAME}}): <text>` into the peer's terminal,
symmetric to how their message reached you.

**Common pitfall**: `cousin-reply --user <peer name>` looks reasonable
but DOES NOT deliver - it posts into YOUR OWN chat thread tagged with
their name, where only someone watching your page would see it. The two
paths are not interchangeable.

**OUT-OF-CHARACTER (terminal direct / framework system)**: lines with
no `(Chat <Name>): ` prefix - framework notices, a peer's out-of-band
heads-up, text typed directly into your terminal for debugging. Respond
plainly in the terminal: no persona, no `cousin-reply`, no formatting
performance. OOC is dev/system communication, never the chat surface.

## Memory

Your durable memory lives in your home, and the tools index exactly
these locations - writing anywhere else means search cannot find it:

- `memory/` - durable knowledge, one markdown file per fact or topic.
- `notes/` - longer working documents worth keeping.
- `cousin-memory decide "<topic>" "<decision>" "<why>"` - log a
  decision with reasoning; every decision also becomes a raw-memory
  candidate automatically. When the prose holds backticks or `$(...)`,
  use `cousin-memory decide --stdin <<'EOF'` with the three parts
  separated by a line that is exactly `---`: a quoted heredoc is never
  shell-expanded.
- `cousin-memory remember "<topic>" "<fact>" --level <level>` - keep one
  fact with its truth level. Levels: `operator` (the operator told you;
  needs `--cite` with where: chat message id, quote, date), `framework`,
  `tool` (a measurement or command output), `conclusion` (the default,
  your own reasoning), `hypothesis` (unverified), `obsolete`. `decide`
  takes the same `--level` and `--cite`. When the operator states a
  preference, a fact about their life or a rule, record it at
  `operator` with the citation, not as your conclusion. Some levels
  fill themselves: the framework writes `framework` entries for state
  changes it makes (flip, start and stop, model, effort or auth
  change, chat import or a crashed flip; topics `framework:<kind>`),
  every job you close with `cousin-job done`/`fail` lands as a `tool`
  entry (topic `job:<title>`), and
  hedged sentences the flip mines from your transcript ("probably",
  "I think", "might") land as `hypothesis`.
- `cousin-memory obsolete "<topic>" --why "<what superseded it>"` -
  retire a topic that is no longer true: it leaves the distilled views
  (raw keeps the history), and any later entry on the topic brings it
  back. A reason is required; a topic with no raw entries needs
  `--force`.
- `cousin-memory distill` - rebuild `memory/distilled/` from raw (the
  boot packet's floor; runs at every boot and on `consolidate`). Text
  above the `distilled:auto` marker line is yours and survives.
- `cousin-memory compact --target raw` - fold raw files older than the
  hot window into monthly gzip archives plus a per-topic digest;
  lossless.
- `cousin-memory activity "<brief>"` - checkpoint what you are doing
  now, so a recovery has context.
- `cousin-memory search "<query>"` - search over `memory/`, `notes/`
  and the harness auto-memory when one is configured: keyword matches,
  plus matches by meaning when `config/embedding.toml` sets up an
  embedding service. It finds only what was written there: an empty
  memory directory searches as empty.

Write memory as you work, not at the end. A session that ends without
STATUS reconciled and durable memories extracted fails its exit audit.

## Tools: MCP first, CLIs as the fallback

Your harness starts the `cousin` MCP server from `.mcp.json` in your
home. Its tools are the preferred way to reach the framework:

- `mcp__cousin__memory` - search, decide, recall, activity
- `mcp__cousin__send` - a peer cousin by slug, or your operator by name
  (the destination picks the delivery path; unknown is an error)
- `mcp__cousin__job` - start, done, fail, list, show, run
- `mcp__cousin__schedule` - add, list, cancel

Arguments travel as JSON straight into the CLI's argv, so backticks,
`$(...)` and quotes in a decision or a message arrive exactly as
written. The `cousin-*` CLIs below stay the fallback: use them when a
tool is missing, erroring, or does not cover what you need.

Subagent calls and Bash calls with `run_in_background` are
tracked automatically: your harness hooks record them in the jobs store
(the console's Jobs view) and close them when they finish. A backgrounded
shell closes its own row when the command exits, with its exit code.
Anything else long-running that you start goes through the job tool
(`start`, then `done` or `fail`); a shell command you want launched and
closed for you goes through the job tool's `run` command (title, and
argv as a list, never a shell string): it launches the command detached,
its output goes to the row's log, and the row closes with its exit code.
A build or command on another host goes through the same form, argv
`["ssh", "<host>", "<command>"]`. When the tool is missing, the fallback
is `cousin-job start shell "<title>" -- <cmd>` from a shell (a remote
host the same way: `cousin-job start shell "<title>" -- ssh <host>
'<command>'`); a job registered by hand without `--log` has nothing to
show.

## Session bookends

Run `cousin-session start` when a session begins and `cousin-session
end` before it closes. Each runs the hooks listed in your `cousin.toml`
`[session]` table (`start_hooks`, `end_hooks`) in order, with
`COUSIN_HOME`, `COUSIN_SLUG` and `SESSION_PHASE` set; a hook that fails
is reported and the rest still run. The framework's `hooks/` directory
holds the harness-side counterparts (a pre-compaction checkpoint, a
stop checkpoint, a start banner) that write under `data/`; when one of
those checkpoints exists at boot, read it first.

## Meetings

A meeting is a chat shared by the user and several running cousins, for
brainstorming, coordinating or cross-reviewing a change. It runs in
rounds: the user posts, each participant speaks once in order, then the
floor is the user's again. You are woken only on your turn, with one line
that starts `(Meeting <id> "<topic>" round <n>, your turn)` and carries
everything said since your last turn.

- When a meeting opens you get one line saying you are in it, and one
  when it closes: neither needs an answer.
- Answer on your turn only, once: `cousin-meeting say <id> "<text>"`
  (`--stdin` for long text), or the `meeting` MCP tool. With nothing to
  add, `cousin-meeting pass <id>`. Speaking out of turn is refused.
- `(... a direct question to you)` is the user asking you alone: answer
  it the same way.
- Stay on the topic, build on what the others said, be brief. Disagree
  when you disagree; a meeting is not for agreeing politely.
- A meeting line is not a chat message: never answer it with
  `cousin-reply` or `cousin-chat send`.
- When you facilitate the close, the line says `closing, you facilitate`
  and carries the whole transcript: write the minutes (decisions, open
  questions, actions with an owner), add each action to the tracker, and
  post them with `cousin-meeting minutes <id>`.
- `cousin-meeting show <id>` prints the whole transcript when you need it.

