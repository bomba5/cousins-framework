# The console

The console is the web page I run the whole install from: every [cousin](glossary.md#cousin)'s
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

- Every `/api` route needs a session except login, `GET /api/auth/me` and
  `GET /api/version`.
  There is no bypass for localhost or the LAN.
- A user signs in on the login form and gets a `console_session` cookie
  (HttpOnly, SameSite=Strict). Idle sessions expire after 30 days.
- Sessions are saved in `data/console-sessions.json`, so restarting the
  console keeps everyone logged in.
- All users can do everything. There are no per-user permissions.

If `console-users.json` exists but can't be read (bad JSON, no users), the
console closes: every `/api` route answers 503 with the reason, and the
startup line in the log says `CLOSED`. `adduser` refuses to write over a
broken file. Restore it from a backup, or delete it and run `adduser` again.

Settings has an account panel to change your password (at least 8
characters; your other sessions stay valid) and log out.

## Layout

The top bar has a button that collapses the sidebar, the console's icon and
brand, and the
version the console is running: `v0.1.0 0a119f6`. In a git checkout with a
browsable `origin` remote, the version links to the repository and the
commit hash to that commit. The version is read when the console starts, so
after a `git pull` it shows the old one until you restart it. On the right
are an eye toggle that shows or hides hidden cousins and loops, the logged-in
user, and a clock.

The sidebar lists the pages (Ctrl/Cmd + 1 to 7, 0 for Settings) and below
them your cousins. The Overview entry carries a count when cousins are
waiting on a person, and the foot of the sidebar shows the next daily [flip](glossary.md#flip):
the earliest `flip_at` among the running cousins, how far away it is, and
who flips then. Click a cousin to open its chat. A stopped cousin is
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

The landing page. It opens with one sentence of fleet health: how many
cousins are running, how many need you, and when the next daily flip is
("5 of 6 running · 1 needs you · next flip 04:00, in 8h 28m"). Under it a
strip of the numbers you compare: cousins running, tokens today, jobs in the
last 24 hours (running and failed), and loops with their recent fires or the
loops daemon's complaint.

The fleet table has one row per cousin, what needs you first, then
warnings, then the running ones, then the stopped ones. The columns:

- **state**, in words beside its dot: `working`, `idle`, `needs you`,
  `rate limited`, `errored`, `enrolled` (a [worker](glossary.md#worker)) or `stopped`. A cousin
  "needs you" when its [runner](glossary.md#runner) waits for a permission
  or a login; the reason replaces the role line in the cousin column.
  A stopped cousin is never flagged: stopping it was your decision.
- **cousin**: name, slug and role.
- **runner**: the [lane](glossary.md#lane), read from the row: the runner's own kind (`sdk`,
  `opencode`, ...), `tmux`, `worker` or `remote`.
- **model** and effort, what the next start renders.
- **next flip**: the cousin's own `[lifecycle] flip_at` and how far away it
  is on your browser's clock; `default` when it takes the install default
  (the route does not say which time that is), `never` for an opt-out.
- **beat**, **operator** and **tokens today**.

Click a running cousin's row to open its chat. The fleet rows carry no
generation or context fill, so the table shows neither.

Beside the table (under it on narrower screens) sit the host (hostname,
kernel, uptime, CPU and load, memory, disk for `/`, network rate and totals,
how long the console has been up) and an activity rail: jobs from the last
24 hours, loop and heartbeat fires, and any flip the console saw while the
page was open, newest first.

## Cousins

One card per cousin. A card shows the name and slug, its state in words, the
same reading as the overview's (`working` if the pane changed in the last
minute or the runner is mid-turn, `idle`, `needs you`, `enrolled` for a
worker, or `stopped`), the role, and
a row of facts: chat, memory scope, operator, heartbeat, flip time,
model, the agent's pid and uptime, the last activity line and tokens
spent today. A runner cousin's card shows `chat · console` (the console
serves its chat), and a runner line: the last state the runner recorded and
the contract items it declares it does not support, if any.

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
- **Identity.** Slug, type, home, operator, scope, tmux session, chat
  state, heartbeat, lane (with the account, held and auto start), pid,
  uptime, flip time, activity. Three of them have an edit button:
  - operator: one line, up to 64 characters, no leading or trailing spaces.
    Needs a restart (the runner reads it at start).
  - scope: `private` or `shared` (may propose to the [shared tier](glossary.md#shared-tier)). Applies at once.
  - heartbeat: whole seconds from 60 to 2592000 (30 days). Applies at the
    loops daemon's next tick.

  Each writes only that key in `cousin.toml` and keeps the rest of the file,
  comments included. When a restart is needed the field says "restart to
  apply".
- **Agent** and **cousin settings.** See [Agent settings](#agent-settings).
- **Telegram.** The cousin's Telegram bridge: status (enabled, token set,
  bridge running, what is missing), the bot's @name after a check, an enable
  switch, a write-only token field (stored at `config/telegram/<slug>.token`,
  never shown again), the allowed people by numeric id, and "waiting to be
  added": whoever pressed Start on the bot and was refused, with an add
  button, so nobody has to look up a Telegram id. The bridge starts and
  stops with the cousin. See [chat](chat.md).
- **Account** (a runner cousin). The account it runs on, "check auth"
  (`cousin-runner --check-auth`: is it logged in, no model call) and, on the
  `sdk` lane, "validate", which spends one smallest model turn on a
  throwaway client and asks first. Both run as the cousin's long operation
  with their steps shown. While `data/login-required.json` stands, the
  panel shows its action line and "log in here", the account's login from
  the Accounts page. See [Accounts](#accounts).
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
- **MCP.** Three tabs: the tool registry, the `.mcp.json` servers and
  `cousin-mcp`. See [MCP and policy](#mcp-and-policy).
- **Policy.** The cousin's `policy.toml`. See [MCP and policy](#mcp-and-policy).
- **Flip.** The last flip the console ran (idle, running, done, failed, or a
  stale marker from a crashed flip) with its generation, and any pending
  timed flip with a cancel button.
- **Buttons.** start / stop, kill (does the same as stop), flip and restart
  (only while running), and hide / unhide. A hidden cousin drops out of the
  sidebar and the Cousins page until you turn on "show hidden" (the eye in
  the top bar, or Settings). Hiding changes nothing else.

On a tmux-legacy cousin, model and effort are identity rows (`[runtime]`).
On a runner cousin they are in the agent panel, where its runner reads them.

### Agent settings

Two inspector panels under identity edit the rest of `cousin.toml`, each
value, choice and default served by the console, so nothing in the page can
drift from what the runner accepts.

- **Agent.** The kind (`[agent] runner`) is shown read-only with "switch
  kind", which opens the kind-switch dialog (a migration). A `held` badge
  says a stop keeps the runner down until its next start. Then one row per
  `[agent]` key the kind reads: account (only the accounts that run on this
  kind), model (free text with suggestions; on `opencode`
  `"<provider>/<model>"` on a provider the account holds, never a Claude
  model), effort, auto start, [rollover](glossary.md#rollover) percentage, side sessions
  (`[agent.sessions]`, `sdk` only; `operator` and `system` always stay on the
  primary), the reply and peer gates (`reply_gate`, `peer_gate`, `sdk` and
  `opencode`; see [configuration](configuration.md#agent-runner)), strict harness
  (`strict_harness`; see [configuration](configuration.md#agent-strict_harness)),
  the opencode keys (`small_model`, `shell_env`,
  `opencode_models_fetch`; `opencode_bin` read-only) and the tmux kind's
  `env_allow`, shown beside the names the pane always gets and the hard deny
  that always wins. A deprecated `api_key_file` shows a warning. "unset"
  removes a key so its default applies. Save checks every change the way the
  runner does and writes them in one go; a new `sdk` model is first checked
  with one smallest model turn on the account being saved with it, shown as
  the cousin's long operation. Every change applies at the next start: the panel
  says "restart to apply" and offers a restart (click twice). A cousin with
  no `[agent] runner` has no agent settings and is refused by 2.0.0
  ([migrating](migrating.md#a-cousin-with-no-runner)).
- **Cousin settings.** Name, peer visible, recall lines and keyword recall,
  the review batch and review model, the daily flip time (`HH:MM`, `never`,
  or blank for the install default, with the effective time shown) and commit
  attribution: the install default (with where it comes from), on or off.
  On a tmux cousin, commit attribution also rewrites its harness settings
  file, where that lane reads it, and says so when your own
  `includeCoAuthoredBy` or `attribution` there says the opposite and wins. The tmux session and
  the `[session]` hooks are shown read-only.
- **Plugins.** Only on an install with [plugins](plugins.md): a checkbox per
  plugin `config/plugins.toml` declares, writing the cousin's `[plugins]
  enabled`. It applies at the next start (restart offered); a tmux cousin is
  told its pane does not get a plugin's tools. Without plugins the section is
  not there at all.

The chat header's effort select shows only on a kind that reads an effort.
The install-wide `[agent]` defaults are on the System page.

### MCP and policy

Two inspector panels and one Settings panel edit what a cousin's model can
reach. Each change is checked by the parser the runner reads the file with,
keeps every line it does not touch, and applies at the next start: after a
save the panel says so and, on a running cousin, offers "restart now" (a
second click confirms). A file that changed on disk since the panel loaded it
is never overwritten; reload and make the change again. (The model writing the
file in the milliseconds between that check and the console's write loses to
the console.)

- **Tool registry** (`<home>/mcp-registry.toml`): a switch per tool, and the
  ceiling, timeout and output cap. More enabled tools than the ceiling is
  refused, and so is enabling a tool the runner has no handler for. Adding a
  tool is a file edit. A cousin without a registry of its own shows the one
  it reads and offers "copy the install default here".
- **Servers** (`<home>/.mcp.json`): stdio servers (command, args, env) and
  http or sse servers (url, headers). The runner passes these to the agent CLI
  in a private file, never on its command line, but `.mcp.json` is plain text
  the model can read and edit, and a stdio server's args are on its own
  command line, which any user on the host can read, so a value that
  looks like a secret is refused and the panel offers the `${VAR}` reference
  to write instead; set the variable in the runner's environment. A literal
  secret already in the file is never shown. The `cousin` entry and entries
  of no known shape are kept as they are. The panel also shows what the
  runner loaded and skipped at its last start.
- **cousin-mcp**: selftest (the registry, where each command resolves, the
  SDK), last connection (what the harness logged, its stderr included) and
  approve (trusts the home and enables `cousin` in the harness settings file
  `config/harness.toml` names; only the tmux lane has it). Approve rewrites
  that whole file and a live harness session writes its own copy back, so
  approve while the cousin's session is stopped; you type "approve" to
  confirm.
- **Policy** (`<home>/policy.toml`): `deny_tools` and `ask` as chips,
  `deny_bash_patterns` one per line (compiled on save; on opencode a pattern
  JavaScript cannot compile is flagged, since it would deny every command),
  and `outbound_filter`. `mcp__cousin__handoff` can never be denied: every
  generation ends through it. A change that removes a deny entry or switches the
  filter off lists what it removes and needs "save and loosen". A guardrail,
  not a sandbox: the model can rewrite the file.
- **Settings > mcp tool registry**: the install default,
  `config/mcp-registry.toml`, with "copy from example" while it is absent.

Replacing a registry with the default, or a broken `.mcp.json` or
`policy.toml`, asks you to type "replace".

### Kind and migration

The inspector's "kind and migration" panel shows the cousin's lane and its
migration and kind switch records, and opens the switch dialog, "switch kind"
(`cousin-migrate --to sdk|tmux`). A cousin with no `[agent] runner` shows the
2.0.0 refusal line instead: the tmux-lane migration is 1.x only
([migrating](migrating.md#a-cousin-with-no-runner)). The agent panel's switch
button opens the same dialog. The routes are in
[the API reference](reference/console-api.md#kind-switch-and-migration).

- **plan**: a checklist of every check with its detail, the steps, and
  ready or not. It writes nothing. The kind switch keeps the
  cousin's account and its session: the source stops at idle and the
  target resumes it.
- **apply**: only after a ready plan for the same options, with the
  [supervisor](glossary.md#supervisor) up, and a second click. It runs in the background with its
  steps as they happen (trust, close, toml, cursor, start, notice, verify),
  one switch at a time across the fleet. When the
  switch's verify finds the tmux pane waiting on the trust dialog, the step
  says so and a button opens the pane: accept the dialog there (arrows and
  Enter). Keys go into a tmux-kind pane only while it waits on a person;
  everywhere else the runner types, and the chat is the way in.
- **check**: the exit criterion as a report ([inbox](glossary.md#inbox) rows, tool calls with no
  result, recorder hook errors, the runner's config against the cousin's),
  optionally since a time and with one validating [turn](glossary.md#turn).
- **roll back**: offered while a record allows it, the kind switch back to
  the kind it came from. A second click confirms.

Two things the panel does not do, and it says so: adopt a live pane (a
tmux-kind start adopts its pane on its own), and switch the whole fleet
(`--all --keep-going`; switch one cousin at a time).

### Lifecycle

The inspector's "lifecycle" panel runs `cousin-reincarnate` and
`cousin-transplant`, each in the background with its steps:

- **reincarnate**: a new one-line role, the memory kept. A snapshot, the
  bequest (the cousin answers it on the flip's own handoff request), the
  role rewritten in CLAUDE.md and cousin.toml, then a flip. A second click
  confirms.
- **transplant**: a donor, a recipient and a mode: soul-donation (the
  recipient carries the donor's memory), body-swap (the two trade identity
  files, name and role) or merge (the donor's memory braided into the
  recipient's). Both are snapshotted, the mode applied, both flipped. It runs
  on the recipient while the donor is held, so nothing else starts on either;
  the donor's own panel says so. A merge is confirmed by a second click; a
  soul donation wants `donate <donor> <recipient>` typed, a body swap
  `swap <donor> <recipient>`. Each request and its outcome is written to
  `data/lifecycle/audit.jsonl` with the console user who asked.

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
| operator | the person it answers to; blank is allowed |
| kind | the runner kinds the console serves; `COUSIN_DEFAULT_RUNNER` preselects one, else `sdk` |
| account | the accounts that run on the chosen kind |
| model, effort | only where the kind reads them; `config/harness.toml [agent]` `models`, `default_model` and `default_effort` are the suggestions (a built-in list without `models`); the model is free text, `"<provider>/<model>"` on opencode, with the account's providers suggested |
| heartbeat | seconds, default 3600 |
| memory scope | private, shared |

"create cousin" creates it and then starts it. If the create works and the
start fails, the cousin exists and you can start it from its card once you
fix the problem (usually the supervisor, or the account's login).

The kinds and accounts come from the console (`GET /api/spawn/options`),
never from the page. A cousin is started through `cousin-supervisor`
(where no supervisor runs, the start answers 503). The Docker install's
`compose.yml` sets `COUSIN_DEFAULT_RUNNER`, which only preselects the kind.
`COUSIN_DEFAULT_ACCOUNT` only preselects too: a runner kind created with the
account left blank gets it (unset, `host`).
There is no worker option: spawn has no backend for `[cousin] type =
"worker"` yet.

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
- "home chat", when `home_cousin` is set in `hive.toml`: the node's
  `[tell-home: ...]` reaches that cousin through this queen,
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

The console serves a local cousin's chat itself, from the cousin's own
`chat.db` (no cousin runs a chat server of its own), and forwards a remote
hive node's chat to the node's chat server. The console stores no messages;
what you read is the cousin's history. A cousin with no runner kind has no
chat in 2.0.0.

The [thread](glossary.md#thread) you see is picked like this: `?user=` in the URL, else the
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
- **terminal** (a tmux cousin) or **reasoning** (a runner cousin): opens
  the pane beside the chat, when it is closed.
- **fullscreen**: hides the rest of the console.

On a phone, and whenever the chat column is narrower than 740 px (the pane
open beside it on a narrow screen), the header is one row: the effort
select, a search button that opens the search field (its x closes and
clears it), a **⋯** menu with archive, archived / live and media on / off
(and whose thread you are in), then the pane and fullscreen buttons as
icons. The status line under the name never wraps: it is cut with an
ellipsis, and on a phone it holds only the state and the model.

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

### Beside the chat: the pane

The pane sits beside the chat rather than in place of it, so you watch the
cousin work while you talk to it: a runner cousin's reasoning [stream](glossary.md#stream), or a
tmux cousin's terminal. Where the window is wide enough for both it opens by
default; its "x" closes it and the header's button brings it back, and the
choice is kept in your browser. On a phone the open pane takes the whole
width, as it always did.

With a [plugin](plugins.md) that has a console page enabled on the cousin, a
strip of tabs sits over the pane: the reasoning (or terminal) tab, and one tab
per such plugin, showing the plugin's page (an iframe of
`/plugins/<name>/...`, served through the console's proxy behind your login).
A plugin whose page is placed `"chat"` shows it instead as a strip on top of
the chat's messages, which you can collapse, resize by its bottom edge, or pop
out into its own window ([plugins](plugins.md#what-happens)). Without such a
plugin there is neither.

### The terminal pane

For a tmux cousin the pane is its tmux session, rendered with
xterm. It's interactive: what you type goes to the session. Keys are batched for 40 ms and mapped to tmux
key names; unusual escape sequences are dropped rather than sent as a stray
Escape. The pane fits itself to the browser and resizes the tmux window to
match.

A tmux-kind runner cousin's pane is shown this way only from its kind switch
and migration panel, to answer the trust, bypass or MCP approval dialog. Its
runner types into it, so keys go in only while such a dialog shows, only
arrows, Enter, Escape, Tab, Backspace, a digit, y or n, and nothing after an
Enter until the next screen has settled; it keeps its fixed size. The login
and onboarding flows take several screens: the panel says to finish them in
a terminal attached to the pane.

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
session, so for it the pane is its reasoning stream instead: every state
change, turn, text, thinking block, tool call and tool output the runner
records, live, as it records them (read from the cousin's own
`data/stream/`). It opens at the newest 200 events, not the whole history;
a dropped connection picks up where it left off, and a runner that
restarted meanwhile is marked with a new-session line. When no runner is
running, the header says "not running" beside the last state it recorded.
By default the stream is folded. A strip under the header says what the
runner is doing now (a spinner with "thinking", the running tool's name, or
the idle state, with how long it has been at it), the Claude usage limit
as a bar with its reset day (amber from 80% or on a warning, red when
rejected), the session's model and whether it runs on the host login or an
API key, a red "login required" chip while the login needs renewing, and
the turns, tokens and estimated cost of the turns in view.
While the agent has background tasks running (a subagent or a shell it
started in the background), the strip has an "N bg tasks" chip; click it for
the list under the strip: each running task first (what it is doing, agent
or shell, how long it has run, the last tool it used), then the last five
that ended (completed, failed, stopped or killed, and how long ago; hover
for the summary). A click outside, Escape or the chip again closes it. A
task ends on its notification or on a final status update, whichever comes
first; a runner restart marks the tasks still running as lost. A stream
recorded before 1.27 has no task details, so its tasks are only counted.
When a background task ends, the agent may take a turn of its own to read
its notification; the log shows it as a turn headed "background", closed by
the usual turn line.
Below it the log holds one row per real thing: a turn under a rule (who
sent it, when, and the start of the message; click for all of it, with the
recall hits as a chip), each tool call with its result as one card (a tick
or a cross, what it was asked, how long it took; click for the input and
output), a reply as "reply" with the message, a thinking block as
"thought 2s", the model's text, and a line that closes each turn (steps,
tokens, cost, memories written, checkpoint). A message folded into a
running turn gets its own rule and recall chip. An open turn lists what
recall gave the cousin for that message: each memory by name, where it
lives, its truth level and similarity; click one to read it (a raw entry
shows its content, level, time and cite). A message recall skipped says
why (it already carried its recall, it was not a chat message, or recall
timed out). Login and credential
problems, API retries and failed tool calls with no card are one red (or
amber) line each; a session rollover is a divider line and a memory review
one line (kept, dropped, pending). The runner's setup events, fresh or
resumed, sit in one collapsed "runner started" row; thinking ticks and
bookkeeping are not rows. "raw" in the strip shows every event as its own row, as it
arrives, and is remembered per browser. Scrolled up, the view stays where
you left it and a "new events" button takes you back to the bottom.

The divider between the chat and the pane drags to resize (the pane takes
20% to 80% of the width, remembered per browser); a double-click puts it
back to half. Let go of it within 40 px of the chat's left edge and the chat
collapses: the pane covers the whole area, and the thin handle left at that
edge brings the chat back at its last width. Let go within 40 px of the
right edge and the pane closes. The collapse is remembered per browser too;
closing the pane brings the chat back, and a chat plugin strip hides and
returns with the chat. While the chat is collapsed, "send as chat" beside
the say box posts what you type as a normal chat message, stored as the
composer's are, instead of saying it to the running turn.
"interrupt", beside the say box
at the foot of the stream, ends the running turn, past its first answer
too; the button is live only while a turn runs, and the foot says what came
of it (`delivered`, or `failed` when the turn had already finished or the
agent refused). The box at the bottom says
something to the running turn as its operator: the runner writes it into the
live turn, or takes it next. What you say there is not stored in the chat,
as typing into a tmux pane is not; a login code typed there while a login
waits on this cousin is taken for the login and never reaches the cousin. The chat itself works as for any cousin:
the console serves it from the cousin's `chat.db`. `cousin-watch <slug> -f` shows the same stream in a terminal.

## Jobs

Everything `cousin-job` tracks, including the subagents and background
shells each cousin's harness hooks register on their own. Filter by state
(active, last 24h, done, failed, lost, all; last 24h is the default), by cousin
and by kind. Running jobs with a log show their last ten lines live.

Click a job to open its log. The panel loads the tail and then follows the
file every 2 seconds. It follows the end while you're at the bottom, stops
when you scroll up, and "follow" takes you back. The browser keeps the last
2 MB. A job without a log says so.

A running job has "cancel" (it sends SIGTERM when the pid is known, then
marks the job cancelled); a finished one has a delete button. A running
job whose process died without closing it shows as "lost" (amber, with
its own filter), within seconds while the console runs; jobs still
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
column starts with the truth levels, each with its colour and its count:
operator, framework, tool, conclusion, hypothesis, obsolete. Click one to
filter the raw entries to it (click more to add levels, "all levels" to
clear); the counts follow the list's other filters while a raw list is
open, and count the live entries otherwise. Under them the layers, grouped:

| group | layers |
|---|---|
| now | active state (STATUS.md, handoff, checkpoints), the MEMORY.md index |
| candidates | raw entries (daily), monthly digests, raw archive (gzip) |
| durable | [distilled](glossary.md#distilled) views, decisions log, memory files, notes, harness auto-memory |
| machinery | search indexes, recall log, trash, legacy archive |

Each shows a count and when it last changed. "insights" at the top is a
summary: entries per truth level, most recalled files and files never
recalled, hygiene flags (dangling links in MEMORY.md, daily files waiting to
be folded, distilled views behind raw), sources and the busiest topics, raw
entries per month, and the state of the keyword and semantic indexes.

Raw entries can be filtered by truth level (L0 operator down to L5 obsolete),
topic, text, source and date range, and are grouped by level (operator
first, obsolete last; "newest first" lists them in time order instead).
Each entry shows its level in words and colour, topic, content, source and
time. An operator-stated entry shows where it was cited (or says none was
stored); an obsolete one is struck through and dimmed, never hidden. Decisions show what was decided and why. File
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

### Operator actions

Under "insights" the left column has an **operator** group: what you do to
a cousin's memory yourself, the same library calls the `cousin-memory`,
`cousin-self-portrait`, `cousin-reason` and `cousin-callback` CLIs make.

| action | what it does |
|---|---|
| search | the cousin's own search: keyword always, meaning when `config/embedding.toml` is set up. Each hit says which found it; a raw hit shows its entry, a file hit opens in place. Your searches are not recorded as the cousin's recall. |
| write | remember a fact or log a decision, at a truth level. The console fills the cite with your user name and the time, plus an optional note of where it came from. A fact can also take a scope (what it holds for) and a valid-until date; its claim card then shows them, and "expired" once the date passes ([memory](memory.md#what-a-fact-holds-for-and-until-when)). |
| tensions | topics whose live claims disagree; "retire this claim" writes an entry-level obsolete mark with your reason. |
| review gate | the entries the gate holds; mark each keep or drop and apply. A drop has no undo and asks twice. More than two verdicts run as the cousin's long operation. |
| history | a topic's claims, oldest first, with their valid time; a live claim can be retired here too. |
| maintenance | distill, compact raw, compact the MEMORY.md index (preview first), reindex. Each runs as the cousin's long operation and shows its stages. |
| self-portrait | the diff between the committed portrait and its candidate, a draft from the cousin's sources, an editor, and the commit. |
| capsules and callbacks | the cousin's reasoning capsules and callback moments, read-only. |

Some acts are a person's, and the console asks for a login before it takes
them: a review verdict, a self-portrait commit (you also type the cousin's
slug, and a candidate that changed since you read it is refused), and a
change to the shared reviewer list. The operator level is the operator's
word, so only the operator account may write it: the console has no roles,
so that is the logged-in user whose name is the cousin's `[operator] name`
(case aside). The same account is the only one that may retire an
operator-level claim (or mark obsolete a topic that has a live one) and drop
one at review. Nobody else is offered the level or those buttons.

The **shared** tab also shows the reviewer list from
`config/shared-reviewers.json`, says whether you are on it, and lets a
reviewer edit it (with a second click; while the list is empty any
logged-in user may start it). Each change is written to the shared audit
with the list before and after.

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
an `sdk` or `opencode` cousin's come from its own `data/usage.db` and need
nothing (an opencode cousin's are the tokens its provider reported). Without
either the page says that instead of showing zeros.

An `sdk` or `opencode` cousin's card also shows dollars: today, over the 14
days, and one bar per day (its amount on hover), the sum of `cost_usd` in its
`usage.db` (on a login, the API-equivalent price the SDK reports). A cousin
read from transcripts shows none. With an [`[agent]
daily_cost_cap_usd`](configuration.md#agent-daily_cost_cap_usd) set, the card
shows **cap today**, spent of the limit, red once it is reached; the cap is
set on the cousin's agent panel.

## Tracker

The install-wide list of work in flight, the same list `cousin-tracker`
edits. Filter by owner, state and domain; open items come first, then
closed ones, most recently updated first. "add" takes a title, domain,
state, owner, tags and notes. Each row has "edit" (all fields, state
included: open, active, blocked, done, dropped) and a delete button. Ids
never get reused. The edit panel has an "add note" box, which appends a
dated line signed with your login, and below it the item's history,
oldest first. Saving sends only the fields you changed, so a note a
cousin appended while the panel was open survives unless you edited the
notes box too; editing that box replaces the notes, and the old text
stays in the history.

## Meetings

A chat shared by you and several running cousins, in rounds. "New meeting"
takes a topic and participants (a sidebar group, all running cousins, or one
by one), an optional facilitator and a timeout; the meeting view shows the
transcript, whose turn it is, and lets you post, skip the speaker or close.
See [meetings](meetings.md).

## Accounts

The accounts runner cousins run on: `host` (the host's own `~/.claude`) and
every entry in `config/accounts.toml`, with its kind, where it lives, the
lanes it runs on and the cousins on it. No secret is ever shown: a key or
token file reads "set (ends WXYZ)" at most.

- **check**: is the account logged in, with no model call (`claude auth
  status` under it; an opencode account's auth.json). When it is not, the
  line that fixes it.
- **add account / edit / remove**: the entry's kind and keys, written by the
  validated writer (every other line of the file kept, checked by the rules
  the runner reads it by). An edit a cousin on the account could not run
  with is refused; remove asks you to type the name and is refused while a
  cousin names the account.
- **log in / keys**: per kind. A claude-login account: "log in" (the host's
  own login asks you to tick that you mean it). A claude-token account:
  "mint a token", or paste one. An anthropic-key account: paste the key. An
  opencode account: per provider, paste its API key, or name an OAuth
  method by its opencode label and "sign in" (`opencode`'s free models need
  no key). Anthropic and Claude are
  refused on opencode.

A login runs in the background with its steps shown. The sign-in URL appears
as a link; open it and sign in. For a Claude login or token the page then
shows a code (`code#state`): paste the whole of it into the code box that
appears. That box is write-only, the code goes to the console once and only
to that login, never into a chat, and a second code is refused. An opencode
OAuth method has nothing to paste back: opencode finishes by itself, and a
browser method only completes on the console's host (its callback goes to
localhost), so prefer a headless or device method from elsewhere. "cancel
login" ends it. A login belongs to the console session that started it:
another session sees that it runs, but not its URL, and cannot send its
code or cancel it. Editing, removing or setting a key on the account waits
until the login ends.

The logins, keys and entry changes need a logged-in console user, even on
a console with no users file (add one with `cousin-console adduser`), and
refuse when the console itself was started inside a cousin: a cousin never
obtains credentials. Who started a login or wrote a key is kept in
`data/accounts/audit.jsonl` (the name and the account, never the value).
The page refreshes when another session changes an account.

## System

The install as a whole, in six tabs. The routes are in [the API reference](reference/console-api.md#system-the-system-view).

- **supervisor**: every child of `cousin-supervisor` with its state, pid, restarts and the reason it gave. Start or stop the loops daemon and each runner cousin, and rescan the registry (reload). The console itself is never stopped from its own page: its row offers the console restart. Stopping the loops daemon asks twice, because every heartbeat, loop and scheduled prompt stops with it. A Telegram bridge follows its runner.
- **schedules**: every cousin's pending one-shot prompts (`cousin-schedule`), with history on a switch. Add one for any cousin ("in 30m", "tomorrow 06:30", an ISO time) or cancel one. A cousin's own schedules are also a panel in its inspector.
- **users**: console users. Add one, reset another user's password (your own changes in Settings, with the current one), remove one by typing its name. The last user and the one you are logged in as cannot be removed. Passwords are never shown again.
- **backup**: back up now. Pick an absolute destination (remembered in this browser) and the cousins; each one becomes a long operation and a job, and lands in `<dest>/<slug>/<date>/`. A destination inside the install, or one other users can write without the sticky bit, is refused, and the snapshot is owner-only (`0700` directories, `0600` files). A copy that lands anywhere but its own directory fails with the path it landed at, nothing is deleted, and the event is logged in `data/system/audit.jsonl`.
- **agent defaults**: `config/harness.toml [agent]`: `default_model`, `default_effort` and `commit_attribution`, each shown with where its value comes from. A cousin reads them when it starts, so restart one from its inspector to apply.
- **install config**: editors for `media.toml` (with each provider's key as a write-only field), `embedding.toml`, `hive.toml`, `external-peers.toml` (with each peer's outbound and inbound token as write-only fields, and below it the outbox: messages to external peers being sent again, delivered or given up), `outbound-filter.json`, `law.md` and `net-allowlist.json`. Each save is checked by the file's own loader first, a number that does not parse is refused, and an emptied field removes its key; the JSON and Markdown files are backed up to `data/config-backups/` and refused if they changed since you opened them. The allowlist refuses a list that would lock out the address you are on, and offers the console restart it needs. `worker-cmd` is shown read-only: edit it on the host. A leftover `agent-cmd` shows there too; no runner kind reads it.

## Settings

- **cosmetic**: accent hue and saturation, chat reveal speed (off, slow,
  normal, fast) and effect (plain, glitch, matrix, typewriter, boot), font
  scale. Stored in your browser.
- **visibility**: show hidden cousins and loops.
- **process control**: "restart console". The console exits and expects its
  service manager to start it again (about 4 seconds). Logins survive the
  restart. If it isn't running under a supervisor, it warns you that restart
  means stop. Cousins are restarted from their inspector.
- **mcp tool registry**: the install default registry, see
  [MCP and policy](#mcp-and-policy).
- **account**: who you're logged in as, change password, log out. Without a
  users file it shows the `cousin-console adduser` line instead.

## Rough edges

- A page can lag its store by its poll interval (2 to 15 seconds depending on
  the page). When they disagree, the store is right.
- There's no model editor and no way to create a cousin's first login from
  the page; those are `cousin.toml` and the pane.
- The pane is a terminal over HTTP, not a real PTY. Normal typing, arrows,
  Ctrl keys and F1 to F4 work; exotic key sequences don't.
