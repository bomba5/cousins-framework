# Changelog

The version lives in `pyproject.toml`. `cousin-version` prints it and
`cousin-version bump [major|minor|patch]` changes it. Newest first.

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
