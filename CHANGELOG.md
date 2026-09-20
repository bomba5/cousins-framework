# Changelog

The version lives in `pyproject.toml`. `cousin-version` prints it and
`cousin-version bump [major|minor|patch]` changes it. Newest first.

## 1.5.1 - 2026-09-20

### Fixed
- WAL mode on `jobs.db`, `scheduled.db`, `hive.db` and the memory indexes (#38)

## 1.5.0 - 2026-09-20

### Added
- `cousin-mcp --last-connection`: why a cousin's MCP server failed, with the
  server's own stderr (#36)
- The boot packet reports a failed MCP connection
- `mcp_logs_dir` in `config/harness.toml`

## 1.4.0 - 2026-09-20

### Added
- Every cousin flips daily, whether or not its `cousin.toml` says so
- `default_flip_at` in `config/harness.toml`; `flip_at = "never"` opts a
  cousin out
- `cousin-loops flips`: each cousin's flip time and where it comes from

## 1.3.1 - 2026-09-20

### Fixed
- Additive column migrations raced between two openers of the same database
- The console reports a serving thread that outlives `stop()`

## 1.3.0 - 2026-09-20

### Changed
- A tool that does not validate is skipped with a warning instead of killing
  the MCP server. Unparseable TOML and the tool ceiling stay fatal (#35)
- `cousin-mcp --selftest` exits 1 when a tool was skipped

## 1.2.2 - 2026-09-20

### Fixed
- An added registry table goes in with its own tool, not at the end of the file

## 1.2.1 - 2026-09-20

### Fixed
- `cousin-spawn <slug> --sync-template` crashed instead of running

## 1.2.0 - 2026-09-20

### Fixed
- The MCP registry sync is additive at every level. A command or property
  added inside a tool block a cousin already had never reached it (#32)

## 1.1.0 - 2026-09-19

### Fixed
- Closing a shell job ends its processes, not just the runner
- A finished job whose process group still has processes is shown as a leak

## 1.0.3 - 2026-09-19

### Fixed
- The flip tests no longer leave a chat server on :8100
- Install docs and README quick start, from a clean Ubuntu 24.04 re-test

## 1.0.2 - 2026-09-19

### Fixed
- The console fits on a phone: no view, inspector or modal is cut off

## 1.0.1 - 2026-09-19

### Fixed
- The tokens page reads every transcript in the window, not only today's
- Each message counts once; totals were roughly doubled

## 1.0.0 - 2026-09-19

### Changed
- **Breaking:** `cousin-meeting teach` is removed; the template sync replaces it
- Every start and flip syncs the framework part of `CLAUDE.md` from the
  template. `## Identity` and `## Voice` stay the cousin's own

### Added
- `cousin-spawn <slug> --sync-template`, with `--apply`

## 0.12.1 - 2026-09-19

### Fixed
- The console pane no longer flickers while a cousin works
- Telegram first-time setup with no operator configured

## 0.12.0 - 2026-09-19

### Added
- A Telegram panel per cousin in the console, and the bridge starts and stops
  with its cousin

## 0.11.0 - 2026-09-19

### Added
- `cousin-reply --image` and `--video` attach to the reply (#24)

### Fixed
- Telegram attachments over the upload limit, and rejected sends log the reason

## 0.10.0 - 2026-09-19

### Changed
- Every cousin's memory index is kept fresh by the loops daemon

## 0.9.0 - 2026-09-19

### Added
- Telegram carries images, video and voice both ways (#13)

## 0.8.2 - 2026-09-19

### Fixed
- A failed Telegram relay no longer loses the message; the bridge saves its
  position

## 0.8.1 - 2026-09-19

### Fixed
- The boot packet gives the same pre-exit order as the rest of the docs

## 0.8.0 - 2026-09-19

### Added
- Delete a meeting, turn numbers in every turn line, and an opening notice

## 0.7.0 - 2026-09-19

### Changed
- `[memory] scope` is `private` or `shared`; `both` is read as `shared`

## 0.6.0 - 2026-09-19

### Added
- Meetings: a chat shared by the user and several cousins, in rounds.
  `cousin-meeting`, a console page, and docs

## 0.5.0 - 2026-09-19

### Added
- Boot packet layer 2, shared rules and fleet memory

## 0.4.0 - 2026-09-19

### Changed
- Sidebar groups are saved per console user on the server

### Fixed
- The daily flip skips a stopped cousin instead of starting it

## 0.3.0 - 2026-09-18

### Added
- Memory fills in its own truth levels: framework changes at L1, finished jobs
  at L2, hedged sentences at L4
- `cousin-memory obsolete TOPIC --why ...` retires a topic (L5)
- The documentation is rewritten from scratch

## 0.2.0 - 2026-09-18

### Added
- Remote cousins on other machines, with the console as the hive queen
- Truth levels on memory writes, and a layered memory explorer
- A read-only file browser per cousin
- Markdown and Mermaid in chat; images, video and audio
- `config/external-peers.toml` for cousins on another install

### Fixed
- Chat is not typed into a pane showing a menu or a prompt
- The console pane cursor and mouse behave

## 0.1.0 - 2026-09-18

The first numbered release: cousins with their own memory, chat and
lifecycle; the loops daemon; per-cousin auth; the web console; optional
media, Telegram and remote cousins; systemd units and backups.
