# Changelog

The version lives in `pyproject.toml`. `cousin-version` prints it and
`cousin-version bump [major|minor|patch]` changes it. Newest first.

## 1.2.1 - 2026-09-20

- `cousin-spawn <slug> --sync-template` crashed with a TypeError instead of
  running. The `@traced_cli("cousin-spawn")` decorator sat on the internal
  `_sync_template` helper rather than on `spawn_main`, so the call from
  `spawn_main` hit the tracing wrapper's `(argv=None)` signature. Shipped in
  1.0.0 and not caught because the tests called `template_sync.sync()`
  directly; there is now a test on the CLI entry itself. `cousin-spawn` is
  traced again, this time at its real entry point.

## 1.2.0 - 2026-09-20

- Template sync: the MCP registry sync is additive at every level, not
  only at the top. Before, it compared top-level tool names only, so a
  command or a property added inside a tool block a cousin already had
  never reached that cousin, and no start or flip could fix it. Two live
  cases: `cousin-memory obsolete` was missing from 9 of 11 cousins, and
  `[tools.meeting]` had arrived without its commands, leaving the meeting
  tool with nothing to call. The sync now appends any table the shipped
  registry has and the cousin's lacks, and adds a missing key to a table
  they share. A value the cousin already has is never changed, so an
  edited description or argv survives (tracker #32, found by juno).

## 1.1.0 - 2026-09-19

- Jobs: closing a shell job ends its processes. The command runs in its
  own process group (the group id is stored on the job); `cancel`,
  `done` and `fail`, and a close from the console, SIGTERM the group and
  SIGKILL what is left after 3 s. Before, `cancel` signalled only the
  runner, so a child such as `ssh host tail -F` kept running for good.
- Jobs: a finished job whose group still has processes is shown as a
  leak in `cousin-job list` and `show` (with the pids). Only processes in
  the job's own group that started after the job are counted or
  signalled; nothing is killed by name.

## 1.0.3 - 2026-09-19

From install re-test 5 (a clean Ubuntu 24.04, the docs followed literally;
verdict: works as written).

- Tests: the flip tests no longer leave a real chat server running on
  :8100 after the suite (the respawn launched one for a temp home).
- Install docs: a stand-in agent for trying it without a Claude login;
  the console user is added before the console starts; a piped password
  for scripted installs; the first-run theme picker; the test summary and
  its duration; `~/.claude.json` entries and the ollama group on uninstall.
- README quick start agrees with the install guide: `python3-venv` and
  tmux, the clone location the LAN drop-in expects, `cousin-tool-surface`,
  and port 8600.

## 1.0.2 - 2026-09-19

- Console on phones: nothing is cut off on the right any more, on any view,
  the inspector and the modals included. Single-column grids used a bare
  `1fr`, whose minimum is the widest child's content, so one unbreakable line
  (an activity line, a path, a table) made the column wider than the screen,
  and `.main`'s overflow hid the rest. Grids now use `minmax(0, 1fr)`, long
  strings break, wide tables scroll in their own panel, the tracker becomes
  labelled cards, and the inspector starts below the iPhone status bar.
  Desktop is unchanged.

## 1.0.1 - 2026-09-19

- Tokens page: the 14-day series showed only today. It read only the
  cousin's current session, and a cousin that flips daily starts a new one
  every day. Every transcript in the window is read now, subagents
  included.
- Tokens: each message counts once. The harness writes one line per
  content block, each repeating the message's usage (208 lines for 118
  messages in a real transcript), and cache writes were added twice (the
  per-TTL split on top of cache_creation_input_tokens). Totals were
  inflated by roughly 2x.

## 1.0.0 - 2026-09-19

The framework part of every cousin's CLAUDE.md follows the template.

- **Breaking:** `cousin-meeting teach` is removed. It copied one section
  and one tool into existing cousins; the template sync below does that
  for every section and every tool, by itself.
- Every start and flip syncs the part of `CLAUDE.md` above the marker
  line with `templates/cousin-CLAUDE.template.md` before the agent reads
  it: each framework section gets the current template text filled in
  with the cousin's name, slug, port and role; `## Identity` and
  `## Voice` stay the cousin's own; a section the template doesn't have
  is kept; below the marker nothing changes except a leftover copy of a
  framework section that is word for word the template's. The old file
  goes to `data/claude-md-backups/`. Tools the shipped MCP registry has
  and the cousin's lacks are appended to its `mcp-registry.toml`.
  Before, a template change reached only cousins spawned after it.
- `cousin-spawn <slug> --sync-template` shows the diff; `--apply` writes it.

## 0.12.1 - 2026-09-19

- Console pane: no more flicker while a cousin works. A full frame was
  applied as term.reset() and then a write, so the screen went blank for
  a render between the two, and a busy pane (the spinner changes it on
  every poll) blinked twice a second. The frame is now one write that
  clears and repaints; the per-frame refocus that reset() needed, which
  also pulled focus from other inputs, is gone.
- Telegram: the first-time setup works. With no operator the bridge
  cannot run, so nothing recorded the Start press and "waiting to be
  added" stayed empty. While no bridge polls, the console reads the
  bot's pending messages itself (getUpdates without an offset: nothing
  is confirmed, nobody is served).

## 0.12.0 - 2026-09-19

Telegram provisioning in the console.

- A Telegram panel in each cousin's inspector: status, the bot's @name
  (getMe check), enable switch, a write-only token (stored at
  `config/telegram/<slug>.token`, 0600, never answered), the allowed
  operators by numeric id, and "waiting to be added": the last people
  who pressed Start and were refused, added with one click, so nobody
  looks up a Telegram id.
- The bridge belongs to its cousin: it starts with the cousin when
  `[telegram]` is enabled and complete, stops with it (pid in
  `data/telegram.pid`, log in `data/telegram.log`), and restarts on a
  token or operator change. No per-cousin service unit is needed.
- Routes `GET /api/cousins/<slug>/telegram` and `POST .../telegram/token`,
  `.../operators`, `.../enabled`, `.../check`; `cousin_lib.telegram_admin`.

## 0.11.0 - 2026-09-19

- Chat (tracker #24): `cousin-reply --image` records the picture on the
  reply row (`attachment_kind` / `attachment_path`), the convention
  `cousin-image` already used, instead of a side copy in
  `chat/inbound/<reply id>.<ext>` that the row didn't mention. The file
  is copied to `chat/images/`. The console shows it as before, and the
  Telegram bridge now relays it; before, only the caption reached
  Telegram. Older replies keep showing from `chat/inbound/`.
- Chat: `cousin-reply --video <file>` (MP4, WebM, MOV, M4V) attaches a
  video the same way, copied to `chat/video/`; the MCP send tool has a
  matching optional `video` field (a cousin's own `mcp-registry.toml`
  gets it when regenerated from the example).
- Telegram bridge: an attachment over Telegram's bot upload limit (10 MB
  for a photo, 50 MB otherwise) or missing on disk is logged and skipped
  before the upload. A missing file used to read as a transient error
  and would have retried forever.
- Telegram bridge: a rejected send or relay logs what the server said
  (Telegram's `description`, such as "chat not found", or the chat
  server's `error`), not only "HTTP Error 400: Bad Request".

## 0.10.0 - 2026-09-19

- Every cousin's memory index is kept fresh by default: `cousin-loops`
  checks each home every 5 minutes and refreshes the keyword index and
  the vectors when a source changed, embedding only what changed, one
  home at a time on a background thread (never several homes against the
  embedding service at once, never blocking the tick). Before, a cousin's
  index moved only when that cousin searched, so a quiet cousin fell
  behind. `memory_search.refresh_if_stale(home)` is the unattended entry.

## 0.9.0 - 2026-09-19

- Telegram bridge (tracker #13): a reply with an image, video or voice
  attachment is uploaded as the file (sendPhoto, sendVideo, sendAudio).
  Before, the send carried only a caption and Telegram rejected it.
- Telegram bridge: a photo from an operator goes in as an image
  attachment with its caption, largest size, up to 10 MB; a stranger's
  photo is never downloaded. Voice, video and documents are still
  skipped with a log line.
- Telegram bridge: `cousin-telegram --home <home>` finds the framework
  root from the home when `FRAMEWORK_ROOT` is unset, and no root at all
  is exit 2 with a message instead of a Python traceback.

## 0.8.2 - 2026-09-19

- Telegram bridge (tracker #18): a failed relay no longer loses the
  message. The update offset and the reply position moved before the
  relay ran, so a chat server that was down or a failed Telegram send
  dropped the message for good. Each now moves only past what was
  delivered; transient errors are retried, and a permanent 4xx is
  logged and skipped so it can't wedge the bridge.
- Telegram bridge: its position is saved in `data/telegram-bridge.json`,
  so a restart resumes instead of re-sending the reply thread from the
  start, and a first start begins at the end of each thread.
- Telegram bridge: each operator's thread is read and its replies go to
  that operator. Before, only the first operator's thread was read, so
  a second operator's replies never went out, and every reply went to
  every operator. An operator with no `name` is now always `operator`,
  not their Telegram first name.

## 0.8.1 - 2026-09-19

- Boot packet: Required Boot Action 5 now gives the same pre-exit order
  as the flip and the clean stop (STATUS.md, active-threads, durable
  memories, handoff.md LAST). It still said "three artifacts" with the
  handoff second, and a cousin following it could end its session
  before its memories were saved.

## 0.8.0 - 2026-09-19

- Meetings: delete a meeting and its transcript (`DELETE /api/meetings/<id>`,
  `cousin-meeting delete`, a Delete button in the console); a running
  meeting's participants are told it is over.
- Meetings: every turn line says which turn is yours ("2 of 3") and the
  numbered speaking order with the current speaker marked; the console
  shows the same order above the thread.
- Meetings: every participant gets one line when a meeting opens (it is
  in the meeting, with whom, wait for its turn) and one when it closes.
  Before, a cousin not called in the first round had no way to know it
  was in a meeting.

## 0.7.0 - 2026-09-19

The memory scope `both` is retired.

- `[memory] scope` is `private` or `shared`. `shared` means the cousin may
  propose memories to the shared tier; every cousin reads the shared tier
  and keeps its private memory whatever its scope, so `both` behaved
  exactly like `shared` and promised a difference that never existed.
- Backward compatible: a `cousin.toml` with `both` is read as `shared`, and
  spawn, the console and `--memory-scope` still accept `both` and store
  `shared`. The spawn dialog offers only the two.

## 0.6.0 - 2026-09-19

Meetings: a chat shared by the user and several running cousins.

- `cousin_lib/meetings.py` over `data/meetings.db`, and `cousin-meeting`
  (list, show, open, post, say, pass, minutes, skip, close, teach). Rounds
  by default: the user posts, each participant speaks once in order and
  is woken only on its turn, with everything said since its last turn;
  `@slug` asks one participant; out of turn is refused. `cousin-loops`
  retries undelivered turns and skips a silent or stopped speaker. An
  optional facilitator writes the minutes at closing.
- Console: a Meetings page (list, new meeting by sidebar group, all or
  single cousins, the thread with a turn banner, post, skip, close), the
  `/api/meetings` routes and the `meeting-change` event.
- Every cousin learns it: a Meetings section and a `cousin-meeting` row in
  the CLAUDE.md template, `cousin-meeting teach [--apply]` for existing
  cousins, the `meeting` MCP tool, and the how-to line in every turn.
- Docs: meetings.md, console API, commands, MCP.

## 0.5.0 - 2026-09-19

The shared tier reaches every cousin.

- New boot-packet layer 2, "Shared Rules and Fleet Memory": canonical
  shared entries with `kind: rule` in their frontmatter are quoted in
  full, every other entry is one index line (file and description).
  Every cousin gets it, whatever its memory scope; pending proposals
  never do. The later layers move down one (Tool Surface is 9,
  Required Boot Actions 10).

## 0.4.0 - 2026-09-19

Console sidebar groups follow the user, not the browser.

- Sidebar groups are saved on the server per console user
  (`GET`/`POST /api/prefs/sidebar`, `data/console-prefs/<user>.json`),
  so another browser, the phone and a restart keep them. A layout made
  earlier in a browser is uploaded the first time no server copy exists.
- Fixes since 0.3.0: the daily flip skips a stopped cousin instead of
  starting it; the login cookie is persistent and sessions survive a
  console restart (token hashes in `data/console-sessions.json`); PNG
  home-screen icons.

## 0.3.0 - 2026-09-18

Memory fills in its own truth levels, and the docs are new.

- The framework writes L1 entries when it changes a cousin: flips,
  starts and stops, model, effort and auth changes, chat imports, role
  changes, transplants, a flip that died, a chat server respawn.
- A finished job (done or failed) lands in its cousin's memory at L2
  with its title, exit code and summary.
- Hedged sentences mined from a transcript at flip land at L4
  (hypothesis) instead of L3.
- `cousin-memory obsolete TOPIC --why ...` retires a topic (L5): it
  leaves the distilled views, the history stays. Also a "mark obsolete"
  button in the console explorer and an `obsolete` command on the MCP
  memory tool.
- The documentation is rewritten from scratch: a short README, one page
  per area, and the API details under `docs/reference/`.

## 0.2.0 - 2026-09-18

Remote cousins, with the console as the queen.

- Turn on the hive with `config/hive.toml` and the console answers the
  queen routes under `/hive/` on its own port. Without the file there's
  no hive at all. See [remote cousins](docs/remote-cousins.md).
- The spawn dialog has a "Remote (another machine)" option. It builds
  the node archive and gives you a one-time download link (15 minutes,
  one download) and the install command to run on the other machine.
- Remote cousins show up as cards: host:port, last seen, online or
  offline, runtime version. You can chat with them through the console,
  and revoke and forget them from the card.
- Nodes check in with the queen on start and every `checkin_seconds`.
  A node that listens on its network answers chat only to its own token.
- Queen recall is by meaning when `config/embedding.toml` is set up,
  and by substring otherwise. The inbox supports long polling.
- New `cousin-hive` commands: `revoke`, `forget`, `nodes`, and
  `import-legacy` to bring over tokens and memory from an older queen.
  Existing `hive.db` files are upgraded in place.
- Memory writes carry a truth level. `cousin-memory remember` stores
  one fact with its level, and `--level operator` needs a `--cite`.
- The Memory view is now a layered explorer (raw entries, digests,
  distilled views, decisions, notes and more), and removing an entry
  can be undone.
- The cousin inspector has a read-only file browser for the cousin's
  home. `.secrets` is never shown.
- Chat renders Markdown and Mermaid. Images, video and audio show
  inline, there's a media viewer with previous/next, and a media on/off
  toggle in the chat header.
- The Jobs log panel scrolls, follows live output, and says when a job
  has no log.
- `config/external-peers.toml` lets a cousin message a cousin on
  another framework install on the same host.
- Commands find the framework root from `COUSIN_HOME` when
  `FRAMEWORK_ROOT` isn't set.
- Chat delivery no longer types into a pane that shows a menu or
  prompt (the attention patterns in `config/harness.toml`), and presses
  the insert key first when the input box is in vim normal mode.
  A skipped delivery stays due instead of counting as sent.
- The console pane: the cursor sits where tmux puts it, and the mouse
  wheel scrolls full-screen programs properly.
- The console's version links to the repository and the commit.
- With the `api_key` auth mode, a stray credentials file without a
  login in it no longer counts as a login.
- Uninstall removes more leftovers (timer stamps, Claude Code's cache
  and state), and the install steps say that the console is open to
  anyone until you add the first user.

## 0.1.0 - 2026-09-18

The first numbered release.

- Cousins: one `CLAUDE.md` template, `cousin-spawn` to create and
  remove them, a chat server per cousin, and cousin-to-cousin messages
  and replies.
- Memory: private memory per cousin with keyword and optional semantic
  search, decisions, raw folding and distillation, and a shared tier
  that needs review.
- Lifecycle: boot packets, the flip with a handoff, the self-portrait,
  reincarnate and transplant.
- Recurring work: the loops daemon, heartbeats, one-shot schedules,
  timed and size-triggered flips, jobs, and the tracker.
- Auth per cousin: the harness's own login (`claude`, the default) or
  a per-cousin API key (`api_key`). `cousin-auth` and the console
  switch it and restart the cousin on the same session.
- The web console (`cousin-console`): cousins, chat, pane, jobs, loops,
  memory, login, and the running version in the top bar.
- Optional extras: media generation, a Telegram bridge, cousins on
  other machines, and the CLIs as MCP tools.
- Operations: systemd units, the sweep, the tool list, backups, and the
  contamination gate.
