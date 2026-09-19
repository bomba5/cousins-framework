# Changelog

The version lives in `pyproject.toml`. `cousin-version` prints it and
`cousin-version bump [major|minor|patch]` changes it. Newest first.

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
