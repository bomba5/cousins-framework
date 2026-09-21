# Changelog

The version lives in `pyproject.toml`. `cousin-version` prints it and
`cousin-version bump [major|minor|patch]` changes it. Newest first.

## 1.7.0 - 2026-09-21

### Added
- Memory search indexes the raw store (`memory/raw/*.jsonl` and its monthly
  archives), one unit per entry, as the `raw` collection. It holds what
  `cousin-memory decide` and `remember`, the flip's transcript miner, the jobs
  ledger and framework events write, and nothing indexed it: `*.md` only. An
  entry reached recall solely through `distill`, which keeps one truncated
  line per topic and caps each file at 40 lines. Measured on a real cousin:
  904 entries over 789 topics survived as 139 lines, so 82% of topics could
  not be found

### Changed
- One result slot is reserved for a curated topic file (`memory/**/*.md`, or
  the harness auto-memory) when the ranking would drop every one of them.
  Indexing the raw store took curated files from 13 of 45 top-three slots to
  2 on a real corpus, because BM25's length normalisation puts a short entry
  above a long file that names the term once. The reserved slot is the last,
  so the best match is never displaced; an explicit `--collection` is never
  overridden

### Fixed
- The same entry is indexed once however many files hold it. `raw_fold` keeps
  a month in both `<YYYY-MM>-digest.jsonl` and `archive/<YYYY-MM>.jsonl.gz`,
  and the twins carry the same topic and content under different metadata, so
  one memory returned as two hits (344 duplicates on the benchmark corpus)

## 1.6.2 - 2026-09-21

### Fixed
- The boot packet's MCP line claimed a scope it did not always have. A cousin
  with no persisted `runtime.session_id` (hand-made, or on its first flip) fell
  back to the newest log file, which is the pre-1.6.0 reading and may belong to
  an older generation. The line now says it could not be scoped instead of
  claiming the generation that just died (#54)

### Documentation
- The 1.5.1 WAL entry stated a property the build only permits; it now names the
  conditions under which WAL does not apply

## 1.6.1 - 2026-09-21

### Fixed
- The boot packet's MCP warning silently depended on the flip assembling the
  packet before persisting the new session id. Nothing stated or tested that
  order, and inverting it would have made the warning scope to a session with
  no log and go quiet forever. Named at the persist site and guarded by
  `tests.test_flip.TestAssembleSeesTheDyingSessionId`, which was proven to go
  red under the inversion (#54)

## 1.6.0 - 2026-09-21

MINOR rather than PATCH for one reason: `cousin-mcp --last-connection` gains
a third state on its public interface. Everything else here is a fix.

### Changed
- `cousin-mcp --last-connection` reports three states instead of two:
  `connected`, `FAILED`, and `no outcome recorded`. Exit 2 now also covers an
  attempt whose outcome was never written down, which previously exited 1 as
  a failure
- `mcp_logs.last_connection()` returns `state` (`connected` | `failed` |
  `unrecorded`) in place of `ok`, plus `reason` and `earlier`

### Fixed
- The boot packet's MCP line no longer predicts the session that is booting.
  It names the session it is about, reports that session's attempt time, and
  is scoped to the generation that just died whenever a `runtime.session_id`
  is on file; see 1.6.2 for what it says when none is (#54)
- An attempt whose outcome nobody recorded reads as unrecorded, not as a
  failure, and a failure in wording the parser has no literal for keeps its
  reason instead of printing "no reason recorded". Measured over 5908 harness
  logs: 93 files were reported FAILED with no reason; 80 held the reason and
  13 held no outcome at all, one of them since April (#54)
- The warning no longer claims "Nothing retries it": a harness session
  reconnects inside itself, which 186 of those logs record (#54)

## 1.5.1 - 2026-09-20

### Fixed
- WAL mode on `jobs.db`, `scheduled.db`, `hive.db` and the memory indexes,
  best-effort: `sqlite_util.wal` swallows a `sqlite3.Error` and SQLite converts
  the journal on the first WRITE, so a database on a network filesystem, or one
  nobody has written since, stays in the rollback journal (#38)
  <!-- wording corrected in 1.6.2: the original entry stated the property
       without the condition the build merely permits -->

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
