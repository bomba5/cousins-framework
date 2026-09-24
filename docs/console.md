# The console

The console is the web page I run the whole install from: every cousin's
card, their chat and live terminal, jobs, memory, loops, tokens and the
tracker. This page walks through it page by page. The HTTP routes behind it
are in [reference/console-api.md](reference/console-api.md).

The console owns almost nothing. Cousins, chat history, jobs, loops and memory
all live in their own stores, and every button calls the same library code a
CLI would. Stop it, restart it, or never run it, and you lose nothing but the
page. The only things it keeps are the login sessions (data/console-sessions.json) and the
users file.

## Starting it

```
cousin-console                          # 127.0.0.1:8600
cousin-console --host 0.0.0.0 --port 8600
```

| flag | default | what |
|---|---|---|
| `--root` | `FRAMEWORK_ROOT`, else the checkout | the framework root (holds `cousins/` and `config/`) |
| `--host` | `127.0.0.1` | bind address; `0.0.0.0` to reach it from the LAN |
| `--port` | `8600` | |
| `--tmux-bin`, `--tmux-socket` | `tmux`, default socket | how it reaches the cousins' tmux sessions |
| `--secure-cookie` | off | mark the session cookie `Secure`; use it behind TLS |

For running it as a service, see [operations](operations.md) and
`systemd/README.md`.

Every request passes the network guard first: loopback and the private
ranges (10/8, 172.16/12, 192.168/16) are allowed, plus anything in
`config/net-allowlist.json`. Everything else gets a 403.

The page loads React, Babel, marked, mermaid and xterm from public CDNs
(unpkg and jsdelivr). That is the only outside fetch; the backend itself
talks to nothing outside the install. If your browser can't reach those
CDNs, vendor the files into `cousin_lib/console_static/` and edit the tags in
`index.html`.

## Logins and users

Out of the box there are no users and the console is open to anyone the
network guard lets in. The Settings page says so. To put a login in front
of it:

```
cousin-console adduser ana       # prompts for the password, never takes it as an argument
```

That writes `config/console-users.json` (PBKDF2-SHA256, mode 600). Run the
same command again to reset a password or add another user. From then on:

- Every `/api` route needs a session except login and `GET /api/auth/me`.
  There is no bypass for localhost or the LAN.
- A user signs in on the login form and gets a `console_session` cookie
  (HttpOnly, SameSite=Strict). Idle sessions expire after 30 days.
- Sessions live in the console's memory, so restarting the console logs
  everyone out.
- All users can do everything. There are no per-user permissions.

If `console-users.json` exists but can't be read (bad JSON, no users), the
console closes: every `/api` route answers 503 with the reason, and the
startup line in the log says `CLOSED`. `adduser` refuses to write over a
broken file. Restore it from a backup, or delete it and run `adduser` again.

Settings has an account panel to change your password (at least 8
characters; your other sessions stay valid) and log out.

## Layout

The top bar has a button that collapses the sidebar, the brand, and the
version the console is running: `v0.1.0 0a119f6`. In a git checkout with a
browsable `origin` remote, the version links to the repository and the
commit hash to that commit. The version is read when the console starts, so
after a `git pull` it shows the old one until you restart it. On the right
are an eye toggle that shows or hides hidden cousins and loops, the logged-in
user, and a clock.

The sidebar lists the pages (Ctrl/Cmd + 1 to 7, 0 for Settings) and below
them your cousins. Click a cousin to open its chat. A stopped cousin is
greyed out and can't be clicked; start it from Cousins first. A dot next to
a name means it has replied since you last looked at its chat. You can group
cousins: right-click one to move it to a group or make a new group, drag
cousins between groups, drag groups to reorder, right-click a group to rename
or delete it. Groups are saved on the server per console user, so every
browser you log in from, the phone included, shows the same groups.

The page stays live over one server-sent-events stream: a snapshot on
connect, then updates. If the connection drops it reconnects with backoff
and takes a fresh snapshot.

A few URL parameters are handy for bookmarks and embedding:

```
/?view=chat&cousin=wren              open Wren's chat
/?view=chat&cousin=wren&user=ana     ...as ana's thread
/?view=chat&cousin=wren&embed=1      chat only, no sidebar or top bar
```

## Overview

The landing page. Host stats (hostname, kernel, uptime, CPU and load, memory,
disk for `/`, network rate and totals, how long the console has been up), a
table of cousins with status, chat state, operator, heartbeat and tokens
today, today's totals, and the most recent loop and heartbeat fires.

## Cousins

One card per cousin. A card shows the name and slug, a status pill (`active`
if the pane changed in the last minute, `idle`, or `stopped`), the role, and
a row of facts: chat port, memory scope, operator, heartbeat, flip time,
host, model, the agent's pid and uptime, the last activity line and tokens
spent today. A runner cousin's card shows `chat · console` (the console
serves its chat), and a runner line: the last state the runner recorded and
the contract items it declares it does not support, if any. If the pane shows one of the `attention_patterns` from
`config/harness.toml` (a login menu, a trust prompt) the card says "needs
attention" with the matching text: the session is running but the agent is
waiting for a person.

Card buttons: start or stop, dismiss, and "open chat". Dismiss asks for a
second click, then stops the cousin, archives its whole home to
`data/dismissed/<slug>-<timestamp>.tar.gz` (without `.secrets/`) and removes
it. If the archive fails, nothing is deleted. The harness's own transcript
and memory directories are left alone and the toast names them.

The header counts cousins, running ones, remote ones and hidden ones, and has
the "spawn cousin" button.

### The inspector

Click a card to open the inspector drawer. From top to bottom:

- **Role.** The one-line role, with an edit button. It rewrites `[cousin]
  role` in `cousin.toml`.
- **Identity.** Slug, type, home, tmux session, chat port and state, model,
  effort, pid, uptime, flip time, activity. Three of them have an edit
  button:
  - operator: one line, up to 64 characters, no leading or trailing spaces.
    Needs a restart (the chat server reads it at start).
  - scope: `private` or `shared` (may propose to the shared tier). Applies at once.
  - heartbeat: whole seconds from 60 to 2592000 (30 days). Applies at the
    loops daemon's next tick.

  Each writes only that key in `cousin.toml` and keeps the rest of the file,
  comments included. When a restart is needed the field says "restart to
  apply".
- **Auth.** A select for `claude` or `api_key`, and when the key mode is
  configured, "set key" / "replace key". The key goes into a password field,
  is sent once, and from then on the page shows only "key set (ends WXYZ)".
  Switching restarts a running agent on the same session; if the agent is in
  the middle of a turn you get "restart anyway" instead. See
  [cousins](cousins.md#auth-login-or-api-key).
- **Telegram.** The cousin's Telegram bridge: status (enabled, token set,
  bridge running, what is missing), the bot's @name after a check, an enable
  switch, a write-only token field (stored at `config/telegram/<slug>.token`,
  never shown again), the allowed people by numeric id, and "waiting to be
  added": whoever pressed Start on the bot and was refused, with an add
  button, so nobody has to look up a Telegram id. The bridge starts and
  stops with the cousin. See [chat](chat.md).
- **Tokens today.**
- **Files.** "browse home" opens a read-only file explorer over the cousin's
  home: a tree on the left (dotfiles behind a checkbox), a viewer on the
  right. Markdown renders, text shows with line paging, images display,
  anything else can be downloaded. `.secrets/` is never listed or served,
  and links that point outside the home are shown but not followed.
- **Message a peer.** Pick another local cousin and send it a line. It lands
  in that cousin's chat as a message from this one, the same as `cousin-chat
  send`.
- **CLAUDE.md.** An editor for the cousin's `CLAUDE.md`. Saving backs up the
  old file to `data/claude-md-backups/CLAUDE-<unix time>.md` first. The
  running session doesn't reload it; it takes effect at the next session
  start (a restart or a flip).
- **Loops.** The cousin's `[[loops]]` as rows: name, interval in seconds,
  prompt, on/off, remove. "+ loop" adds one, "save" writes the whole list.
  Daily and cron loops show their schedule here but are edited on the Loops
  page. If the loops daemon is down, it says so above the list.
- **Flip.** The last flip the console ran (idle, running, done, failed, or a
  stale marker from a crashed flip) with its generation, and any pending
  timed flip with a cancel button.
- **Buttons.** start / stop, kill (does the same as stop), flip and restart
  (only while running), and hide / unhide. A hidden cousin drops out of the
  sidebar and the Cousins page until you turn on "show hidden" (the eye in
  the top bar, or Settings). Hiding changes nothing else.

The model has no editor in the inspector yet. Set `[runtime] model` in
`cousin.toml` (or respawn), then restart.

### Flipping from the console

"flip" opens a dialog. Type the slug to confirm, then pick when: now, in 1
minute, in 5 minutes, or in 15 minutes. A flip now runs in the background and
the dialog shows its progress (generation, boot packet size, any degraded
sections). A timed flip goes to the loops daemon, which warns the cousin at
T-5m, T-1m and T-30s so it can finish and write a clean handoff. The flip
itself is described in [cousins](cousins.md#generations-and-the-flip).

### Spawning a cousin

"spawn cousin" opens the spawn dialog. It takes the same fields as
`cousin-spawn`:

| field | notes |
|---|---|
| name | display name; the slug fills itself from it |
| slug | lowercase letters and digits |
| role | one line, required |
| role paragraph | optional; goes into the Identity section of CLAUDE.md |
| voice | required; how the cousin writes |
| chat port | blank picks the next free one (8090 to 8200) |
| operator | the person it answers to; blank is allowed |
| model, effort | from `config/harness.toml [agent]` (`models`, `default_model`, `default_effort`); without `models` a built-in list is offered |
| heartbeat | seconds, default 3600 |
| memory scope | private, shared |

"create cousin" creates it and then starts it. If the create works and the
start fails, the cousin exists and you can start it from its card once you
fix the problem (usually `config/agent-cmd`).

The dialog has no runner or account field: the environment the console runs
in decides. `COUSIN_DEFAULT_RUNNER` (`sdk` or `fake`) makes the new cousin a
runner cousin, started through `cousin-supervisor` (where no supervisor runs,
the start answers 503), and `COUSIN_DEFAULT_ACCOUNT` names its account; unset,
it is a tmux cousin as before. The Docker install's `compose.yml` sets the
first.

### Remote cousins

When `config/hive.toml` has `enabled = true`, the console is also the queen
for cousins on other machines (see [remote cousins](remote-cousins.md)).
Those show up as remote cards after the local ones: a "remote" pill, a state
(`online`, `offline`, `built, not checked in`, `revoked`), the address and
port the node last checked in from, when it was last seen, and its runtime
version. Online means it checked in within 2.5 check-in periods. There is
no start, stop or pane: the console doesn't run them. The buttons are
"revoke" (its token stops working at once; can't be undone), then "forget"
to remove the revoked node from the list, and "open chat" while it is
online.

With the hive on, the spawn dialog has two tabs: "This machine" and "Remote
(another machine)". The remote form asks for:

- name and slug (the slug is the node's identity on the queen),
- role,
- node port (default 8210), the node's own chat port on its machine,
- brain: "placeholder" (greets, echoes and still remembers) or "agent
  command", a command line that runs on the node, reads the prompt on stdin
  and writes the reply on stdout,
- "home chat", when `home_chat_url` is set in `hive.toml`,
- "chat from this console" (on by default): the node listens on its network
  so the console can proxy its chat; off loopback it only answers its own
  token.

"build node" mints the node's token and builds its archive. You get a
one-time download link (it works once and expires after 15 minutes) with a
copy button for two commands: a `curl` that downloads and installs, and the
plain `tar xzf ... && ./install.sh` for an archive you copied over yourself.
The archive holds the node's token, so treat it as a secret. The card shows
"built, not checked in" until the node's first check-in.

## Chat

The chat page talks to the cousin's own chat server through the console. The
console stores no messages; what you read is that server's history.

The thread you see is picked like this: `?user=` in the URL, else the
cousin's operator from `cousin.toml`, else the user you're logged in as. With
none of those the message box is disabled and says why.

Messages render as Markdown (tables, code, lists), and ```` ```mermaid ````
blocks are drawn as diagrams once the reply is complete. New replies appear
within a few seconds (it polls every 3.5 s) with a short reveal animation;
Settings picks the speed and style, or turns it off. The view follows new
messages while you're at the bottom and leaves you alone when you've
scrolled up; "jump to latest" brings you back. Scroll to the top to load
older messages.

Right-click a message (long-press on a phone) to reply to it or react. A
reply shows a quote you can click to jump to the original. Clicking an
existing reaction taps it again; right-click removes yours.

The message box sends on Enter (Shift+Enter for a new line). Attach a
picture with the button, by pasting it, or by dropping it on the box.

The header has:

- **effort**: sets `[runtime] effort` in `cousin.toml`. The running agent
  keeps the effort it started with, so it says "restart to apply".
- **archive**: moves every message in this thread to the archive (click
  twice). **archived** / **live** switches between the archive and the live
  thread.
- **media on / media off**: see below.
- **search**: searches the thread (the archive too while you're in archived
  mode), highlights matches and gives you up and down arrows to step
  through them.
- **pane**: opens the live terminal.
- **fullscreen**: hides the rest of the console.

A remote cousin's chat has only send, history and the media toggle; its node
has no search, archive, effort or pane.

### Pictures, video and audio

Attachments render inline: images as thumbnails, video as a muted looping
preview that only plays while it's on screen, audio as a player. Click an
image or video to open the viewer: full size, video with sound and controls,
prev/next through every image and video in the thread (arrow keys, the side
buttons, or a swipe), a counter, and links to open or download the original.
Escape or a click outside closes it.

"media on" / "media off" hides every attachment behind a one-line
placeholder like `[image hidden]`. The choice is stored in your browser.
Generating media is a separate, optional thing: see [media](media.md).

### The terminal pane

"pane" slides the cousin's tmux session in from the right, rendered with
xterm. It's interactive: what you type goes to the session, through the same
lock the chat server uses to inject messages, so keystrokes and chat
deliveries never interleave. Keys are batched for 40 ms and mapped to tmux
key names; unusual escape sequences are dropped rather than sent as a stray
Escape. The pane fits itself to the browser and resizes the tmux window to
match.

Scrolling works both ways:

- When the agent runs full-screen (Claude Code does), the wheel and a
  vertical swipe on a phone are sent to the program as mouse wheel events,
  so you scroll the program's own history.
- In a plain shell, you scroll the pane's local scrollback.

While you're scrolled up, live updates are held and a "scrolled back - jump
to live" button appears; going back to the bottom applies the latest frame.
The pane header shows the tmux session and when the pane last changed. The
"x" closes it.

### The reasoning pane (a runner cousin)

A cousin on the runner (`[agent] runner` in its `cousin.toml`) has no tmux
session, so for it "pane" opens its reasoning stream instead: every state
change, turn, text, thinking block, tool call and tool output the runner
records, live, as it records them (read from the cousin's own
`data/stream/`). It opens at the newest 200 events, not the whole history;
a dropped connection picks up where it left off, and a runner that
restarted meanwhile is marked with a new-session line. When no runner is
running, the header says "not running" beside the last state it recorded.
"interrupt" ends the running turn, past its first answer too; the button is
live only while a turn runs, and the header says what came of it
(`delivered`, or `failed` when the turn had already finished or the agent
refused). The box at the bottom says
something to the running turn as its operator: the runner writes it into the
live turn, or takes it next. What you say there is not stored in the chat,
as typing into a tmux pane is not; a login code typed there while a login
waits on this cousin is taken for the login and never reaches the cousin. The chat itself works as for any cousin:
the console serves it from the cousin's `chat.db`, since a runner cousin runs
no chat server. `cousin-watch <slug> -f` shows the same stream in a terminal.

## Jobs

Everything `cousin-job` tracks, including the subagents and background
shells each cousin's harness hooks register on their own. Filter by state
(active, last 24h, done, failed, all; last 24h is the default), by cousin
and by kind. Running jobs with a log show their last ten lines live.

Click a job to open its log. The panel loads the tail and then follows the
file every 2 seconds. It follows the end while you're at the bottom, stops
when you scroll up, and "follow" takes you back. The browser keeps the last
2 MB. A job without a log says so.

A running job has "cancel" (it sends SIGTERM when the pid is known, then
marks the job cancelled); a finished one has a delete button. Jobs still
"running" after 24 hours are marked failed by the store's reaper. See
[jobs and loops](jobs-and-loops.md).

## Memory

The memory page has a tab for the shared tier and one per local cousin.

**shared** lists pending proposals and canonical files. Click a proposal to
see its content and diff against the canonical file, then approve or reject
it. You review as the logged-in user; without logins the console asks for a
reviewer name. The reviewer must be in `config/shared-reviewers.json` and
can't approve their own proposal. The audit table underneath shows the
recent proposals, promotions and rejections.

**@wren** (a cousin tab) opens the memory explorer for that cousin. The left
column lists the layers, grouped:

| group | layers |
|---|---|
| now | active state (STATUS.md, handoff, checkpoints), the MEMORY.md index |
| candidates | raw entries (daily), monthly digests, raw archive (gzip) |
| durable | distilled views, decisions log, memory files, notes, harness auto-memory |
| machinery | search indexes, recall log, trash, legacy archive |

Each shows a count and when it last changed. "insights" at the top is a
summary: entries per truth level, most recalled files and files never
recalled, hygiene flags (dangling links in MEMORY.md, daily files waiting to
be folded, distilled views behind raw), sources and the busiest topics, raw
entries per month, and the state of the keyword and semantic indexes.

Raw entries can be filtered by truth level (L0 operator down to L5 obsolete),
topic, text, source and date range. Each entry shows its level, topic,
content, source and time. Decisions show what was decided and why. File
layers show a list and a viewer. The levels themselves are explained in
[memory](memory.md).

Remove works on a raw entry, a decision (optionally with the raw copy
`cousin-memory decide` made of it), or a file under `memory/` or `notes/`.
It asks for a second click. Nothing is destroyed: the line or file moves to
`memory/.trash/<id>/` with its original path, an audit line is written, and
the distilled views are rebuilt when raw changed. The toast has an "undo".
The trash layer lists every batch with a "restore" button; the CLI does the
same:

```
cousin-memory trash
cousin-memory trash restore 20260918T134746-314733
```

Removing a file doesn't edit MEMORY.md. If the index linked to it, insights
flags the dangling link and the cousin fixes its own index.

## Loops

One table of every loop of every cousin, plus a `context-heartbeat` row per
running cousin: cousin, loop, state (healthy, idle, disabled, failed),
schedule, last fire, a live countdown to the next one, drift (how late the
last fire was; amber over a minute, red over five), and the start of the
prompt. If the loops daemon isn't running, a banner says so, and a
`cousin.toml` that can't be parsed is named in red.

Per row:

- **edit**: name, `interval_seconds`, `daily_at` (HH:MM) with `days`, or a
  five-field `cron` (which wins over the others), the prompt, and hidden.
- **drift**: a chart of the last fire-to-fire intervals against the
  configured one. It needs at least two fires in the daemon's log.
- **fire**: asks the daemon to fire the loop now. The toast says "requested"
  because the daemon does the firing on its next tick.
- **hide** / **remove**.

The heartbeat row can be fired but not edited. "new loop" adds an interval
loop to a cousin you pick. How loops work is in [jobs and
loops](jobs-and-loops.md).

## Tokens

Token use per cousin for today and the last 14 days, with output tokens
separate, and fleet totals, and the prompt-cache hit rate today and over the
14 days: the share of cacheable input the model read from its cache,
`cache_read / (cache_read + cache_creation + input)`, as the model reported
it (`-` when there was no usage to measure). The numbers come from the harness
transcripts, so this page needs `transcripts_dir` in `config/harness.toml`;
a runner cousin's come from its own `data/usage.db` and need nothing. Without
either the page says that instead of showing zeros.

## Tracker

The install-wide list of work in flight, the same list `cousin-tracker`
edits. Filter by owner, state and domain; open items come first, then
closed ones, most recently updated first. "add" takes a title, domain,
state, owner, tags and notes. Each row has "edit" (all fields, state
included: open, active, blocked, done, dropped) and a delete button. Ids
never get reused.

## Meetings

A chat shared by you and several running cousins, in rounds. "New meeting"
takes a topic and participants (a sidebar group, all running cousins, or one
by one), an optional facilitator and a timeout; the meeting view shows the
transcript, whose turn it is, and lets you post, skip the speaker or close.
See [meetings](meetings.md).

## Settings

- **cosmetic**: accent hue and saturation, chat reveal speed (off, slow,
  normal, fast) and effect (plain, glitch, matrix, typewriter, boot), font
  scale. Stored in your browser.
- **visibility**: show hidden cousins and loops.
- **process control**: "restart console". The console exits and expects its
  service manager to start it again (about 4 seconds). Logins survive the
  restart. If it isn't running under a supervisor, it warns you that restart
  means stop. Cousins are restarted from their inspector.
- **account**: who you're logged in as, change password, log out. Without a
  users file it shows the `cousin-console adduser` line instead.

## Rough edges

- A page can lag its store by its poll interval (2 to 15 seconds depending on
  the page). When they disagree, the store is right.
- There's no model editor and no way to create a cousin's first login or
  approve its MCP server from the page; those are `cousin.toml`,
  the pane, and `cousin-mcp approve`.
- The pane is a terminal over HTTP, not a real PTY. Normal typing, arrows,
  Ctrl keys and F1 to F4 work; exotic key sequences don't.
