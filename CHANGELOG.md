# Changelog

The version lives in `pyproject.toml`. `cousin-version` prints it and
`cousin-version bump [major|minor|patch]` changes it. Newest first.

## Unreleased

### Fixed
- **A runner waiting for a login notices the fix within about a second.**
  It looked at the account only on the doubling backoff (1, 2, 4 ... 300
  seconds), so a login finished between two looks waited up to minutes and
  the cousin stayed `errored`. Between two looks the `sdk` and `opencode`
  runners now read the cheap signals about once a second: the credential
  file's mark (a stat and a small hash) and whether
  `data/login-required.json` is still there. A changed credential or a
  deleted file ends the wait at once; `claude auth status`, which starts
  the CLI, stays on the backoff, and a retry that fails for another reason
  is not repeated every second on the same credential.
- **A good `cousin-account login <name> --via <slug>` wakes that cousin.**
  The `--via` cousin's `data/login-required.json` is deleted when it names
  the account just logged in (also `token ... --via` and an opencode OAuth
  `--method ... --via`), so its runner retries at once, as on a manual
  retry. Only after a good login, only the `--via` cousin's file, and
  never a file naming another account.
- **A missing opencode binary says what to do.** An opencode cousin on the
  default image (which has no opencode) failed every start with only
  "opencode binary not found or not executable: opencode". Inside the
  framework's image the error now says the image has no opencode and names
  the fix on the Docker host: `cp compose.opencode.yml compose.override.yml
  && docker compose up -d --build`. Outside it, the error says to install
  opencode on `PATH` or name the binary in `COUSIN_OPENCODE_BIN` or
  `[agent] opencode_bin`. `cousin-spawn --runner opencode` and the
  console's spawn dialog now refuse a new opencode cousin with the same
  message when the binary is not found, before anything is written.
  docs/install.md makes switching to the opencode image a separate first
  step.
- **The suite passes without an agent CLI on the host.** The account
  status, check and login-flow tests hand `status()` their own `run` and
  the flows a fake `spawn`, but still resolved the real `claude` binary
  first, so on CI (no `claude` on `PATH`, no `sdk` extra) they failed with
  "no agent CLI". They now point the CLI lookup at a fake path, as the
  console's account routes already did. No product change.

## 2.3.4 - 2026-10-01

### Fixed
- **A `host` cousin's login line in the container names the way in the
  container.** A cousin on `host` with no login said "run `claude auth
  login` as the host user on <container id>": inside the image there is no
  host user, no `claude` on `PATH`, and the hostname is the container's id.
  The image now sets `COUSIN_IN_CONTAINER=1`, and there the line (the
  runner's login-required file and `auth` event, `cousin-account status`,
  the console's account status) says to run `docker compose exec framework
  cousin-account login host --via <slug>` on the Docker host, with no host
  named. The login lands in the image user's `~/.claude`, on the volume
  (`HOME=/data/home`), so it stays across a restart. Outside the image the
  line is unchanged.
- **The fleet row's `tmuxSession` and `auth` say what a runner cousin
  has.** `GET /api/cousins` gave every cousin `[chat] tmux_session` (default
  the slug) and the legacy lane's `[runtime] auth` mode (default `claude`),
  so an opencode cousin read as a Claude login in a tmux session. Both keys
  stay; on a runner cousin `auth` is now null (no runner reads the mode;
  the credential is the row's `account`) and `tmuxSession` is null, except
  on the `tmux` kind, where it is the runner's own `tmux-<slug>` (the row
  said the slug, a session that does not exist). `tmux-legacy` rows are
  unchanged. The inspector shows "none" for a null session.

### Changed
- **The image build is pinned.** The base image is
  `python:3.13-slim@sha256:...` (its multi-arch index digest, so arm64
  still builds), named by one `ARG PYTHON_IMAGE` that every `FROM` uses.
  The Python packages come from `docker/requirements.txt`: the sdk extra,
  what it pulls and the build backend, at exact versions with the sha256
  of every file, installed with `--require-hashes` before the framework
  goes in place with `--no-deps --no-build-isolation` and `pip check`
  (the build backend leaves the venv with pip). Before, the tag floated
  and pip resolved pyproject.toml's ranges on the day of the build.
  pyproject.toml keeps its ranges for pip users. `sh docker/lock.sh`
  re-resolves the lock in the pinned base image;
  `docs/development.md` says when and how. `docs/install.md` no longer
  calls the build not reproducible: nothing in it floats (no stage
  installs a system package); only the optional embeddings service's
  ollama image is pinned by tag alone.

## 2.3.3 - 2026-10-01

### Fixed
- **A killed pane's CLI is SIGKILLed before the tmux runner gives up on
  it** (#130). The runner waits for an old or refused pane's CLI to be gone
  before it starts another on the same session: SIGKILL after
  `kill_grace_s`, give up at `kill_bound_s`. It checked the bound first, so
  a poll a loaded host returned past both gave up without ever sending the
  SIGKILL, and the reopen failed with "the old CLI is still running"
  against a CLI nobody had killed. The SIGKILL now comes first and is
  polled once more before the runner gives up.
- The between-turns reader (#134, 2.3.2) opened a `background_turn` for a
  subagent's own messages, which the CLI forwards to the parent's stream
  (seen live: a "turn" that never ended, handed over at the next row). A
  background turn is now opened only by the model's own message (no
  `parent_tool_use_id`); task progress and subagent messages open none.
- An opencode account whose `providers` names `opencode` (opencode's own
  hosted service, OpenCode Zen) no longer reads as logged out without a
  key. 1.26.0 said Zen needs its own API key; its free models need none
  (measured on opencode 1.18.31: `opencode run -m opencode/big-pickle`
  answered with no `auth.json` at all). `cousin-account status`,
  `cousin-runner --check-auth`, the console's accounts page and check-auth,
  and `cousin-migrate`'s auth check now count `opencode` as logged in with no
  key, and list it under `providers`, never under `missing`; a key stored
  for it is still used (the paid models). Every other provider still needs
  its key. Most free models let the vendor use the prompts to improve or
  train models (docs/configuration.md names where Zen lists the ones that
  keep nothing).
- Install docs, from a fresh-clone Docker run. `docs/install.md` now opens
  the Docker section with a worked cousin on opencode's free model, no key
  and no Claude account: the opencode image through `compose.override.yml`,
  an `opencode` account with `providers = ["opencode"]`, and
  `cousin-spawn --runner opencode --account <name> --model
  opencode/big-pickle --start`, each command run with
  `docker compose exec -T`. It says to repeat the same `-f` files on every
  compose command (a plain `up` falls back to the default image, which has
  no opencode), that one opencode account serves one cousin, and how to
  pipe `cousin-console adduser` without a terminal. The spawn dialog does
  have kind and account fields (the environment only preselects them); the
  container shows no commit; the build is not reproducible (only the
  opencode binary is pinned). The bare-host section drops the
  `config/agent-cmd` steps 2.0.0 removed, installs the `sdk` extra, enables
  `cousin-supervisor.service` before the first cousin is started (a start
  needs a running supervisor) and moves the LAN drop-in, update and
  uninstall steps to it. The README gains a Docker quick start, says a
  Claude account is optional, and its bare-host quick start runs the
  supervisor instead of writing `config/agent-cmd`. `compose.yml` names the
  opencode lane; `docs/reference/runners.md` no longer says
  `cousin-spawn --runner opencode` skips the model; the entrypoint's
  first-start checklist no longer suggests an editor the image does not
  have.
- **Timing tests that failed on a loaded host and passed alone** (#130):
  the tmux runner, kind switch, job run, SDK backoff and console memory
  explorer tests now wait for the condition they assert under deadlines a
  loaded host does not reach, instead of fixed sleeps, wall-clock bounds or
  counted polls. What each test checks is unchanged. The console test
  harness waits for the server's serving and events-poller threads before
  it removes the temp root.

## 2.3.2 - 2026-10-01

### Fixed

- **The SDK runner reads the CLI between turns** (#134). It read the CLI's
  stream only inside a turn, on the assumption that the CLI is quiet
  between turns. It is not when a background task runs: a subagent or a
  background shell streams its progress after the turn that started it has
  ended, and each completion starts a CLI turn of its own (a task
  notification). Unread, the SDK's message buffer (100 messages) filled,
  its reader blocked, and the control requests queued behind it were never
  answered: about ten minutes after the turn ended, every hook and
  in-process tool call of the background task failed ("PreToolUse hook did
  not respond before its timeout"), and the task notifications were
  dropped ("UserPromptSubmit hook callback timed out"). The runner now
  reads between turns and records what it reads like a turn's own
  messages. A CLI turn of its own is a `system` event
  `{"subtype": "background_turn", "phase": "start"}` and a `result` with
  `background: true` and no inbox ids, followed by the usual usage record,
  extraction and memory proposal; the console's reasoning pane shows it as
  a turn headed "background". A row that arrives while one is open is
  written into it (`background_turn` phase `handed_over`) and closed by the
  result its echo precedes. A stream that ends between turns is a
  `background_end` event; the next turn finds the dead CLI and recovers as
  before.

## 2.3.1 - 2026-09-30

### Changed

- **One model catalogue and one effort list** (#41): `DEFAULT_MODELS` and
  `EFFORT_LEVELS` in `cousin_lib/config.py` are the only copies; every
  command and route already imported them, and a test now fails when a
  module spells either out again, when a doc or shipped example lists an
  effort sequence that is not `EFFORT_LEVELS`, or names a model id outside
  `DEFAULT_MODELS`. `docs/configuration.md` names both as the source.
  `config/harness.toml.example` said the built-in list held three names (it
  holds seven); it now points at `DEFAULT_MODELS`.

### Fixed

- **SDK-lane tests wait for what they read** (#102): four more tests read
  something the runner writes after the event they waited for: a login's
  `auth` event (after its file), the last turn's pressure check (after its
  row closes), the failure count (after the idle move) and a side
  session's `side_restarted` (after the rebuilt side starts). Each waits
  for the thing itself now. Tests only.
- **Console start and stop around the supervisor** (#92):
  - A stop with no supervisor running and a runner started by hand still
    holding the lock answered `502 cousin-supervisor refused the stop: no
    reason given`; nothing had refused. It is `503` saying that no
    supervisor runs, the runner was started outside one and cannot be
    stopped from here, and that the hold is written.
  - A start the supervisor refused as "still stopping" (transient) was a
    `500`; it is a `409` (`spawn.StillStopping`).
  - A start while the cousin is held and a runner the supervisor did not
    start holds its lock answered `started` for a second runner that only
    waited for the lock in the supervisor's backoff. The live supervisor's
    snapshot is read first: with no running child of its own for the
    cousin the start is refused (`spawn.ForeignRunner`, `409` in the
    console, exit 1 from `cousin-spawn --start`) and nothing is started.
  - A refused start emitted `starting` and nothing after it; it now
    emits `start failed` with the error.
  - The loops lock's at-fork handler checked device and inode only; a
    recycled inode under another path (a later temporary root in a test
    run) could have it close a descriptor that was not its own. It checks
    the path too, where `/proc/self/fd` says.
- **Runner review leftovers** (#68):
  - `cousin-runner --once` gave up after 10 s on a runner `errored` inside
    its resync, whose drain may take its `drain_timeout_s` (30 s), and
    exited 3 on a runner about to recover. The drain now comes on top of
    the 10 s; a login or a stalled side session keeps the plain 10 s.
  - `--once` read the inbox unguarded: one busy or broken read ended it
    with a traceback. A read that raises is retried, and one that keeps
    raising for 10 s is exit 3 naming the error.
  - A silent tool call longer than the idle timeout (600 s: a build, a
    10-minute Bash) failed its turn as a stalled stream, and the resync
    interrupted it. While a tool call is open (its `tool_use` seen, its
    `tool_result` not yet) the bound is `tool_idle_timeout_s` (an hour).
  - A live turn's interrupt poll (5 times a second) read the inbox on the
    event loop thread, where a busy inbox (sqlite waits up to 30 s) froze
    the reader and the hooks; the read runs off the loop. That shifted
    when a fold is written, and one test that read the turn's threads
    right after the fold's write, before its echo (where the row joins
    the turn), failed about 4 runs in 6; it waits for the echo now.
  - A row with an image and a file attachment is echoed and closed as
    any other: now pinned by a test.
- **The runner pane marks where markdown stops** (#99): past the first
  20 000 characters a text is not parsed, and the rest (`.rp-md-rest`) had
  no style, so a raw `**` there looked like a rendering bug. It now sits
  under a thin dashed rule with a muted "raw text from here" note.
- **The runner contract names only the tools it serves** (#97): the
  doctrine prose ("How you answer", "Memory", "Tools, not the terminal
  CLIs") named `send`, `memory`, `job`, `schedule` and `meeting` from a
  fixed text, so a cousin whose registry disables one was told to use a
  tool it does not have. Each mention now follows `tool_definitions`: a
  disabled tool's sentences, bullets and section go, and the paragraph
  that held them is re-wrapped. With every tool served the contract is
  byte for byte what it was (the prompt cache keys on it).

## 2.3.0 - 2026-09-30

### Changed
- Console chat: the compact one-row chat header (2.2.1's phone header) now
  also shows whenever the chat column is narrower than 740 px, measured on
  the column with a `ResizeObserver` (the reasoning pane open beside it on a
  desktop), where the wide header wrapped onto two or three rows. A wider
  column keeps the desktop header as it was. The status line under the
  chat's title no longer wraps: it is cut with an ellipsis, the path first.

### Added
- Console chat: the divider between the chat and the reasoning pane snaps
  (#126). It still drags; let go within 40 px of the chat's left edge and the
  chat collapses, the pane covering the whole area, and a thin handle at that
  edge brings it back at its last width; let go within 40 px of the right
  edge and the pane closes. The collapse is kept per browser
  (`fw_chat_collapsed`, beside `fw_pane_w`), and closing the pane brings the
  chat back. A chat-placement plugin strip hides with the collapsed chat.
- While the chat is collapsed, the pane's say box has a "send as chat"
  toggle: the text is posted as a normal stored chat message, through the
  composer's own send, instead of being said to the running turn.

## 2.2.2 - 2026-09-30

### Fixed
- Plugins: a plugin's event stream through the console's `/plugins/<name>/`
  proxy no longer ends after 5 quiet seconds. The proxy asks the service for
  `Connection: close`, so the connect timeout stayed on the socket the relay
  reads; a page reconnected every few seconds and missed events sent in the gaps.

- `cousin-runner`: a status probe of a runner's lock no longer takes it
  (#79). `hold_lock` now also holds an open-file-description lock on
  `run/runner.lock` beside its flock, and `is_running` reads it with
  `F_OFD_GETLK`, which takes nothing, so a runner starting while the
  fleet poll, the loops tick or the console probes it is never refused.
  The flock still keeps a second runner out, one of an older version
  included; a runner started before the upgrade reads as stopped to the
  new probe until it restarts.
- The runner's event stream reader (`EventStream.tail()`) no longer
  raises `UnicodeDecodeError` when the stream file ends inside a
  multi-byte character (a writer that died mid-write): it reads bytes
  and decodes each complete line, and the partial last line is skipped
  as before (#86).
- Chat hooks: a `shell:` handler no longer inherits the chat server's
  credentials (#88). Its environment is the server's minus the auth
  variables (`accounts.AUTH_VARS`) and every credential-shaped name
  (`*SECRET*`, `*_KEY`, `*_TOKEN`, `*_PASSWORD`), the rule the runner
  already applies to the cousin's own tools.
- The sdk runner has a test that a turn the CLI ends with an `is_error`
  result (no exception, no login or rate-limit signal) closes its rows
  `failed` and the next row still runs (#67).
- Semantic memory search: a file's last chunk that would add fewer new
  characters than `chunk_overlap` now joins the chunk before it instead
  of standing alone as a fragment that is mostly overlap (#82); such
  fragments ranked erratically. Nothing is re-embedded at upgrade:
  existing indexes pick it up at their next rebuild or refresh, which
  re-embeds only the affected files' last chunk.

## 2.2.1 - 2026-09-30

### Fixed

- **The chat header on a phone** ([docs/console.md](docs/console.md#chat)):
  below the console's 820 px breakpoint the chat toolbar took three rows and
  the status line under the name wrapped mid-line, so a third of the screen
  went before the first message. The toolbar is now one row of 36 px
  targets: the effort select, a search button that opens the search field
  (with a close x), a "⋯" menu holding archive, archived / live and
  media on / off (and the "@slug as user" line), then the pane and
  fullscreen toggles as icons. The status line is one line: dot, state and
  model (cut with an ellipsis); the slug (the title above) and the heartbeat
  are left out there. The desktop header and the embed are unchanged.

## 2.2.0 - 2026-09-30

### Added

- **A plugin page over the chat** ([docs/plugins.md](docs/plugins.md#what-happens)):
  `[console] placement` in `plugin.toml`, `"pane"` (the default: the 2.1.0 tab
  over the pane, unchanged) or `"chat"`: a strip on top of the chat's messages,
  like a video call, with a bar (title, pop out, collapse) and a bottom edge to
  drag its height (120 px to 70% of the column), both kept per cousin and
  plugin in the browser. The messages and composer stay below it and the chat
  keeps following its latest message. Any other value skips the plugin with
  the reason. `GET /api/plugins` rows and the fleet row's `plugins[].tab` carry
  `placement`. Without a `"chat"` plugin the chat view is as before.

## 2.1.0 - 2026-09-30

### Added

- **Plugins** ([docs/plugins.md](docs/plugins.md)): the framework learns to run
  install-level extensions it does not ship. A plugin is a directory with a
  `plugin.toml` declaring any of an `[mcp]` server, a `[service]` and a
  `[console]` page; the install declares it in the new, optional
  `config/plugins.toml`, and a cousin turns it on with `[plugins] enabled` in
  its `cousin.toml` (or the inspector's new "plugins" list). Parsing is strict
  and a bad plugin is named and skipped, never fatal (`cousin_lib/plugins.py`).
- The runner adds each enabled plugin's MCP server beside the home's
  `.mcp.json` servers on the `sdk` and `opencode` kinds (a `.mcp.json` server
  of the same name wins); the `mcp_config` event marks them
  `"source": "plugin"`. The `tmux` kind does not get them yet (a documented gap).
- `cousin-supervisor` runs each plugin service some cousin enables as a child
  `plugin:<name>` (restart with backoff, `data/plugins/<name>/service.log`,
  picked up or dropped by `reload`); `status` names a plugin that does not load.
- Console: `GET /api/plugins`, `GET`/`POST /api/cousins/<slug>/plugins`, the
  fleet row's `plugins`, and the `/plugins/<name>/...` proxy to a service's
  loopback port behind the console login, streaming (an event stream stays
  open). A cousin with a plugin page gets a tab beside its chat pane. An
  install without plugins shows no plugin UI at all.

## 2.0.0 - 2026-09-30

The legacy tmux lane is retired: every cousin is a runner kind (`sdk`,
`tmux`, `opencode` or `fake`), and no cousin runs a chat server of its own.
The tmux **kind** (Claude Code in a pane, driven by `cousin-runner`) stays.

### Upgrading from 1.x

Do these in order.

1. **Move every cousin to a runner kind first, on 1.x.** `cousin-migrate plan
   <slug>` says `on the tmux lane` for a legacy one; move each with
   `cousin-migrate apply <slug> --validate --yes`, one at a time. 2.0.0 keeps
   no conversion: a cousin with no `[agent] runner` is refused by name
   everywhere. Missed one? docs/migrating.md, "A cousin with no runner", has
   the steps by hand.
2. **Hive nodes:** a node with `HOME_CHAT_URL` set moves to `TELL_HOME=1`, with
   `home_cousin` named in the queen's `config/hive.toml`.
3. **Units:** disable what only the legacy lane used:
   `systemctl --user disable --now cousin-chat-watchdog.timer
   cousin-chat-server@<slug>.service cousin-start@<slug>.service`.
4. **Upgrade:** pull, `pip install -e '.[sdk]'`, restart `cousin-supervisor`,
   `cousin-console` and `cousin-loops`. No store changes schema; runners
   resume their sessions.
5. **Stop the old chat servers** still running from 1.x, and remove the keys
   2.0.0 no longer reads: `cousin-migrate tidy --all` lists them,
   `cousin-migrate tidy --all --yes` removes them (each file's prior bytes are
   kept as `.pre-2.0.0`) and stops a 1.x chat server still running for a cousin.

**Rolling back:** check out the last 1.x tag, `pip install -e '.[sdk]'`,
restart the supervisor, console and loops. Runner cousins resume their
sessions. A cousin.toml `tidy` edited has its prior bytes in
`data/cousin.toml.pre-2.0.0`; the chat-server units can be re-enabled by hand.

### Removed (breaking)

- The legacy tmux lane. A cousin with no `[agent] runner` is refused with one
  line naming the way out, by `cousin-spawn --start` (exit 2), the console's
  start/stop/restart (409), `cousin-runner` (exit 2), delivery (`failed`),
  `cousin-flip`, `cousin-reincarnate`, the supervisor and `cousin-migrate
  plan|apply|rollback` without `--to` (exit 2). A worker says it has no
  session to deliver to.
- The per-cousin chat server: `cousin-chat-server`, `cousin-chat-watchdog`,
  their units, port allocation and `cousin-spawn --port`. The console, the
  inbox and the hive carry chat. A peer install still on 1.x is still reached
  at its chat server's `/api/send`.
- `systemd/cousin-start@.service`: the supervisor starts cousins at boot.
- The hive node's `HOME_CHAT_URL` (ignored, with a line at start): tell-home
  goes through the queen (`TELL_HOME=1`). `cousin-spawn-node --home-chat` is
  now `--tell-home`.
- `cousin-migrate plan|apply|rollback` need `--to` (the kind switch); the
  1.x tmux-lane migration is refused. The console's migrate routes answer
  `409` (no runner) or `400` (a runner cousin) without `to`.
- The auth mode switch (`cousin-auth`, the console's auth route) refuses a
  cousin with no runner (`409`) before anything is stopped; a dismiss of one
  archives it without the refused stop.
- A new cousin with no `--runner` and no `COUSIN_DEFAULT_RUNNER` is `sdk`
  (it was the legacy lane); `tmux-legacy` is not a lane any more.
- The console's rows lose `port` and `host`; the spawn dialog offers runner
  kinds only.

### Added

- Removed keys are named, never silently ignored: `cousin-runner` at start
  (a stderr line and a `system` `config` event), `cousin-supervisor status`
  (`config` and `refused` blocks), the console's card (`removedKeys`) and
  `cousin-migrate plan --to` / `check`. `cousin-migrate tidy <slug>|--all
  [--yes]` removes them. The list is in docs/configuration.md, "Removed in 2.0.0".

### Changed

- Docs describe runner kinds only (cousins.md, configuration.md, chat.md,
  chat-api.md, migrating.md, operations.md, remote-cousins.md, telegram.md,
  console.md, the glossary, the loops pages), plus eight doc and comment
  fixes found by the framework map.

## 1.27.0 - 2026-09-30

### Added
- Reasoning pane: the "N bg tasks" chip opens the list of background tasks
  (subagents, background shells): the running ones with their description,
  kind, elapsed time and last tool, then the last few ended ones with their
  status (#129). The SDK runner now keeps each task event's id, description,
  kind, status, last tool and a short summary on the stream (no prompt, no
  output).

### Fixed
- Reasoning pane: a background task that ends with only a `task_updated`
  status no longer keeps the bg-task count above zero; a runner restart
  marks tasks left running by the old process as lost.

## 1.26.0 - 2026-09-30

### Added
- An opencode account may name `opencode` in its `providers`: the runner
  then leaves opencode's own hosted service (OpenCode Zen, whose free models
  are `opencode/<model>`) enabled, and `cousin-account login <name>
  --provider opencode` stores its key. Every other account keeps it disabled,
  as before. Zen needs its own API key, and most free models let the vendor
  train on the prompts (docs/configuration.md).

## 1.25.3 - 2026-09-30

### Fixed
- A runner restarted after a login was fixed no longer shows "login
  required" forever: a data/login-required.json already on disk at start
  now clears on the first good result (SDK and opencode runners). It used
  to wait for a new login failure that never came.

## 1.25.2 - 2026-09-29

### Changed
- Reasoning pane: a reply card reads "reply", without the arrow.

## 1.25.1 - 2026-09-29

### Fixed
- Reasoning pane: a message folded into a running turn shows its own
  recall count; it used to overwrite the first message's, because the
  runner sends a recall before its message.
- Reasoning pane: a login that needs renewing, a credential mismatch and a
  401 retry now show as red lines, and a "login required" chip sits on the
  strip until the login is restored. They used to be grey JSON.
- Reasoning pane: a resumed session joins the runner-start group like a
  fresh one; a session rollover shows as a divider line and a memory review
  as one line (kept, dropped, pending); a failed tool call with no card (one
  outside a turn) shows as an error line instead of being dropped.

## 1.25.0 - 2026-09-25

### Added
- The reasoning pane is folded by default (#125). A strip under the header
  shows what the runner is doing now (a spinner while it thinks or runs a
  tool, with the elapsed time), the Claude usage limit as a bar with its
  reset day, the session's model and whether it runs on the host login or
  an API key, and the turns, tokens and cost in view. The log shows one row
  per real thing: turns, tool calls paired with their results, replies,
  thinking, text, and one summary line per turn. "raw" shows every event,
  as before.
- The divider between the chat and the reasoning pane drags to resize,
  remembered per browser; a double-click resets it to half.

### Changed
- Scrolled up, the reasoning pane stays where you are when events arrive,
  with a "new events" button back to the bottom.
- A run of thinking ticks is kept as one event, so they no longer push real
  events out of the pane's 500-event window.

## 1.24.6 - 2026-09-25

### Changed
- The console sidebar shows no scrollbar at rest and a thin one while the
  pointer is over it. It still scrolls.

## 1.24.5 - 2026-09-25

### Removed
- The sidebar's NEXT FLIP card. The next flip stays in the overview's fleet
  sentence and in the fleet table's column.

## 1.24.4 - 2026-09-25

### Changed
- The overview's host and activity panels sit under the fleet table at every
  width, side by side, instead of in a right-hand rail on wide screens.

### Fixed
- The overview's fleet sentence counts remote cousins too, so it agrees with
  the running tile and the fleet table ("3 of 11", not "2 of 10").

## 1.24.3 - 2026-09-25

### Fixed
- The console's reasoning pane footer lines up with the chat composer beside
  it: same padding and a 36px input and button row.

## 1.24.2 - 2026-09-25

Three runner fixes, each proven by a test that failed before it.

### Fixed
- A new session's id is on file as soon as the SDK's init names it (#119),
  so a runner killed during a new session's first turn resumes that session
  instead of starting fresh. A lost resume keeps the old id on file until
  its fresh start has run. A failing write is retried once per turn, not per
  message.
- A turn's `result` event reaches the stream before its inbox rows close, on
  every kind (#87): a closed row's result is always visible. The row close
  sits in a try/finally, so a failed append never leaves a row claimed. The
  contract suite checks the order. Known exception: the tmux kind's
  `_pane_lost` and `_settle_on_stop`.
- The SDK runner's carried read (a row written while the CLI finished a
  turn, its echo not yet come) honours stop and interrupt (#66). A stop
  gives the echo up to 2 s, then requeues the rows the CLI never took up.
  An interrupt is taken, and sent again to the turn if the CLI was idle
  when it first went; after `drain_timeout_s` with no echo the client is
  replaced (a reconnect resuming the session) before the rows are
  requeued, so a late echo can never run a row twice. Neither counts as a
  failure: no `errored` state, no backoff.

## 1.24.1 - 2026-09-25

The documentation sweep (master plan 10c, task 6) and the glossary (task 7),
against the running 1.24.0. The 2.0.0 steps of 10c (the CHANGELOG migration
section, the major bump) wait for 10b, the legacy lane's retirement.

### Added
- `docs/glossary.md`: the words the docs use in a sense of their own, one
  entry each. Every page links the first use of each term to its entry;
  `tests/test_glossary_links.py` holds the pages to it.
- `docs/migrating.md`: "Switching between runner kinds" (`cousin-migrate
  --to sdk|tmux`), which was undocumented.
- `docs/mcp.md`: a runner cousin's own `.mcp.json` servers, per kind.
- `docs/reference/lifecycle.md`: the runner lane's clean stop.
- `docs/operations.md`: `cousin-loops fire` for testing a loop by hand.

### Fixed
- Every page read against the code and a scratch install: the tmux kind and
  the legacy tmux lane told apart everywhere; `docs/memory.md` numbers the
  boot packet's nine layers as `boot.assemble` does; `docs/mcp.md` lists the
  `obsolete` and `meeting` tools; `docs/cousins.md`: a runner cousin needs no
  chat server; `docs/chat.md`: `cousin-reply` exits 1 only on a local write
  failure; `docs/reference/console-api.md`: the `ok` and `restart_note`
  fields and the tmux kind in the spawn options; the cousin CLAUDE.md
  template's `cousin-runner`, `cousin-migrate` and `cousin-watch` rows.
- `cousin-migrate --help` names the kind switch and the legacy lane.

## 1.24.0 - 2026-09-25

Phase 11 of the runner plan, integrated onto 1.23.0. The live proofs the
phase's exit criteria require (a kind switch both ways on a real cousin, the
pane under a real login) passed on this release candidate before it shipped.

### Added
- The console finishes its map of the framework:
  - **Agent settings** per cousin, lane-aware (sdk, opencode, tmux,
    tmux-legacy): model, effort, account, auto_start, rollover threshold,
    side sessions, the opencode keys, env_allow, commit_attribution, plus
    name, peer visibility and the memory keys; every `[agent]` write through
    one validated path (`spawn.persist_agent_values`), a new SDK model
    validated by one turn in a child process on the account being written.
    The spawn dialog picks the kind from the server's list.
  - **Kind switch and migration**: plan, apply, check and rollback, and
    `--to sdk|tmux`, as background operations with their stages; the pane of
    a tmux-kind cousin opens from the dialog for the one-time trust answer
    (keys reach it only while it shows a dialog that waits on a person, and
    only the keys such a dialog takes). Reincarnate and transplant, with a
    typed confirmation for the destructive modes and an audit line naming
    the console user.
- The `tmux` runner kind: `[agent] runner = "tmux"` runs the host's
  interactive Claude Code CLI in a tmux pane on the framework's own socket
  (`run/tmux.sock`), driven by `TmuxRunner` inside `cousin-runner` and
  supervised as `runner:<slug>` like every kind. It is fed by the same inbox,
  seen through the same event stream, interrupted from the same console
  button and rolled over by the same row. The CLI's transcript is the source
  of truth: a row is taken when a turn starts with its nonce and closes at
  that turn's end. The session id (`data/runner-session.json`, `claude
  --resume`) is the continuity, so a deploy, a supervisor restart and a kind
  switch keep the conversation. It is the fallback if Agent SDK usage moves
  off subscription limits.
- What the tmux kind cannot do: fold a message into a running turn.
  `TmuxRunner.UNSUPPORTED = ("midturn_fold",)`: the CLI queues typed input
  to the turn's end or interrupts, never folds, so every row (an operator's
  and a peer's included) is claimed at idle and waits for the running turn;
  the operator's urgent path is the interrupt. Every other contract item is
  implemented; the contract table in `docs/reference/runners.md` has the
  `tmux` column.
- It runs on `host` or a `claude-login` account only (`claude-token` and
  `anthropic-key` are refused, exit 2). The pane starts from a fixed
  environment allowlist plus `[agent] env_allow`; `CLAUDE*` and
  `ANTHROPIC*` never reach it. `[agent] model` and `effort` are passed to
  the CLI, and the console edits them on this lane.
- The kind's project settings (`apply_project_settings(kind="tmux")`, at
  spawn, `--repair-settings` and a kind switch): auto-compaction, auto-
  continue at a usage limit and remote control off, `editorMode` normal,
  `policy.toml`'s `deny_tools` as `permissions.deny`, and the bridge hooks;
  `remove_kind_settings` undoes exactly what the kind added. Attribution
  on the pane follows `[agent] commit_attribution` (#112), as on every
  other kind.
- The kind switch: `cousin-migrate plan|apply <slug> --to sdk|tmux` moves a
  runner cousin between the two Claude kinds as a step machine, keeping its
  session; `cousin-migrate rollback <slug> --to <kind> --yes` puts it back
  byte for byte. `--to tmux` needs no step first: the pane may ask for the
  trust (or bypass) dialog once, the runner types nothing into it and says
  so, and verify waits up to 10 minutes for the operator to accept it in the
  pane. A switch waits out a tmux rollover whose new session the CLI has not
  written yet.
- The pane hook, `cousin_lib.runner.tmux_hook`: on `SessionStart` it
  records `run/tmux-session.json` (session id, transcript path, source, the
  CLI's pid), and on every event it sends the runner one datagram on its
  wake socket, which drops a datagram from another uid. It only wakes the
  runner, exits 0 and is bounded (3 s itself, `timeout` 5 in the settings);
  it acts only for the pane's own CLI (`COUSIN_PANE_PID`). A runner that
  hears no hook by its first turn end says `hooks_silent` once.
- Adopt: a restarted runner adopts a live pane only when the hook's record
  names the recorded session and the pane's CLI pid; otherwise a `system`
  `adopt_refused` event says why (`no_record`, `session_mismatch`,
  `pid_mismatch`), the old CLI is killed and the recorded session resumes
  in a new pane. A session the pane changed (a `/clear`) is a
  `session_changed` event, not followed (runners.md, Known gaps). Claims a
  previous runner left are settled from the transcript at start.
- The kind's rollover asks for the handoff, mines the old session and ends
  the CLI with `/exit`; a stop with no runner up reaps the pane
  (`cousin-runner --reap-pane`, run by the supervisor). The console's token
  view reads a tmux-kind cousin's usage from its pane's transcripts.
- The pane's stdio `cousin` server serves `reply` and `handoff` for a
  tmux-kind home, through the runner's own tool code.

### Fixed
- A runner asked to stop claims nothing new (live proofs, finding 3: during
  a held close for a kind switch the SDK runner answered a queued row before
  it stopped). `cousin-runner`'s signal handler calls the runner's
  `begin_stop()` at once (`SdkRunner`, `TmuxRunner`, the side sessions'
  `Sessions`, and a side session rebuilt during the stop), and `stop()` calls
  it first; from then on no turn, fold or digest is claimed, and a claim the
  stop raced goes back to the queue. What is live is still finished or
  settled. A signal during the stop's own teardown does nothing more, and
  the wake poke it sends never raises.
- The kind switch's notice is the first turn after the switch (finding 4:
  rows queued before the switch were answered by a model that still
  believed the old kind). It is queued before the target starts, ranked
  ahead of every row (a flip or an interrupt included). A rollback closes
  it if it is still queued or claimed, and clears the tmux runner's
  `data/login-required.json`, so the restored kind never reads LOGIN
  REQUIRED from a trust dialog nobody accepted. A switch whose verify gave
  up but whose target took the notice later (the trust dialog accepted
  late) reads `switched`, with a `late` note, at the next read of its
  record; `cousin-migrate check` prints it.
- A pane lost while the runner stops is the stop's cut (finding 5: under a
  systemd stop the unit's cgroup kill takes the tmux server first). The cut
  row reads "cut by restart", or "cut by a requested stop" when held (the
  held stop's own settle uses the same words), the restart mark tells the
  next start, and the loss counts toward no give-up.
- A tmux rollover's `data/runner-session.json` names the rollover's
  generation as soon as it moves (finding 6: it kept the old one until the
  new session's first turn).

### Deferred to 1.24.x
- Context-pressure detection for the tmux kind's rollover (1.24.0 keeps the
  minimal rollover), `cousin-migrate --all`, `cousin-migrate adopt` (a legacy
  cousin onto the tmux kind with its session) and the docker `tmux` profile.

## 1.23.0 - 2026-09-25

### Added
- The console maps the rest of the framework (the operator's rule: every
  function has a UI path):
  - **Accounts**: every account with its kind, where it lives, its lanes
    and its cousins, never a secret. Check (no model call), validate (one
    turn, asked first), add, edit and remove entries through a validated
    writer, and log in per kind: a Claude login or token (the sign-in URL
    as a link, the code into a write-only box bound to the console session
    that started the login), an Anthropic key, opencode keys and OAuth per
    provider (Anthropic and Claude refused on opencode). Credential actions
    need a logged-in console user and are recorded in
    `data/accounts/audit.jsonl` (who and which account, never a value).
  - **MCP and policy**, per cousin and install-wide: the tool registry
    (toggles and limits), the home's `.mcp.json` servers (stdio, http, sse;
    a secret-looking value must be a `${VAR}` reference), selftest,
    last connection and approve (only on the lanes whose harness reads
    `.mcp.json`, with a typed confirm), and `policy.toml` (a removed deny
    asks first; `mcp__cousin__handoff` can never be denied; patterns are
    checked the way both runners compile them).
  - **Memory actions**: tensions and "retire this claim", search, remember,
    decide and history (the cite is the console user and the time; only the
    operator writes or retires an operator-level claim), the review queue,
    distill, compact and reindex as background operations, and the
    self-portrait (a person commits it, typing the slug back).
  - **System**: the supervisor's children (start, stop, reload), every
    cousin's one-shot schedules, console users (add, reset, remove; never
    the last one or yourself), backup now into a checked destination
    (owner-only files; a destination other users can write is refused
    unless it is sticky; a misplaced copy is reported in
    `data/system/audit.jsonl`, never deleted), `harness.toml [agent]` defaults with where each
    value comes from, and editors for media, embedding, hive,
    external-peers, outbound-filter, law and allowlist, each checked by its
    own loader before it is written.
- `toml_edit.write_file_keys` edits any TOML file byte for byte: root keys,
  table removal, a validate hook on the text and the parsed document.
- `backup.snapshot` takes an explicit target directory.

### Changed
- `mcp_server.approve_registration` keeps the settings file's mode, leaves
  no temp file behind, and writes through a symlinked settings path.
- `review_gate.settle` reads the held entries once under one lock.
- The self-portrait candidate is written through a fresh temp file and a
  rename, never through a link.

## 1.22.0 - 2026-09-24

### Added
- The console has a new look: restrained dark tokens with the current
  accent and icon, the overview as one fleet table with what needs the
  operator first and an activity rail (`GET /api/jobs?since_hours=`), the
  chat and its pane side by side, and memory grouped by truth level (a raw
  record now carries its entry's cite). Every existing control is kept. The
  pane's remembered state moved from `fw_pane_open` to `fw_pane_side` in
  the browser's storage, so a browser forgets it once.
- Console foundations for editing everything from the UI:
  `cousin_lib/agent_settings.py` (the `[agent]` schema per runner kind,
  validated with the runner's own checks: the opencode bridge guard, the
  account preflight, the lane check); `console/toml_edit.py` (floats, lists,
  dotted subtables, `write_keys` with a validate hook, byte-preserving);
  `console/secrets.py` (write-only secret fields, confined, checked by the
  runner's own reader); `console/longop.py` (one long operation per cousin
  with stages, a `cousin-op` event and `GET /api/cousins/<slug>/op`); and
  registration seams (route modules, jsx views, named slots, `SecretField`,
  `useLongOp`). The fleet row carries `lane`, `account`, `held`,
  `autoStart` and `loginRequired`. The spawn dialog picks the lane and the
  account; `tmux-legacy` is an explicit choice that no
  `COUSIN_DEFAULT_RUNNER` overrides. Dismiss, start, stop, restart and the
  auth switch hold the cousin exclusively (409 while another of them, a flip
  or a long operation runs on it).
- The SDK runner names a turn's long wait on its stream: a `system` `stall`
  event while a write, a fold or a control call runs past 30 s, and again
  with its duration when it ends (#104).
- `[agent] commit_attribution` (tracker #112): install-wide in
  `config/harness.toml`, overridable per cousin in `cousin.toml`. `false`
  turns off the harness's own injected commit/PR attribution (a
  Co-Authored-By trailer, a "Generated with Claude Code" line); unset
  anywhere, `true`, the harness's stock behaviour. The SDK runner composes
  it into `options.settings` (and `validate_account`'s), side sessions get
  the same, and the tmux lane's `apply_project_settings` writes
  `includeCoAuthoredBy: false` and an empty `attribution` object into
  `<home>/.claude/settings.json`, idempotently and without touching an
  operator's own keys there. The resolved value rides the runner's head
  `runner` stream event.

### Fixed
- A resumed runner session is told why its last turn was cut: a runner
  restart or crash says it was not the operator and to continue; a requested
  stop says so; a console restart says to continue. The note is put once and
  never aborts start-up (#98).
- A runner cousin's model and effort change in the console writes `[agent]`
  (not `[runtime]`), validated per lane: the SDK lane runs one smallest
  turn on the cousin's own account in a child process (the console's own
  environment is never touched), opencode checks the model and refuses an
  effort. An unchanged value runs nothing and asks for no restart (#100).
- A runner's PreToolUse/PostToolUse job recorder waits at most 2 s on the
  shared jobs store; past that the tool runs, a late registration is
  withdrawn, and no row is left running (#104).
- `{home_encoded}` is Claude Code's own project-dir encoding: every
  non-alphanumeric character is a dash (#106).
- A migration rollback removes the runner lane's session records (#107).
- The console's Telegram toggle runs its own bridge stop only when no
  supervisor is there at all; a slow supervisor stops nothing (#113).
- `cousin-job` and the `job` tool refuse an empty program, or one with
  leading or trailing whitespace, before any row exists (#115).
- The template sync now corrects a cousin's `mcp-registry.toml` when a
  `description` field still holds text an earlier framework release
  shipped, not just what it lacks entirely (#110). The `job` tool's `run`
  command shipped with new wording for its own tool description and its
  `kind`, `title` and `desc` property descriptions, but the sync only ever
  ADDED missing tables and keys: a cousin spawned before `run` kept the
  old text pointing at `cousin-job start shell` through Bash forever. A
  small table of previously shipped values per field (`_KNOWN_TEXT` in
  `cousin_lib/template_sync.py`) tells a framework value the sync may
  still replace from the cousin's own edit of the same field, which never
  matches and is left alone; the correction is idempotent. The tmux-lane
  CLAUDE.md template now teaches the `job` tool's `run` as the doctrine
  for a tracked shell command, with `cousin-job start shell` through Bash
  named only as the fallback when the tool is missing; `examples/wren`
  regenerated to match.
- A peer's message (another cousin, thread `peer:<slug>`) that arrives while
  a turn runs is folded into that turn, as an operator's or a person's is,
  on the `sdk`, `opencode` and `fake` runners (#118). It used to wait for the
  turn to end, and a turn has no length bound: a coordinator's 13 messages,
  two of them STOP, sat queued behind one 60-minute turn while the peer
  duplicated finished work. Meeting, loop and schedule rows still wait for a
  turn of their own, and a peer already queued when one of those turns
  starts folds into it. The claim order at a turn boundary is unchanged.
  `reply` still never answers a peer thread, and with a peer folded into an
  operator's turn a `reply` that names no thread is refused, never guessed:
  the refusal names the operator's `thread=` and the peer's `send`. The
  contract item `peer_waits` is now `loop_waits`, and `midturn_fold` covers a
  peer message too.
- On the `sdk` runner a mid-turn write (a folded message, an interrupt row)
  no longer stops the turn's reader. It used to be awaited on the path that
  reads the CLI's output: once that output was full and unread, the CLI
  stopped reading its input, the write blocked, the reader waiting on it
  never drained the output, and hook replies queued behind the transport's
  write lock timed out, the 18-39 minute stalls (#104). Each turn now has one
  writer task that takes those writes in order (a fold taken before an
  interrupt is written before it) while the reader keeps reading. A folded
  row still counts as delivered only when the CLI echoes it; a write that
  wrote nothing still requeues its row and fails the turn; a write never
  begun when the turn ends goes back to the queue; an interrupt whose control
  write the turn's end cut off is closed delivered ("written as the turn
  ended"), never left claimed; a result counts as interrupted only when the
  interrupt reached the client; no writer outlives its turn.
- A chat row the runner cannot render into a message (a broken attachment,
  say) is closed `failed` with the detail "could not be rendered: <type>:
  <message>", whether it starts a turn or is folded into one; it used to be
  left `claimed` as a turn's first row. A folded one fails alone: the live
  turn and the rest of its claim go on.

## 1.21.0 - 2026-09-24

### Added
- A runner cousin loads the MCP servers in its home's `.mcp.json` (Claude
  Code's format: stdio, `http` and `sse`) beside its own `cousin` server; it
  used to get `cousin` only. `cousin` is reserved, so the tmux lane's entry
  that spawn writes is skipped, never started twice. `${VAR}` and
  `${VAR:-default}` are passed through for the agent CLI to expand from the
  runner's environment, so a secret is never on the CLI's command line; an
  unset variable with no default skips that server, and so does any
  reference to an account variable. A malformed file or entry is
  skipped with its reason in one `mcp_config` stream event (names and types
  only, never a value); the cousin still starts. The set is ordered by name,
  the user servers' tools stay deferred (only `cousin` is always loaded),
  side sessions get the same set, and `policy.toml` applies to their tools.
  Read once per runner: restart it to pick up a change. `cousin-migrate plan`
  lists the servers the runner will load, by name, in an `mcp` line.
- The contract says, in one fixed sentence, that other MCP servers from the
  home's `.mcp.json` may be present and how their tools are named. It
  applies at each cousin's next rollover; a restart resumes the recorded
  prompt.
- The `job` tool has a `run` command: `title`, `argv` (the command as an
  array, never a shell string), optional `desc` and `log` (relative to the
  home and confined to it). It is `cousin-job start shell --json [--desc D]
  [--home-log L] -- TITLE CMD`, the same launcher, on both lanes, with the
  title after `--` so no title is read as an option: the command runs detached in its own process group from the
  cousin's home, its output goes to the row's log, and the row closes with
  its exit code. It returns the job id and log path at once, and an answer
  from the launcher without a job id is an error naming what came back. An
  empty argv, a non-string element, a program starting with `-`, or a log
  outside the home or in `.secrets` is refused before a row exists. A cousin no longer needs `cousin-job`
  through Bash for a tracked long command, and the runner contract says so.
  An existing cousin's registry gains `run` at its next start or flip; the
  job tool's description and the `kind` text there keep their old wording,
  because the registry sync never changes a value a cousin already has.
- MCP registry: an `array` placeholder is checked (a JSON array of its
  `items` type, not empty unless optional), and a command's options go
  before a literal `--` in its argv.
- `cousin-job start KIND [options] -- TITLE [CMD...]`: with the title after
  `--`, nothing after it is re-read as cousin-job's own options (the job
  tool's `start` and `run` both use this shape, so a model's title or
  command can never set `--log`). The title-first shape keeps its options
  as before. A command whose program starts with `-` is refused, exit 2,
  no row.
- `cousin-job start --home-log REL`: a log path confined to the cousin's
  home with the console's own rule (home_files.resolve_in): absolute, `~`,
  `..` and `.secrets` are refused, exit 2, no row.

### Changed
- `cousin-job start ... --log PATH -- CMD` records the log path as absolute,
  so the console finds a relative one. `--log` itself stays unconfined: it
  is the operator's own flag.

### Fixed
- A long chat message on the tmux lane no longer reaches the cousin as a bare
  paste (#111). Claude Code reads one keyboard read of more than 800
  characters as a paste and wraps it in a pasted-content block its system
  prompt tells the model to trust only where the user's own message asks: in
  one cousin's transcript 41 of 244 inbound messages arrived that way, with
  nothing typed outside the block. A chat line over 600 bytes, or with a
  newline, is now preceded by `(Chat <Name>): <Name>'s message follows in
  full below; answer the message, not this line.`, typed as its own
  keystrokes with a short pause before the body. If the body then fails, or
  a login or trust menu appears during the pause, the header is backspaced
  out and nothing is submitted. A tmux call that hangs before the body starts
  erases the header the same way; one that hangs after it erases nothing and
  logs `stranded input possible` (docs/reference/chat-api.md, known limit). The header comes from the sender's name only; the line itself, its
  `(Chat <Name>): ` prefix and the single Enter are unchanged, and short
  messages are typed exactly as before (docs/reference/chat-api.md).
- A runner cousin's Telegram bridge is now a `cousin-supervisor` child,
  `telegram:<slug>`, beside `runner:<slug>` (#101). It used to start only
  from the console's switch, as a process nothing watched: nothing brought it
  back after a reboot, a supervisor restart or a crash, and nothing stopped it
  with the runner. Now it starts after its runner when `[telegram]` passes the
  bridge's own check (a config that does not is one line with the reason, and
  no child), is restarted with the usual backoff, and is stopped and held with
  its runner. A rescan (`reload`, SIGHUP) adds or removes it as `[telegram]
  enabled` changes and restarts it when its token or operators change.
  `cousin-supervisor status` lists it. For a runner cousin the console's
  Telegram switch, token and operator changes write `cousin.toml` and ask the
  supervisor to rescan; they never start a bridge themselves. A bridge already
  running outside the supervisor is left alone while its config runs (the
  child starts once it is gone) and is stopped by the rescan once the config
  no longer runs. Switching the bridge off and on again before the old one is
  down keeps it. The token and operator routes report `bridge` like the
  switch does. A tmux cousin's bridge is unchanged.
- A cousin moved to the runner by `cousin-migrate apply` is handed its
  conversation from before the move (#103). The working conversation does not
  carry: the runner starts a new session from the state digest, the handoff
  and memory. A new `handover` step, after `close`, records the tmux lane's
  last transcript (and the newest other one in the same directory) in
  `data/previous-transcript.json`, resolved through `config/harness.toml
  transcripts_dir`; a transcript it cannot find is recorded as missing and
  never fails the migration. The runner's first fresh start appends a fixed
  paragraph to its digest naming the path(s), read-only, to be read from the
  end by a subagent, and renames the record to `.consumed`; a rollback removes
  it. `plan` says the conversation does not carry, `apply` prints the
  recorded paths, and `apply` warns, with the file's age, when the handoff it
  leaves was not written during the close or is the emergency one.
- `cousin-migrate plan` no longer prints `warn 2.0.0` lines telling the
  operator to delete the tmux lane's keys (`[chat] port`, `tmux_session`, the
  harness's pane patterns, `config/agent-cmd`, a missing `[agent] runner`).
  The tmux lane stays a supported lane, so those keys are not deprecated and
  following the advice would break a tmux cousin. The removal table
  (`removed_keys`) is withdrawn with them.

## 1.20.0 - 2026-09-24

### Added
- The opencode lane: `[agent] runner = "opencode"` runs a cousin's turns
  through `opencode serve` (`OpencodeRunner`) instead of the Claude Agent
  SDK, on the same inbox, event stream, tools, policy and memory. One
  server per cousin on `127.0.0.1` with a fresh password per start, its
  `HOME` and XDG directories in the account's data dir, an allowlisted
  environment (no `OPENCODE_*` of the shell, no `*_API_KEY`), and a config
  rendered at every start: opencode's own hosted
  provider disabled, the model named (`[agent] model = "<provider>/<model>"`
  is required, no default; `small_model`), permission allow-all, the plugin
  pack, and one MCP server. opencode merges other sources over that config
  (its global config dir and `.opencode` under the account's `HOME`, with
  their plugin and tool dirs): the runner refuses to start while any is
  there, and runs no turn unless the config opencode actually runs with
  (`GET /config`) holds exactly the plugin pack, the one MCP server, the
  account's providers and the named models. An `auth.json` holds only `api`
  entries and other vendors' `oauth` logins. The framework's tools reach opencode as a
  remote MCP server the runner itself serves on loopback behind a per-start
  token, so every call runs in the runner against the live turn; the model
  sees them as `cousin_<tool>` and the contract in the system prompt names
  them so (`contract.render(..., tool_name=)`; the SDK lane's bytes do not
  move). Turns use opencode's v1 routes and its event stream: a message
  mid-turn is folded into the run, an interrupt aborts and requeues what the
  abort dropped, an auth failure (401/403) puts the rows back and waits for
  a login, a restart resumes the session opencode still holds, a rollover
  asks for the handoff and starts a new session with the state digest, and
  context pressure reads the model's limit. `[agent] opencode_bin`,
  `COUSIN_OPENCODE_BIN` and `[agent] opencode_models_fetch = false` (no
  models.dev fetch at start).
- `kind = "opencode"` accounts in `config/accounts.toml`: a `data_dir`
  (default `.secrets/accounts/<name>.opencode`, 0700) and either
  `providers` (keys in opencode's own `auth.json` there) or `endpoint` plus
  `endpoint_model` for a local OpenAI-compatible model (`endpoint_context`
  and `endpoint_output` give it a context limit). The lanes do not mix: an
  opencode cousin on a Claude account, or an SDK cousin on an opencode
  account, is refused (exit 2).
- The plugin pack, `plugins/opencode/cousin-policy.js` (plain JavaScript,
  no dependency): it enforces `policy.toml` in opencode's
  `tool.execute.before`, with opencode's tool names mapped to the SDK
  lane's, and the runner runs no turn until the plugin has acknowledged
  this start's policy file (opencode lists a configured plugin even when it
  failed to load). The runner records from the event stream what the SDK
  lane's hooks record: each tool call with its arguments, a `task` call as a
  subagent job, a checkpoint per turn, and on `session.compacted` the
  pre-compact checkpoint and a rollover. opencode installs nothing at boot:
  the runner marks its plugin library present.
- `cousin-account login <name> --provider <id>` for an opencode account: an
  API key from stdin (hidden on a terminal) or `--key-file`, written
  straight into the account's `auth.json` (0600, merged, tmp + rename),
  never through chat; `--method <label> [--via <slug>]` runs an OAuth
  method through the pty and relays its URL and instruction line one way
  (opencode 1.18.31 takes no code back; another vendor's subscription in a
  client it did not write is the user's terms risk). Refused: the provider `anthropic`
  by key or by OAuth, the provider `opencode`, anything naming the
  Claude-subscription bridge.
- The image's opencode variant: `--target opencode` adds the pinned
  opencode 1.18.31 binary (sha256-checked, x86-64 only; no node, no bun, no
  npm) at `/opt/opencode/bin/opencode`, 223 MB compressed within a 240 MB
  budget; `docker compose -f compose.yml -f compose.opencode.yml up -d`
  runs the framework on it (an override file, not a profile). The default
  image is unchanged.
- Claude runs on the Agent SDK only (ruling P9-1): on the opencode lane an
  account naming the `anthropic` provider, and a model, `small_model` or
  `endpoint_model` whose id contains `claude` or `anthropic`, are refused
  (exit 2). A test by name, a floor rather than a proof (docs/reference/runners.md).
- The bridge guard: the opencode lane never carries Claude subscription
  traffic. A config or environment naming the Claude-subscription bridge
  (its plugin, package, proxy or header names, a base URL on port 3456), an
  Anthropic provider or `ANTHROPIC_BASE_URL` on a loopback address, and an
  Anthropic OAuth login in `auth.json` refuse the start (exit 2). A test
  keeps every shipped file free of the bridge's names except the guard and
  the operator's runbook, "Remove the subscription bridge from a live
  install" in `docs/migrating.md`, which the framework never runs itself.
- The per-runner contract table, `docs/reference/runners.md` (what a runner
  is, the `sdk`, `fake` and `opencode` kinds, how to pick one, and the
  opencode lane's known gaps): one row per contract item, one column per
  kind, each IMPLEMENTED, PLUGIN or DECLARED, rendered by
  `python3 -m cousin_lib.runner.contract_table --write` from each runner
  class's `UNSUPPORTED` and `PLUGIN_ITEMS`; a test fails when the page is
  stale. The contract suite passes 21/21 against `OpencodeRunner` with
  nothing DECLARED. `plugin_items()` is an optional runner method.

### Changed
- `delivery.RUNNER_KINDS` gains `opencode`: `cousin-spawn --runner`,
  `COUSIN_DEFAULT_RUNNER`, the console's spawn and the supervisor take it.
- `cousin-runner` checks the account's lane for an `sdk` cousin too, and
  refuses an `opencode` cousin with `[agent.sessions]` mapping a kind to
  `"own"` (exit 2), as it refuses the fake runner: side sessions are the
  SDK lane's.
- `cousin-account status` on an opencode account reads presence only (no
  process); `cousin-runner --check-auth --validate` is refused for one.
- The Dockerfile's default target is an explicit last stage (`default`),
  the same image as before; the build context now carries `plugins/`.

## 1.19.0 - 2026-09-24

### Added
- The console's runner pane highlights what it shows: the event kind in the
  accent colour, text in white, tool calls and results in grey, diffs in diff
  colours, and JSON and markdown rendered. Untrusted text stays linear: every
  inline markdown pattern is bounded, markdown parsing stops after 20,000
  characters (the rest is shown as is), and JSON above 64 KB is not parsed.
- `[agent] effort` sets a runner cousin's effort (`--effort` on the CLI); an
  unknown level is exit 2 at start.
- `cousin-migrate` carries `[runtime] model`, `effort` and `auth` into
  `[agent]`. `plan` prints the runner's bundled CLI version, and a carried
  model or effort is written only after `--validate` ran one turn with it on
  the cousin's own account: a model the runner's CLI can't run is never
  written. `auth = "api_key"` becomes an `anthropic-key` account made from the
  cousin's key file; rollback removes only what apply made. `check` reports a
  `[runtime]` / `[agent]` mismatch.

### Fixed
- A runner cousin is told, in the fixed part of its contract, to reply, send,
  remember and schedule through its tools, not through the terminal CLIs an
  identity file may still name (#95). It applies at each cousin's next
  rollover; a restart resumes the recorded prompt.
- `validate` (`cousin-runner --check-auth --validate`, `cousin-migrate
  --validate`) fails on an error inside the turn, a non-success result or an
  HTTP error status, not only on a result flagged as an error: the "model not
  supported" 400 used to pass. It never runs on credentials inherited from the
  invoking shell.
- A contract test no longer races the runner: it waits for the turn's result
  event, not only for the row.

### Changed
- Requires `claude-agent-sdk` 0.2.159 (bundled CLI 2.1.281), the first that
  runs `claude-opus-5-5`.

## 1.18.2 - 2026-09-24

### Fixed
- A runner cousin on an `anthropic-key` or `claude-token` account can use
  its tools again. The account no longer sets
  `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`: with it the bundled CLI forced the
  permission mode to `default` (every tool call asked, and a runner has
  nobody to answer) and required the sandbox for every Bash call. Measured
  on CLI 2.1.277 and 2.1.281. The account's key or token now reaches the
  commands the CLI starts, the cousin's own Bash included
  (docs/configuration.md, "No subprocess scrub").
- The live key-lane proof is now `test_a_key_account_runs_its_tools`: a
  Bash call on a key account must succeed with no permission prompt. It
  fails with the scrub set and passes without it.

## 1.18.1 - 2026-09-24

### Fixed
- The inbound gate refuses a sender named like one the framework writes
  itself (`fw-hook`, `runner`, `framework`, `unknown`, `system`,
  `schedule`; `delivery.FRAMEWORK_SENDERS`): a peer or node named
  `fw-hook` was threaded on `system` as a framework hook. `403`.
- `POST /peer/send` answers every refusal before the signature verifies
  with the same `401 {"error": "unauthorized"}`: a known peer's slug no
  longer answers a malformed body with `400` or an unusable entry with
  `503`, so a caller cannot list the configured peers. The no-reach and
  local-slug refusals come after the signature and keep their generic
  `503`; the console's log says why in every case.
- The shipped hive node logs `tell-home dropped` when the queen does not
  take a tell-home or no home is configured; it never retries one, and
  `docs/reference/hive-api.md` now says so.
- `docs/reference/hive-api.md`: a tell-home is shown under the name the
  operator minted the node's token with, and the node's `[tell-home: ...]`
  goes to `POST /hive/tell-home` when `TELL_HOME=1`.
- A runner cousin gets a chat image over 3.7 MB (4.9 MB once encoded,
  under the API's 5 MB cap) as a text block with its full path for the
  model's Read tool, which downsizes it, instead of an inline image the
  API refused (a phone photo failed its turn).
- A runner cousin's own tools (memory, send, reply, job, schedule,
  meeting, handoff) are always loaded: the CLI had deferred them behind
  its tool search, so a cousin had to search before its first call.

## 1.18.0 - 2026-09-24

### Added
- `POST /peer/send` on the console: another install's cousin writes to one
  of these with a message signed by the secret the two installs share
  (`Authorization: HMAC <sender>:<hex>`; `config/external-peers.toml`
  `inbound_token_file`, a 0600 file): the secret never travels. Behind the
  network guard, no console session. `reach` (required) limits where a
  peer may write, and anything outside it answers like a cousin that does
  not exist; `name` is how it is shown. The sending side signs with
  `token_file` and `sender`.
- `POST /hive/tell-home` on the queen: a node's `[tell-home: ...]` with its
  own token, to `config/hive.toml`'s `home_cousin` only, under the name its
  token was minted with (never the name it checks in with). A node built
  with home chat gets `TELL_HOME=1`; `home_chat_url` stays as the legacy,
  unauthenticated path.
- Both go through one gate (`cousin_lib/peer_inbound.py`): a plain sender
  name that is never the cousin's operator or a local cousin; a `msg_id`
  and a finite `sent_at` within five minutes, an id delivered once (a
  repeat is a `409`); control characters stripped; 30 messages a minute
  per sender; 16000 characters at most.
- `cousin-migrate plan` warns (`warn 2.0.0 ...`) about every key a cousin
  or the install still carries that 2.0.0 will reject, from one table
  (`cousin_lib/removed_keys.py`).

### Changed
- A message to a local runner cousin (`cousin-chat send`, the runner's
  `send` tool, the console's peer route) is written into its chat and inbox
  in the sender's own process: a runner cousin needs no chat server to be
  reached. A tmux cousin is still reached through its chat server.
- `cousin-reply` and the media `chat` commands store the reply themselves
  (`chat_api.reply`, what the chat server's reply route runs), with or
  without a chat server.
- The Telegram bridge reads replies from the cousin's chat store, not over
  HTTP, and a runner cousin's bridge needs no `[chat] port`.

### Fixed
- Control characters in a chat message or a sender's name are no longer
  typed into a tmux cousin's pane (the chat server's `/api/send` included).

## 1.17.0 - 2026-09-24

### Added
- `cousin-migrate plan|apply|rollback|check <slug>`: one running cousin from
  the tmux lane to the SDK runner as recorded operator steps (a clean stop,
  the auto-memory import, `[agent] runner = "sdk"`, the supervisor's start
  plus the cousin's chat server, a check that both stay up), the prior
  `cousin.toml` kept in `data/migration.json` byte for byte. `rollback`
  undoes exactly the steps that ran and starts tmux on a fresh boot packet.
  `apply` and `rollback` need `--yes`; nothing else migrates a cousin.
  `check` measures the week on the runner: inbox rows not done after an
  hour, tool calls with no recorded result, failed recorder hooks, the chat
  server. The runbook and the fleet's order are in `docs/migrating.md`.
- Valid time for memory claims, derived from raw: `cousin-memory history
  <topic>` lists each claim's id and when it was valid; `cousin-memory
  obsolete <topic> --entry <id>` (and the console's obsolete route with
  `entry`) retires one claim and keeps the topic.
- `cousin-memory tensions` (and `GET /api/memory/{slug}/tensions`): authored
  topics with two or more live claims of different content, to settle.
- A review gate: when more than `[memory] review_batch` (default 3) entries
  on authored topics were written since it last looked, they are held out
  of the distilled views, the boot packet and the runner's digest until
  kept. On the SDK lane it looks after every turn and at start, and a
  second model (`[memory] review_model`, default the cousin's own) reviews
  in the background (a `review_gate` event). `cousin-memory review` lists
  what is held; its `--keep`/`--drop` are the operator's, refused inside a
  cousin's own process tree.

### Changed
- `cousin-chat-watchdog` judges a runner cousin as running by its runner's
  lock, not a tmux session, so it brings a runner cousin's chat server back
  after a reboot or a crash (the supervisor runs none).
- The monthly raw fold no longer puts an entry-level obsolete mark, a
  retired or held entry, or a review gate record into a month's digest, and
  distill reads a topic with such entries from the whole history.

### Downgrade
- Raw memory written by 1.17.0 reads differently under 1.16.x and earlier: an
  entry-level obsolete mark that is its topic's newest line retires the
  whole topic, and held entries and the gate's records are distilled.
  Nothing in raw changes, and upgrading again restores the views.

## 1.16.0 - 2026-09-24

### Added
- `cousin-supervisor run` keeps an install's daemons up in one process:
  the console, the loops daemon and one `cousin-runner` per runner cousin
  (`[agent] runner = "sdk"` or `"fake"`, unless `[agent] auto_start =
  false` or a stop holds it; tmux cousins are never its). Every child's
  output goes to its stdout prefixed with the child's name (`console | `,
  `runner:wren | `). A child that exits is restarted after 1, 2, 4 ... up
  to 60 seconds, and five exits inside a minute mark it `failing` and leave
  it down with one loud line; a configuration exit (2) is `failing` at
  once, and the console's own restart (exit 75) comes back at once. Exit 5
  is busy, for a runner and for the loops daemon (another runner holds the
  cousin's lock, another loops daemon the install's): the child waits in
  `backoff`, retried after 1, 2, 4 ... 60 seconds with one line per
  attempt, and a busy exit is never counted toward `failing`, so it starts
  as soon as the holder is gone, however long that takes. It
  reaps every child and orphan, so it is a correct PID 1. On SIGTERM it
  stops the runners together (35 seconds each to finish the turn:
  `runner.main.STOP_TIMEOUT_S` plus 5), then the loops daemon, then the
  console, and exits 0; SIGHUP rescans `cousins/` without touching a
  healthy child. One supervisor per install (`run/supervisor.lock`).
- `cousin-supervisor status [--json] | start <slug> | start --name
  console|loops | stop <slug> [--no-wait] | stop --name console|loops |
  reload` talk to the running supervisor over `run/supervisor.sock`; a slug
  only ever names a runner cousin, `--name` the console or the loops
  daemon. `stop` answers once the child is down, `--no-wait` at once.
  `run/supervisor.json` holds the same status for readers that want a
  file, read only while a supervisor holds the lock. The console's fleet
  row gains `supervisor: {state}`, null when no supervisor runs (unknown,
  never stopped).
- A stop of a runner cousin holds it: the supervisor writes
  `<home>/run/held` (the time and who asked) and `start` removes it, and a
  held cousin is not started by a new supervisor, so the stop survives a
  supervisor restart, a `docker compose down` and `up`, an upgrade and a
  reboot until the next `start`, as a stopped tmux cousin stays stopped.
- A runner cousin's start and stop go through the supervisor: the console's
  start, stop and restart routes and `cousin-spawn --start` ask it instead
  of tmux and need no `config/agent-cmd`. The console's stop does not wait:
  it answers 202 `stopping` (200 `stopped` when nothing ran), and its
  restart answers 202 and starts the runner again in the background once it
  is down. With no supervisor running, a start answers 503 (the console,
  a restart's start half included) or exits 1 (`cousin-spawn --start`); a
  stop answers 200 with `"runner": "not running", "supervisor": "not
  running"` and still holds: it writes `<home>/run/held` itself and says
  `"held": true`, so a supervisor started later leaves the cousin down
  until `start` (a restart whose start is refused removes that hold
  again). A stop the supervisor refused is a 502 with its reason in the
  console, never `stopped`.
- `cousin-loops run` holds `run/loops.lock` for its life: a second loops
  daemon on the same root (a second clock) exits 5 (busy) with "another
  loops daemon holds <path>", and the first keeps running. A supervisor's
  loops child that meets such a holder waits for it and becomes the clock
  once it is gone.
- `cousin-spawn --runner sdk|fake [--account <name>]`, and `runner` and
  `account` in the console's `POST /api/cousins`, create a runner cousin;
  `COUSIN_DEFAULT_RUNNER` and `COUSIN_DEFAULT_ACCOUNT` supply them when left
  out (unset: a tmux cousin, as before). The console's spawn dialog sends
  neither, so those two variables decide for it.
- The framework as a Docker image: `Dockerfile` (the source tree installed
  in place at `/opt/framework` with the `sdk` extra, pip removed from the
  image (the venv's and the base image's), uid 10001, all state on the
  `/data` volume, `HOME=/data/home`, a health check on `/api/version`,
  `cousin-supervisor` as the command), `docker/entrypoint.sh` (makes the
  volume a framework root on every start, links `templates/` into the
  image, refreshes `config/*.example`, creates only `config/harness.toml`,
  installs the API key secret privately and declares `[accounts.api-key]`,
  warns while the console has no user) and an allowlist `.dockerignore`,
  so a checkout that is also a live root never leaks its state into the
  build.
- `compose.yml`: the `framework` service with the console on
  `127.0.0.1:8600`, the `framework-data` volume, `COUSIN_DEFAULT_RUNNER=sdk`
  and a 45 second stop grace period; the `embeddings` profile (Ollama).
  `compose.api-key.yml` is the API key lane, turned on with `cp
  compose.api-key.yml compose.override.yml`; `secrets/` and
  `compose.override.yml` are git-ignored.
- `systemd/cousin-supervisor.service` (`TimeoutStopSec=75`): the supervisor
  as one user unit in place of the console and loops units, never beside
  them. `systemd/README.md` gives the migration, which carries the old
  console's `--host` and `--port` into a drop-in, and the way back.
- CI builds the image, runs the runner contract suite inside it and fails
  over 180 MB compressed (`docker/image-size.sh`, `docker save | gzip -6`;
  the image is 162.2 MB).
- `docs/install.md` leads with the Docker install; `docs/operations.md`
  covers the container (what runs, holds, logs, the volume, backup,
  upgrade, extending the image, both auth lanes).

### Changed
- `cousin-runner` exits 5, not 2, when another runner holds the home's
  lock; 2 is configuration only.
- `cousin-backup` snapshots `data/inbox.db` first, then the other
  databases, then the runner's event stream (`data/stream/*.jsonl`, each
  copy cut to its last complete line), then the runner's state files as
  plain copies (`data/runner-session*.json`, `generation.txt`,
  `extract-cursor.json`, `propose-cursor.json`, `proposals.json`). A
  snapshot taken mid-turn restores to a runner that answers the row at
  least once: never lost, possibly answered twice.
- The console's restart route reports `supervised` under `cousin-supervisor`
  too (`COUSIN_SUPERVISED`, set for every child it starts).
- `docs/reference/loops.md` says how the loops daemon treats a runner
  cousin: alive while its runner holds the lock, and every delivery one
  inbox row.

### Fixed
- `python -m cousin_lib.loops run` ran nothing and exited 0: the module had
  no `__main__` block (the `cousin-loops` script calls it directly).
- A runner cousin's chat server no longer refuses to start without a tmux
  binary: its deliveries are inbox rows, so a node's `[tell-home]` and a peer's
  message reach the runner on a host without tmux.
- The console API reference said the restart route exits 0; it exits 75.
- `cousin-runner` retries its lock for about a second before it exits 5
  (#79): `is_running` probes by taking the same lock for microseconds (the
  loops tick, the fleet poll, the console's stream), and a runner starting
  inside a probe was refused. This closes the race for every prober.
- `hold_loops_lock` closes its own fd in every process forked from the
  daemon (an at-fork handler, tolerant of a reused fd number): a worker
  loop's job runs through `jobs._spawn_tracked`, which forks twice with
  no exec, and flock locks are shared across fork, so the job-runner
  child used to inherit the daemon's copy of `run/loops.lock` and hold it
  open past the daemon's own exit - no clock until the job ended.
- A `start` (the console's route, `cousin-spawn <slug> --start`) on a
  runner cousin no longer trusts a stale `delivery.is_alive` read while
  the supervisor is stopping it: a runner can keep its lock for up to
  ~35s after a no-wait stop, and `supervisor.is_held` is true for that
  whole window (the stop writes it at once). A start in that window used
  to read the lock as live and answer "already running" without asking
  the supervisor at all, then the runner went down and stayed held with
  nothing said. It now asks the supervisor whenever the cousin is held,
  and reports its true answer - "still stopping", or a fresh start once
  it is down.
- `spawn.stop_cousin` with no supervisor running answered a runner
  "not running" without checking; a runner started by hand (bypassing
  `cousin-supervisor`) still holds its lock, and the stop now checks
  `delivery.is_alive` and reports "running" truthfully instead. It still
  cannot signal a runner with no supervisor to ask, so it only holds it
  down for the next one, as before.
- `docs/jobs-and-loops.md` did not say that `cousin-loops run --ticks 1`
  beside an already-running daemon exits 5, busy (the same lock a second
  daemon takes); `systemd/README.md` said a second loops daemon left the
  supervisor's loops child `failing` - it is `backoff` (busy, retried
  against the holder forever, never counted toward `failing`).

## 1.15.0 - 2026-09-24

### Added
- Side sessions on the SDK lane: `[agent.sessions]` in cousin.toml maps a
  thread kind to `"primary"` (the default) or `"own"`. A kind mapped `"own"`
  gets one session of its own for all its threads, in the same
  `cousin-runner` process and over the same inbox, with the primary's
  system prompt, tools and working directory (the cached prefix is shared)
  and the same memory. It answers while the primary is busy: a peer is no
  longer kept waiting behind a long task. Its first turn carries a side
  digest (the primary's state, the kinds of threads it is on and since
  when, the cousin's last activity note, then the state digest); it never
  carries a row's words, a sender or a thread key from the primary's live
  turn. The `handoff` tool is refused in a side session; it never proposes
  memories, never runs the `[session]` hooks, and starts over at context
  pressure or when the primary moves to a new generation. A side session
  that fails is restarted inside the process after a backoff; the primary
  and its running turn are never stopped for it. `operator` and `system`
  are always the primary's; an unknown kind or value is exit 2. Its session
  id is kept in `data/runner-session-<kind>.json`, its event stream in
  `data/stream/sdk-<kind>-<id>.jsonl`, headed by a `side_session` event.
  `runner = "fake"` refuses side sessions. A side session is not
  interruptible from the console in this release.
- One memory writer at a time per home: an flock on
  `data/.memory-write.lock`, held by the file-backed memory writes (raw,
  the decisions log and its rotation, the decisions backfill, the recall
  counts, the distilled views with their read of raw, the extraction
  cursors, the activity note, the raw fold's read-archive-remove of a
  month, the trash's check-and-replace), across threads and processes, on
  a lock file opened read-only (another user's lock file never shuts a
  cousin out). Before it, two writers at once could lose a decision from
  the log during its rotation, a recall count, or an extraction cursor, and
  a decision the backfill wrote into an old day file while `compact
  --target raw` folded that day was lost for good.

### Changed
- `data/last-activity.txt` is written through a temporary file, so a reader
  never sees it empty mid-write; a side session's note reads
  `[<kind> session] ...`.
- `data/generation.txt` is bumped through a temporary file and a rename.
- The SDK lane's pre-compact checkpoint shows the tail of the calling
  session's own event stream, not the newest stream file: with side
  sessions that file could be another session's, and a side session's
  checkpoint would have carried the primary's operator rows (and the
  reverse).
- `cousin-runner --once` exits 4 when any session waits for a login, not
  only when the primary does, and 3 when a side session cannot start.

## 1.14.0 - 2026-09-24

### Added
- A runner cousin's reasoning pane in the console: `GET /api/cousins/<slug>/stream`
  (its primary event stream as SSE, live, starting at the newest 200 events;
  frame ids are `<session>:<seq>`, so a reconnect resumes after its last event,
  and one after a runner restart gets a `session` frame and the new stream),
  `POST .../interrupt` (ends the running turn from outside the runner's
  process, past its first answer too) and `POST .../say` (a message to the
  running turn on the operator's thread; a login code is diverted, never
  delivered). The chat page shows it in place of the tmux pane.
- `cousin-watch <slug> [--follow] [--tail N | --after N] [--json]`: the same
  stream in a terminal, from the newest 200 events by default.
- An interrupt is an inbox row (source `interrupt`, priority 0), taken on every
  poll of a live turn: it closes `delivered` when it interrupted, `failed` when
  the CLI refused (the turn goes on) or when no turn was running (then it never
  runs as a turn); a tmux cousin never gets one typed into its pane. Contract
  items `interrupt_row` and `interrupt_row_idle`.
- `cousin-runner` writes a `runner` event first in its stream (kind, pid, the
  items it declares unsupported); the primary stream is the newest file headed
  by one, so a side session's stream never stands in for it. `runner/status.py`
  reads a runner cousin's state from its own stores, and the fleet row carries
  it as `runner`.
- The tokens view's prompt-cache hit rate per cousin and per day
  (`cache_read / (cache_read + cache_creation + input)`, a result with no
  usage left out): `/api/tokens` rows gain `cache`.

### Changed
- The console serves a runner cousin's chat (history, search, send, archive,
  reactions) from its `chat.db`, through `server/chat_api.py`, the library the
  chat server now answers with too: the same bodies on both lanes.
- A runner cousin's fleet row: `status` from the runner's lock, `chat:
  "console"`, `active` a live turn, `pid` the runner's, `lastMsgTs` read from
  `chat.db` read-only; no chat server call and no tmux call for it.
- `policy.toml`'s `ask` is enforced as deny with no approval surface yet; the
  deny reason, the docs and the example no longer promise one in a phase.

### Fixed
- The reasoning stream carries a thinking block's text (bounded at 8000
  characters, `truncated` when cut); since phase 2 it held only its length.

## 1.13.1 - 2026-09-24

### Fixed
- A hit strong in both the keyword and semantic legs could vanish at a
  small `top`. `search()` asked each leg for exactly `top` candidates
  before fusion, so a document ranked just past the cut in BOTH legs
  never reached `_fuse`, although its fused score would have beaten a
  single-leg hit inside the cut (measured on a real replay: fused
  0.0326, rank 3 at top=10, absent at top=5). `search()` now asks each
  leg for `max(20, 4 * top)` candidates and still cuts the fused
  result to `top`.
- `captures_for` (and its sibling `read_capture`) called `.get()` on
  whatever `json.loads` returned; a capture file that held valid JSON
  that was not an object (a list, a number) raised `AttributeError`.
  `captures_for` runs on every operator message (`server/inbound.py`
  `divert_login_code`), so a single malformed capture file broke login
  diversion for every account. Both readers now treat non-object JSON
  like unparsable data: skipped, never raised.

## 1.13.0 - 2026-09-24

### Added
- `cousin-memory import-auto`: folds the agent CLI's own auto-memory,
  its `MEMORY.md` index included, into `memory/imported/auto/` with its
  provenance (`imported_from`, `imported_sha256`, `imported_at` in each
  file's frontmatter). A dry run unless `--apply`; idempotent through a
  manifest; a copy edited since the import is never overwritten, a copy
  removed is never brought back. A manifest that exists but cannot be
  read or parsed refuses instead of guessing: one `ERROR:` line on
  stderr naming the path, nothing written, exit 2; `search` stays
  tolerant of the same manifest (reads it as empty). A copy on disk with
  no row in the manifest is a conflict to merge by hand, unless it is
  exactly what this import would have written, which converges instead
  of blocking on a run that died between copying and saving the
  manifest. `--apply` first replays the cousin's own logged queries that
  reached that memory as a baseline; `--verify` replays them again and
  exits 1 on a lost memory, 2 when nothing was compared, including a
  manifest it cannot read (no comparison is attempted). Both bring the
  search indexes fully current first. An imported copy inherits its
  original's recall weighting, and search indexes it as its original's
  exact text (the provenance lines stripped), so both legs rank it the same.
- On the SDK lane, after a turn in which the cousin reached a decision
  and recorded none, the runner queues one `propose` row asking whether
  to keep it: the lowest priority, never about its own turn, at most
  `PROPOSAL_CAP` a rolling day. A `propose` event records each outcome;
  a step that fails while building the proposal (a broken store, a
  corrupt cursor or cap file) surfaces on that same event's `error`
  field instead of silently producing no proposal, and the turn still
  delivers.

### Changed
- `cousin-memory recall` (and the runner's `memory recall` tool) reads raw
  memory through the search index: a fact written by `remember` is
  recallable, not only a decision. With an embedding service, a hit must
  match the keyword or clear `[recall] min_score`. Without a keyword it
  lists the newest entries the cousin wrote, the framework's own log left
  out. A decision prints as before, a digested one too; times are now
  the raw entry's own (UTC for new decisions, where `decisions.jsonl`
  had local time); the empty result reads `No memories found`. Recall
  no longer counts as a search in the recall weighting.
- The first `recall` or search in a home copies every decision that only
  `data/decisions.jsonl` or its rotated archives hold into raw, once,
  with its original timestamp (marked in `data/.decisions-backfilled`);
  the log is still written but no longer recalled from, and
  `consolidate` counts raw only, so a decision is counted once. A raw
  line removed through the console's trash is never brought back by the
  backfill: a trashed twin counts as already handled.
- The distilled views rank the framework's own log (topics starting
  `episode:`, `job:`, `framework:`) after every authored topic.
- The SDK lane starts the agent CLI with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
- A search finds a harness auto-memory file whose imported copy is current
  as the copy only.
- The `memory` tool's description in `config/mcp-registry.toml.example`
  says what recall now does. It reaches new homes; an existing home reads
  its own `mcp-registry.toml` (copied at spawn, never rewritten by a
  template sync) and keeps the old wording until that file is updated,
  which is also when its cached prompt re-creates once.

### Fixed
- The runner's proactive recall and memory tool read the runner's own
  framework root instead of whatever `FRAMEWORK_ROOT` names, and so
  does `refresh_if_stale` for its keyword index.
- A raw memory in the `[fw-recall]` line is named by its topic, not by the
  date of the file it sits in.
- A failing decisions backfill (a read-only or full `data/`, a bad byte
  in the log, a truncated or corrupt gzip archive under `memory/raw/archive/`)
  no longer silences `search` or `recall`: it prints one stderr line and
  writes no mark, so the next read retries; `consolidate` keeps the
  unguarded, loud failure, since it is a command a person runs. The same
  truncated or corrupt archive no longer crashes a search or recall
  outright either: the raw index and a raw hit's own lookup skip it like
  any other unreadable file.

## 1.12.0 - 2026-09-24

### Added
- The composed system prompt on the SDK lane: the law, a contract generated
  from the tool registry and the version, the authored identity (a missing
  one is a named degraded state, never an improvised persona) and the shared
  operator rules, byte-stable across generations so a rollover and a restart
  keep the prompt cache; passed as the `claude_code` preset with
  `exclude_dynamic_sections` and `snapshot`. The volatile layers (active
  state, task packet, retrieved memories, tool trace, calibration, the shared
  index) ride a state digest under the boot packet's own budgets and
  truncation order (`boot.fit`, shared with the tmux lane) as the first
  message of a generation.
- The framework owns the transcript: `SqliteSessionStore` over
  `data/sessions.db` implements the SDK's session store protocol (its
  conformance suite runs in the tests); a key-lane cousin resumes through
  the store, a login-lane cousin through the CLI's own resume; the first
  init after a resume must name the saved session or the runner starts fresh
  from the digest, loudly. A restart (`data/runner-session.json`) resumes
  the session and costs no generation.
- Usage per result into `data/usage.db` (cost as the per-client difference),
  the console token view reads both lanes; every turn is mined into raw
  memory, deduplicated against the flip miner and capped per rolling day
  (`extract.py`); mirror errors are recorded.
- Rollover as one awaited structured `handoff` call (STATUS open loops,
  threads, memories, the handoff file, in the ritual's order), the session
  bookends, a new session on the same prompt bytes, the digest first; on
  context pressure with hysteresis, at the daily cadence (`max_age`, the
  stagger kept), on `cousin-flip`, reincarnate and transplant (a bequest
  rides the handoff request); coalesced, never interrupting a live turn,
  never losing a row, an unanswered handoff ends in an emergency handoff
  from the real transcript, a failure before the new session ends
  `errored -> idle` with the generation unmoved.
- `rate_limited` is a state: nothing is claimed while a limit holds, a limit
  during the handoff postpones the rollover.
- Accounts: `config/accounts.toml` names `claude-login`, `claude-token` and
  `anthropic-key` accounts; `[agent] account` picks one; the environment per
  kind (`CLAUDE_CONFIG_DIR` under the account, the secret from a 0600 file
  under `.secrets/accounts/`, `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` for the
  secret kinds, twelve auth and provider variables scrubbed from the shell);
  `cousin-account list|status|login|token`: the operator logs in through the
  framework from a host shell (the CLI's own flow in a pty, the URL relayed
  through chat, the code captured once under `<root>/run/`, never stored in
  chat.db, a late or second code diverted); a cousin never obtains a
  credential. A dying login is detected in the turn (the typed
  authentication error, a 401 retry, the 401 result, a refused connect),
  written to `data/login-required.json`, shown by `cousin-chat list` and the
  Telegram bridge, waited out without a turn and without failing a row, and
  the runner reconnects when the credential changes;
  `cousin-runner --check-auth [--validate]` (exit 4).

### Changed
- `boot.py`'s budget engine is `boot.fit`, its shared tier `boot.shared_parts`;
  `trace` readers take an explicit root. `Inbox` gains `claim_id`, `open_rows`,
  `replace_body`, `done_if_queued`. `flip.flip` takes `reason` and
  `queue_if_stopped`; the loops daemon names its reasons. `api_key_file` is
  deprecated for `[agent] account`. The `handoff` tool's schema is the
  structured one (position, next_action, status, active_threads, learned).
- The event stream gains `usage`, `extract`, `rollover`, `rate_limit`,
  `auth` and the `system` subtypes `resumed`, `fresh`, `resume_failed`,
  `connect_failed`.

### Fixed
- Two timing-based runner tests (the mid-turn fold, the fake runner's state
  stream) now wait for the event they assert instead of sleeping.
- The usage tests evaluate today's date per call, so a suite that crosses
  UTC midnight no longer compares against yesterday.

## 1.11.0 - 2026-09-23

### Added
- In-process tools on the SDK lane: the runner serves every registry tool
  (`memory`, `send`, `job`, `schedule`, `meeting`, and any other the cousin's
  `mcp-registry.toml` enables) as an in-process MCP server built from the one
  registry, so a tool exists on both transports or on neither, plus two tools
  that exist only there: `reply`, the only writer of the chat surface, which
  routes by the live turn (one thread implicit, two must be named, a named
  operator or person thread always reachable), and `handoff`. A registry
  command with no in-process handler stops the runner at start with the list.
  No process is spawned per tool call.
- In-process hooks: recording (job rows, activity lines) through the new
  `cousin_lib/recording.py`, memory recall off the loop with a 4 s budget and
  the chat server's own gates (one implementation, `memory_search.recall_context`),
  Stop and PreCompact checkpoints, `waiting_permission` on permission requests,
  and `policy.toml` (deny_tools, deny_bash_patterns on any tool's command, ask,
  outbound_filter) enforced as a PreToolUse hook that fails closed; a subagent
  must name the thread it replies to. Matched PreToolUse callbacks run
  concurrently in the CLI; the recorder consults the policy itself.
- Prompt-cache guards: every `result` event carries the SDK's `usage`;
  `session_init` carries the tools and MCP servers the CLI offered; tool
  definitions are byte-stable across connects (tested across interpreters);
  an opt-in live check requires turn 2 to read back what turn 1 cached.
- Producers on the runner: `delivery.accepted()` (a durable put is
  acceptance) and `delivery.is_alive()` (liveness from `run/runner.lock`);
  schedules, loops and meetings hand a runner cousin its items without
  waiting; a Telegram message for a runner cousin is stored and delivered by
  the bridge; chat hooks are one library call every send path makes.
  `cousin-runner` exports `COUSIN_HOME` and `FRAMEWORK_ROOT`, refuses an
  unservable registry with rc 2, and holds one lock per cousin.

### Changed
- `memory` (decide, remember, recall, activity) and `schedule` (add, list,
  cancel) are library functions the CLI calls, byte-identical output.
- `cousin-schedule tick` keeps a job pending when its deliverer says the
  delivery was not accepted.
- `[agent] runner = "sdk"` is no longer experimental for chat, schedules,
  loops and meetings; the composed prompt and the console views arrive in
  phases 4 and 5.
- The master plan takes the accepted review's order and, after 2.0.0, a
  wishlist; phases 4, 5, 7 and 10 gain the breakdown ledger, the cache hit
  rate in the tokens view, valid-time claims with a tensions view, a review
  gate for bulk memory writes, and a glossary.

## 1.10.0 - 2026-09-23

### Added
- The runner: a cousin can run with no terminal. `cousin-runner --home <home>`
  claims items from a durable thread-keyed inbox (`data/inbox.db`) in source
  priority order, drives one long-lived Claude Agent SDK client, appends
  every SDK message and state change to `data/stream/<session>.jsonl`, and
  marks each item done by the turn that consumed it. A message that arrives
  mid-turn from the operator is folded into the running turn and closed by
  its one result, and only once the CLI has echoed it (`replay-user-messages`):
  a row the CLI did not echo before the result belongs to the next turn and
  is closed by that turn's result. `FakeRunner` is the reference
  implementation; one contract suite (18 items: receipts, priority, folding,
  interrupt, outcomes, failure recovery, stop, the event stream) tests both.
  `cousin.toml [agent] runner = "sdk"` switches a cousin over and routes
  `deliver()` to the inbox; every tmux cousin is untouched and the key is
  experimental until phase 3 moves the producers. The auth lane is the
  presence of the key in the session environment and nothing else (the
  login lane scrubs `ANTHROPIC_*` from the runner's environment), and
  `apiKeySource` from every session init is recorded so a cousin on the
  wrong lane is visible. One runner per cousin, held by `run/runner.lock`.
  A failed turn is drained to its result so the next turn starts in sync,
  reconnecting with `resume` as the fallback; a runner that cannot connect
  exits 3 for a supervisor to restart. Optional extra `sdk`.
- Live proofs, opt in with `COUSIN_LIVE_SDK=1`: two related messages through
  `deliver()` answered in one session, and a message sent during the final
  answer neither lost nor misattributed.

### Changed
- `Inbox` grew `requeue`, `unfinished` and `get`; the master plan's locked
  interfaces say so, and it takes the operator-accepted review: the twelve
  unplaced parity rows placed, `max_age` as the flip cadence, phase 7 tasks
  1-2 ticked, and the order 2, 3, 4, 7(3-6), 5, 6, 7(7-8), 8, 9, 10.
## 1.9.1 - 2026-09-23

### Changed
- `claude-opus-5-5` heads the built-in model catalogue (`DEFAULT_MODELS`),
  probed live on five running cousins the day it was released. The spawn
  dialog's default follows the catalogue head when `default_model` is unset.

## 1.9.0 - 2026-09-22

### Fixed
- A bounded foreground embedding pass is no longer silent. `ensure_index`
  reported `incomplete`, but `search()` branched on `busy` and `failed` only,
  so the flag was produced and never displayed: a semantic leg that had ranked
  against 24 of 416 chunks returned `notice=None` and its hits came back
  looking like a complete result over the whole corpus. Measured on a cold
  home 2026-09-22, 5.8% of the corpus, by the peer that hit it. Hits from 6%
  of a corpus presented as complete are how a confident wrong file gets cited

### Added
- `ensure_index`'s report carries `ranked` (current chunks whose stored vector
  still matches their text) and `total` (current chunks in the corpus), so the
  notice can say how much of the corpus the semantic leg actually saw instead
  of only that it saw some. A carried-over or failed chunk keeps its old
  vector and is NOT counted: it is ranked against text that no longer exists,
  which is a worse failure than being unranked, not a better one. Both are
  None on the busy path, where the pass did no work and the coverage is
  unknown

## 1.8.0 - 2026-09-21

### Fixed
- A search no longer pays for a whole backfill. `search()` asks for a bounded
  embedding pass (24 chunks) and the loops daemon finishes the rest;
  `ensure_index(wait=False)` meant "do not queue behind another pass", not "do
  not do the work", so with the lock free a single query ran every embedding
  itself. Harmless while a cousin had a few hundred chunks; indexing the raw
  store multiplied that by about ten and a cousin's first query after the
  change sat over three minutes with the embedding service pinned

### Changed
- The vector index is SQLite (`memory/vectors.db`, one row per chunk, the
  vector a float32 blob) instead of one JSON object read and parsed in full on
  every search. Measured on a real cousin: loading the index went from 1283 ms
  to 69 ms and the file from 16.4 MB to 4.3 MB, against the 938 ms embedding
  call the index exists to serve. An existing `embeddings.json` is imported
  once on first read and removed
- A vector store too damaged to open is replaced rather than fatal: the index
  is a cache of what the sources say

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
## 1.6.3 - 2026-09-21

### Changed
- Every producer that reaches a cousin (chat, reactions, chat hooks, loops,
  schedules, meetings, the flip, a pending boot) goes through
  `cousin_lib.delivery.deliver()` with a typed, thread-keyed item. The text
  reaching the pane is byte-identical; this is the seam the agent loop runner
  plugs into (`docs/design/agent-loop-runner.md`)
- The memory recall line travels to the cousin as the item's context instead
  of being glued onto the message text. What the cousin reads is unchanged

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
