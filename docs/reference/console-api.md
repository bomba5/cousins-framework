# Console API

Every HTTP route the web console serves, for when you want to script against it or debug what the browser is doing. For what the pages do and how to use them, read [the console guide](../console.md).

The console is one Python process (`cousin-console`, default `127.0.0.1:8600`). The browser page is static files plus these routes. The console keeps almost nothing of its own: it reads cousin homes, the jobs database, the loops state and the tracker on every call, and it proxies chat to each cousin's own chat server.

## Getting in

Two checks run on every request, in this order.

**The network guard.** The client address has to be in the allowlist: loopback, the private ranges `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, plus whatever you add in `config/net-allowlist.json`:

```json
{"allow": ["100.64.0.0/10"]}
```

You can add ranges, you can't remove loopback. A typo in a range is skipped, not fatal. Anything else gets `403 {"error": "address not allowed"}`. The guard covers static files, `/api/*` and the event streams alike. It does not cover `/hive/*` (see below).

**The login.** Users live in `config/console-users.json` (PBKDF2-HMAC-SHA256, 200000 iterations, 16-byte salt, written 0600). You create or reset one with:

```sh
cousin-console adduser ana
```

The password comes from a prompt, at least 8 characters. What the console does depends on the state of that file:

| users file | what happens |
|---|---|
| absent | Open to anyone the guard lets through. `GET /api/auth/me` says `configured: false` and the startup line tells you to run `adduser`. |
| present, at least one user | Every `/api/*` route needs a session, except `POST /api/auth/login`, `GET /api/auth/me` and `GET /api/version`. No bypass for loopback. Static files need no session (the login form is part of the page). |
| present but broken (unreadable, not JSON, empty, an entry that isn't an object) | Closed. Every `/api/*` route answers `503` with the file name and the fix, login included and live sessions too. `me` still answers so the page can say why. `adduser` refuses to write over it. Restore it from a backup, or delete it and run `adduser` again. |

The session is a cookie called `console_session`: 32 random bytes, `Max-Age=2592000; HttpOnly; SameSite=Strict; Path=/`, plus `Secure` when you start the console with `--secure-cookie` (do that behind TLS). The cookie is persistent, not a session cookie, because an iOS home-screen web app drops session cookies whenever the system closes it. Sessions are saved in `data/console-sessions.json` (mode 0600, a SHA-256 of each token, never the token), so a console restart keeps everyone logged in. An idle session expires after 30 days. Changing a password keeps the session that changed it and ends every other session of that user.

Without a session on a protected route you get `401 {"ok": false, "error": "login required"}`.

There are no per-user permissions. Anyone who can log in can do everything.

## Conventions

- JSON in, JSON out. Responses carry `Content-Type: application/json` and `Cache-Control: no-store`. A body that isn't a JSON object is `400 {"ok": false, "error": "malformed JSON"}`.
- Commands answer `{"ok": true, ...}`. Failures answer `{"ok": false, "error": "..."}`. Read routes return their data keys, usually without `ok`.
- Status codes: `200` done, `201` created, `202` accepted and still running in the background, `400` bad input, `401` no session, `403` refused, `404` unknown thing (or an unknown path), `405` known path with the wrong method, `409` state conflict, `500` local failure with the reason in `error`, `502` a cousin's chat server is unreachable or answered something that isn't JSON, `503` broken users file.
- A `<slug>` has to match `^[a-z][a-z0-9_-]{1,31}$`, otherwise `400 {"error": "bad slug"}` before anything is looked up. A loop `<name>` matches `^[a-z][a-z0-9_-]{0,31}$`.
- A trailing slash on a path is accepted.
- Anything not under `/api/` or `/hive/` is a static file (GET only).

## Auth

### `POST /api/auth/login`

Body `{"user": "ana", "password": "..."}`. Answers `200 {"ok": true, "user": "ana"}` with the `Set-Cookie`. Errors: `400` a field missing, `401 bad credentials`, `409 auth not configured` (no users file). An unknown user costs the same PBKDF2 work as a known one, so the timing doesn't tell you who exists.

### `POST /api/auth/logout`

Drops the session and clears the cookie. `200 {"ok": true}`.

### `GET /api/auth/me`

`{"user": "ana" | null, "configured": bool, "users": [...]}`. `users` lists every configured name, but only when you're logged in. With a broken users file: `{"user": null, "configured": true, "users": [], "error": "..."}`.

### `POST /api/auth/change-password`

Body `{"old_password": "...", "new_password": "..."}`. Needs a session. `200 {"ok": true, "user": "ana"}`. `400` new password under 8 characters, `403 current password incorrect`. Your other sessions stay logged in.

## Telegram

Per-cousin provisioning of the Telegram bridge ([telegram](../telegram.md)). The bot token is write-only: it is stored at `config/telegram/<slug>.token` (mode 0600) and no answer ever contains it. The bridge process belongs to its cousin: it starts with the cousin when `[telegram] enabled` is true and the config is complete, and stops with it.

A status: `{"slug", "enabled", "token_set", "operators": [{"user_id", "name"}], "pending": [{"user_id", "username", "first_name", "at"}], "running", "ready": null | "<why the bridge cannot run>"}`. `pending` lists the last five people who wrote to the bot and were refused, so they can be added without looking up a numeric id. Anyone who messages the bot can appear there.

Discovery: while no bridge runs, which is always the case before the first operator is added, the GET (when there are no operators), the token save and the check call `getUpdates` once. The call has no offset, so it confirms nothing, and every refused sender it finds is added to `pending` (`telegram_admin.discover`). This GET therefore calls Telegram and writes `data/telegram-pending.json`. It never runs beside a live bridge.

### `GET /api/cousins/<slug>/telegram`

The status.

### `POST /api/cousins/<slug>/telegram/token`

Body `{"token"}`. Stores it and checks it with Telegram (`getMe`); answers the status plus `check: {"ok", "bot"?, "error"?}`. `400` for something that is not a bot token. A running bridge restarts on the new token.

### `POST /api/cousins/<slug>/telegram/operators`

Body `{"operators": [{"user_id", "name"}]}`, the whole list. `400` for a non-numeric or repeated id or a bad name.

### `POST /api/cousins/<slug>/telegram/enabled`

Body `{"enabled": true|false}`. Starts the bridge when the cousin runs (`bridge: "started"`, or `"starts with the cousin"` when it is stopped), stops it when false.

### `POST /api/cousins/<slug>/telegram/check`

`getMe` with the stored token: `{"ok", "bot"?, "error"?}`.

## Meetings

A chat shared by the signed-in user and several running cousins, in rounds ([meetings](../meetings.md)). The store is `data/meetings.db`; cousins speak through `cousin-meeting`, so changes also arrive from outside the console and the event stream reports them as `meeting-change` (`{"id", "op"}`). A refusal (not your floor, a stopped or remote participant, a closed meeting) is `400` with the reason in `error`; an unknown id is `404`. The user's entries carry the signed-in user name.

A meeting: `{"id", "topic", "participants": [slug...], "facilitator", "state": "open"|"closing"|"closed", "mode": "floor"|"round"|"direct"|"minutes", "turn_slug", "turn_index", "round", "turn_started", "turn_delivered", "turn_timeout_s", "created_by", "created_at", "closed_at", "updated_at"}`. `turn_slug` empty means the floor is the user's.

### `GET /api/meetings`

`{"meetings": [...]}`, open and closing first, newest first, each with `entries` (the transcript length). `?state=` filters.

### `POST /api/meetings`

Body `{"topic", "participants": [slug...], "facilitator"?, "timeout_s"?}` (timeout at least 60, default 600). Every participant must be a running local cousin. Answers `{"ok": true, "meeting": {...}}`.

### `GET /api/meetings/<id>`

`{"meeting": {..., "transcript": [{"id", "speaker", "kind": "user"|"cousin"|"pass"|"system"|"minutes", "text", "round", "created_at"}]}}`.

### `POST /api/meetings/<id>/post`

Body `{"text"}`. Only on the user's floor. Text starting `@slug ` asks that participant alone; anything else starts a round.

### `POST /api/meetings/<id>/skip`

Skips the current speaker.

### `DELETE /api/meetings/<id>`

Deletes the meeting and its transcript. When it was still running, each participant is told it is over. Answers `{"ok": true, "deleted": <id>}`.

### `POST /api/meetings/<id>/close`

Closes the meeting, or, with a facilitator, moves it to `closing` and asks the facilitator for the minutes.

## Preferences

Per-user console settings kept on the server, so every browser and the phone's home-screen app show the same thing. One file per console user in `data/console-prefs/<user>.json` (mode 0600); with no users file (open mode) they are shared under `_open`.

### `GET /api/prefs/sidebar`

`{"sidebar": {"groups": [{"id", "name", "collapsed"}], "assignments": {"<slug>": "<group id>"}}}`, or `{"sidebar": null}` when this user never saved one. The page keeps a copy in local storage for the first paint only, and uploads a layout made before the server kept it the first time it finds no server copy.

### `POST /api/prefs/sidebar`

Body `{"sidebar": {...}}` in the shape above: at least one group, unique string ids, string names, assignments mapping a slug to a group id. `400` for any other shape, `413` over 64 KB. Answers `{"ok": true, "sidebar": {...}}` with the stored value.

## Fleet (the cousin list and the inspector)

### `GET /api/cousins`

`{"cousins": [row, ...]}`. Read fresh from `cousins/*/cousin.toml` on every call and filled in with live state. A row:

| key | what it is |
|---|---|
| `slug`, `name`, `role` | from `[cousin]` |
| `type` | `cousin`, `worker`, or `remote` (a hive node) |
| `port`, `host` | chat port and `[chat] host` (host is null for a local cousin) |
| `home` | the cousin's home directory |
| `tmuxSession` | `[chat] tmux_session`, default the slug |
| `operator` | `[operator] name`, or null |
| `memoryScope` | `private`, `shared` or `both` |
| `heartbeat` | context heartbeat in seconds (default 3600) |
| `flipAt` | `[lifecycle] flip_at`, or null |
| `model`, `effort` | what the next start will use: the cousin's `[runtime]` value, else `config/harness.toml [agent]` default, else null |
| `hidden` | `[cousin] hidden` |
| `auth` | `claude` or `api_key`; null if cousin.toml holds a mode the framework doesn't know |
| `status` | `running` or `stopped`. Local cousin: the tmux session exists. Cousin with `[chat] host`: its chat server answers. Worker: always `running`. Runner cousin (`[agent] runner`): a runner holds its lock (`run/runner.lock`). |
| `attention` | for a running local cousin, the first string from `config/harness.toml attention_patterns` found in the last 20 lines of the pane (a login menu, say), else null |
| `chat` | `ok`, `down` or `none` (no port) from the chat server's `/health`; `console` for a runner cousin, whose chat the console serves itself |
| `active` | the last 20 pane lines changed in the last 60 seconds; for a runner cousin, a live turn (`running` or `waiting_permission`) |
| `pid`, `uptime_seconds` | the agent process in the tmux pane and its age (a runner cousin: the `cousin-runner` process); null when unknown, never 0 |
| `activity` | first 200 characters of `data/last-activity.txt` |
| `lastMsgTs` | unix time of the cousin's newest reply in your thread (the `[operator] name`, last 20 rows), 0 if none |
| `tokensSpent` | today's token total, 0 when token counting isn't set up |
| `runner` | null for a tmux cousin. A runner cousin: `{"alive", "state", "since", "session", "kind", "pid", "unsupported"}` from its own stores: `alive` whether a runner holds its lock, `state` the last state its primary event stream recorded (with `since`, that event's time; it stays the last one recorded after the runner is gone, so read it with `alive`), `kind` (`sdk` or `fake`), `pid` and `unsupported` (the contract items the runner declares it does not support) from the `runner` event `cousin-runner` writes at start |
| `supervisor` | `{"state": ...}` for a runner cousin the running `cousin-supervisor` holds as a child (`running`, `backoff`, `failing`, `stopped`), read from its `run/supervisor.json`; null when no supervisor runs (or the file is stale) or it holds no child for this cousin. Null is unknown, never stopped |

With the hive on, remote nodes follow the local rows. They carry the same keys (the local-only ones null or 0) plus `remote: true`, `remoteState` (`online`, `offline`, `pending` = built but never checked in, `revoked`), `online`, `revoked`, `checkedIn`, `lastSeen`, `version`. For a remote row `status` is `running` when online, and `chat` is derived (`ok` online, `down` offline, `none` before the first checkin), never probed. If a slug is both local and a node, the local row wins.

### `POST /api/cousins`

Spawn a cousin. Body:

```json
{"slug": "wren", "role": "research helper", "voice": "dry, short answers",
 "name": "Wren", "role_paragraph": "...", "port": 8611, "operator": "ana",
 "model": "claude-opus-5", "effort": "high", "heartbeat": 3600,
 "memory_scope": "private", "runner": "sdk", "account": "metered"}
```

`slug`, `role` and `voice` are required (the CLAUDE.md template won't render without a voice). The rest are optional; empty means the default applies and no key is written. `runner` (`sdk` or `fake`) and `account` go to `[agent]`; left out, the install's `COUSIN_DEFAULT_RUNNER` and `COUSIN_DEFAULT_ACCOUNT` apply (unset: a tmux cousin). An account needs a runner and must be in `config/accounts.toml`. `201 {"ok": true, "slug", "home", "port"}` and a `cousins-refresh` event. `400` bad input (the message says which), `409` the slug exists or a leftover directory squats it. This only creates the cousin; the page follows it with `/start`.

### `GET /api/spawn/options`

What the spawn dialog offers:

```json
{"models": [...], "default_model": "...", "efforts": ["low","medium","high","xhigh","max"],
 "default_effort": "high", "memory_scopes": ["private","shared"],
 "default_memory_scope": "private", "default_heartbeat": 3600,
 "heartbeat_bounds": [60, 2592000], "operator_max_chars": 64}
```

`models`, `default_model` and `default_effort` come from `config/harness.toml [agent]`. No `models` there means the built-in list; no `default_model` means the first model in the list. `500` if harness.toml exists but can't be read.

### `DELETE /api/cousins/<slug>`

Dismiss. Stops the cousin, tars the whole home (minus `.secrets/`) to `data/dismissed/<slug>-<YYYYmmdd-HHMMSS>.tar.gz`, then deletes the home. If the archive fails, nothing is deleted and you get `500` with `refusing to delete: ...`. `200 {"ok": true, "slug", "status": "deleted", "archive", "left_in_place": [...]}`. `left_in_place` lists the harness directories (`transcripts_dir`, `auto_memory_dir` from harness.toml) it didn't touch; clean those yourself if you want them gone.

### `POST /api/cousins/<slug>/start`

Starts the tmux session with the agent from `config/agent-cmd`, and the chat server if it isn't answering. `200 {"ok": true, "slug", "status": "started" | "already running", "chat_server": "started" | "reused" | "not running"}`. If the session is already up but the chat server is down, the chat server is started. `500` when `config/agent-cmd` is missing or tmux fails. Emits `cousin-status` `starting`.

A runner cousin (`[agent] runner = "sdk"` or `"fake"`) is started by the running `cousin-supervisor` instead ([commands](../commands.md)): no `config/agent-cmd`, no tmux, no chat server. `200 {"ok": true, "slug", "status": "started" | "already running"}`; already running means a runner holds the cousin's lock. `503` when no supervisor runs for the install (the message says to run `cousin-supervisor run`), `500` with its reason when the supervisor refuses.

### `POST /api/cousins/<slug>/stop`

Body (optional): `{"clean": true}` (the default). A running cousin stops cleanly in the background ([lifecycle](lifecycle.md#a-clean-stop)): `202 {"ok": true, "slug", "status": "closing", "started_at"}`, `cousin-status` `closing`, then `stopped` (or `stop failed`) and a `cousins-refresh` when it is done; `409` while a flip or another clean stop of that cousin is running. The run's stages show on `GET /api/cousins/<slug>/flip`.

With `{"clean": false}`, or when the cousin isn't running: kills the tmux session and stops the chat server at once. Idempotent. `200 {"ok": true, "slug", "status": "stopped", "tmux": "stopped" | "already stopped", "chat_server": "stopped" | "not running"}`. Emits `cousin-status` `stopping`. `400` when `clean` is not a boolean.

A runner cousin always stops through the supervisor, clean or not: a clean stop is the runner's own SIGTERM path (it finishes the turn in hand, then exits, which can take up to about 35 seconds). The route does not wait for that: `202 {"ok": true, "slug", "status": "stopping", "runner": "stopping", "supervisor": "running"}` once the runner is signalled, and the row's `supervisor.state` turns `stopped` when it is down. When there was nothing to stop the answer is `200` with `"status": "stopped"` and `"runner": "stopped" | "not running"`, `"supervisor": "running" | "not running"` (`error` is added when the supervisor refused). The stop holds the runner down until the next start, across a supervisor or container restart too (`<home>/run/held`).

### `POST /api/cousins/<slug>/restart`

Immediate stop (as `{"clean": false}`), wait about a second, start. `200 {"ok": true, "target": "cousin/<slug>", "stop": {...}, "start": {...}}`. If the start fails you get the start's status code with `ok: false` and its error body under `start`.

A runner cousin that is running is signalled and answered at once, `202 {"ok": true, "slug", "status": "stopping", "runner": "stopping", "supervisor": "running", "target": "cousin/<slug>"}`; the console starts it again in the background once the supervisor reports it down, and says how that went with a `cousin-status` event (`started` or `start failed`) and a `cousins-refresh`. With nothing to stop it is the `200` above.

### `GET /api/cousins/<slug>/auth`

```json
{"ok": true, "slug": "wren", "mode": "claude", "modes": ["claude","api_key"],
 "default": "claude", "configured": true,
 "key": {"set": true, "last4": "x9Qa", "error": null}, "key_env": "ANTHROPIC_API_KEY"}
```

`configured` says whether `config/harness.toml [auth.api_key]` exists; `key_env` only appears when it does. `key` describes `.secrets/api-key.env`: whether it's usable, the last four characters when the key is at least 16 long, and why not when it isn't. The key itself never comes back from any route.

### `POST /api/cousins/<slug>/auth`

Switch the auth mode. Body `{"mode": "api_key", "force": false, "restart": true}`. For `api_key` the key file is checked and the isolated harness config dir rebuilt. A running agent restarts on the same session unless `restart` is false. `200 {"ok": true, "slug", "mode", "previous", "running", "restarted", ..., "auth": <the GET body>}`. `409 {"busy": true}` when the pane looks mid-turn (`busy_patterns` in harness.toml) and `force` is false. `400` unknown mode, unusable key, or a restart that can't resume. A refusal changes nothing.

### `POST /api/cousins/<slug>/auth/key`

Body `{"key": "sk-..."}` (a bare key or a `KEY_ENV=key` line). Writes `.secrets/api-key.env` (dir 0700, file 0600). Answers with the GET body above. `400` for an unusable key or no `[auth.api_key]`.

### `POST /api/cousins/<slug>/role`

Body `{"role": "..."}`, up to 5000 characters. Rewrites `[cousin] role` in place, keeping every other line. `200 {"ok": true, "slug", "role"}`.

### `GET /api/cousins/<slug>/claude-md`

`{"ok": true, "slug", "path", "content", "bytes"}`. A missing file is `content: ""`, `bytes: 0`, `missing: true`, not an error.

### `POST /api/cousins/<slug>/claude-md`

Body `{"content": "..."}`, up to 200000 characters. The old file is copied to `data/claude-md-backups/CLAUDE-<unix>.md` first. `200 {"ok": true, "slug", "bytes"}`. The running agent reads it at its next start or flip.

### `POST /api/cousins/<slug>/model`

Body `{"model": "claude-opus-5"}`. Written to `[runtime] model` in cousin.toml. It has to be one word of letters, digits and `._:/+-`, because it goes into the agent's argv. `200 {"ok": true, "slug", "model", "restart_required": true}`: the running agent keeps the model it started with. `400` empty or splittable.

### `POST /api/cousins/<slug>/effort`

Body `{"effort": "high"}`, one of `low`, `medium`, `high`, `xhigh`, `max`. Written to `[runtime] effort`. `200 {"ok": true, "slug", "effort", "restart_required": true}`. `400` any other value.

### `POST /api/cousins/<slug>/operator`

Body `{"operator": "ana"}`. Written to `[operator] name`. One line, 1 to 64 characters, no leading or trailing spaces, no control characters. `200 {"ok": true, "slug", "operator", "restart_required": true}`: the chat server reads it once at start to tell your messages from other cousins', so restart the cousin. `400` leaves the file untouched.

### `POST /api/cousins/<slug>/memory-scope`

Body `{"memory_scope": "shared"}`, one of `private`, `shared`, `both`. Written to `[memory] scope`. `200 {"ok": true, "slug", "memory_scope", "restart_required": false}`: it's read on every shared-tier call.

### `POST /api/cousins/<slug>/heartbeat`

Body `{"heartbeat": 1800}`, whole seconds from 60 to 2592000 (30 days). Written to `[heartbeat] context_beat_seconds`. `200 {"ok": true, "slug", "heartbeat", "restart_required": false}`: the loops daemon reads it on its next tick.

The three identity routes above also emit `cousins-refresh`.

### `POST /api/cousins/<slug>/hidden`

Body `{"hidden": true}`. Sets or removes `[cousin] hidden`. `200 {"ok": true, "slug", "hidden"}`. Only the console reads this flag.

### `POST /api/cousins/<slug>/peer`

Send a message from one cousin to another. Body `{"to": "kestrel", "text": "..."}`. It's posted to Kestrel's chat server `/api/send` with `user` set to Wren's display name, so it lands as `(Chat Wren): ...` like `cousin-chat send`. `200 {"ok": true, "to", "id"}`. `400` empty text or `to` is the same cousin, `404` either cousin unknown, `502` Kestrel's chat server is down.

### `GET /api/cousins/<slug>/flip`

The last flip this console ran for the cousin, and any timed flip waiting in the loops daemon:

```json
{"ok": true, "status": "idle" | "running" | "done" | "failed" | "stale_marker",
 "started_at": 1758200000.0, "stages": [...], "result": {...},
 "recovery": {"marker": "...", "started_at": ..., "hint": "..."},
 "pending": {"request_id": 12, "fire_at": ..., "seconds_until_fire": 240}}
```

`stages` and `result` appear once a flip has finished; they're what `cousin-flip` returns (see [lifecycle](lifecycle.md)). `stale_marker` means `data/.flip-in-progress.json` exists and no flip is running here, so a flip died halfway. `pending` shows a timed flip from any source, including the transcript-size guard.

### `POST /api/cousins/<slug>/flip`

Body `{"confirm": false, "delay_seconds": 0}`.

- No delay: runs the flip on a background thread and answers `202 {"ok": true, "slug", "status": "running", "started_at"}` straight away. Progress comes as `cousin-flip` events. `409` if one is already running.
- `delay_seconds > 0`: queues a flip request for the loops daemon, which sends the T-5m / T-1m / T-30s warnings and fires it. `202 {"ok": true, "slug", "request_id", "fire_at", "delay_seconds"}`. `409` if a timed flip is already pending.

`400` if `delay_seconds` isn't a non-negative integer.

### `POST /api/cousins/<slug>/flip/cancel`

Cancels a pending timed flip. `200 {"ok": true, "slug", "was_pending": bool}`. A flip that is already running can't be cancelled: `409`.

### `GET /api/tokens`

Token use per cousin per day for the last 14 days (UTC), read from the harness transcripts:

```json
{"available": true, "cousins": [{"slug": "wren", "name": "Wren",
  "series": [{"day": "2026-09-18", "total": 812345, "output": 20311}]}]}
```

Each cousin also carries `cache`: the prompt-cache hit rate, `cache_read / (cache_read + cache_creation + input)` from the usage the model reported, `{"rate": <the 14 days>, "days": [{"day", "read", "creation", "input", "rate"}]}`, one row per day oldest first. `rate` is null when the window (or the day) holds no usage; a result that carried no usage adds nothing to either side, so it is left out rather than counted as a miss. A runner cousin (`[agent] runner = "sdk"`) is read from its own `data/usage.db` only, with no transcript seam needed.

It needs `transcripts_dir` in `config/harness.toml`. Every transcript in the cousin's transcripts directory touched in the window is read, its sessions (a cousin that flips daily has one per day) and their subagents, and each message counts once (the harness writes one line per content block, each repeating the usage). The total adds input, output, cache read and cache creation tokens. Without the config: `{"available": false, "reason": "...", "cousins": []}`. The transcripts are read incrementally, so the first call after a console start is the slow one.

## Chat (proxied to each cousin)

These forward to the cousin's own chat server ([chat API](chat-api.md)). A runner cousin (`[agent] runner`, its home on this machine) runs no chat server: for it the console answers the same routes itself over the cousin's `data/chat.db`, through the library the chat server answers with, so the body and the `400` texts are the same on both lanes. Its send stores the row and delivers it to the cousin's inbox (a `chat` item on the sender's thread, an image handed on as its file), then fires the cousin's chat hooks; a reaction tells the cousin with a `reaction` item. The console stores no messages. The cousin comes from `cousin` in the query or body. `400` bad slug, `404` unknown cousin, `502 {"ok": false, "error": ...}` if the chat server is unreachable, has no port, or answers non-JSON. Any JSON answer from the chat server comes back with its own status.

For a slug that isn't local but is a hive node, the console proxies to where the node last checked in from and sends the node's token as a bearer. A revoked node is `404`, one that never checked in is `502`. Remote cousins have no pane, no inbox files and no media folders on this machine.

### `GET /api/messages`

Query: `cousin`, `user` (both required), `limit` (default 200), `before`, `since`, `archived` (`0`, `1`, `all`; default `0`). Forwards to `/api/history`. Answers the chat server's `{"messages", "total", "has_more"}` plus `"cousin"`. Each message gets an `attachment: {"url", "kind"}` when there is a file for it: an inbound image under `chat/inbound/<id>.<ext>` (kind `image`), or the row's `attachment_path` when it sits in `chat/images/`, `chat/audio/` or `chat/video/`. `kind` is `image`, `audio` or `video` (a stored `voice` shows as `audio`).

### `GET /api/search`

Query: `cousin` (required), `q`, `user`, `archived`. Forwards to `/api/search`. `{"messages": [...], "cousin"}`, newest first, at most 50, attachments added as above.

### `POST /api/chat/send`

Body `{"cousin": "wren", "user": "ana", "message": "hi", "image": "data:image/png;base64,...", "reply_to": {...}}`. `cousin` and `user` required. Forwards `user`, `message`, and `image` / `reply_to` when present to `/api/send` with a 15 second timeout. Answers `{"ok": true, "id", "timestamp"}`.

### `POST /api/chat/archive`

Body `{"cousin", "user", "keep": 0}`. Forwards to `/api/archive`. `{"ok": true, "archived": n}`.

### `POST /api/chat/reactions`

Body `{"cousin", "message_id", "user", "emoji", "action": "tap" | "remove"}` (`action` defaults to `tap`). Forwards to `/api/reactions`. `{"message_id", "op": "added" | "bumped" | "removed", "reactions": [...]}`.

### `GET /api/chat/inbound/<slug>/<name>`

One inbound image from `<home>/chat/inbound/`. The name has to be `<message id>.<png|jpg|jpeg|gif|webp>` and sit directly in that folder, otherwise `404`. `Cache-Control: private, max-age=3600`.

### `GET /api/chat/media/<slug>/<folder>/<name>`

One generated file from `<home>/chat/<folder>/`, folder `images`, `audio` or `video`. The name starts with a letter or digit and uses only letters, digits, `.`, `_`, `-`. Allowed suffixes: images png, jpg, jpeg, gif, webp; audio mp3, ogg, oga, opus, wav, m4a, webm; video mp4, webm, mov, m4v. Anything else is `404`. A single `Range: bytes=` header gets `206` with `Content-Range` (that's how the browser seeks a video), an impossible range `416`, otherwise `200` with `Accept-Ranges: bytes`.

## The pane (the tmux terminal)

All four resolve the cousin's tmux session (`[chat] tmux_session`, default the slug) through the console's `--tmux-bin` and `--tmux-socket`. For a cousin with `[chat] host` they run tmux over `ssh <host>` with the remote user's default socket. `404` unknown cousin, `400` no tmux session configured, `409 session not running`.

### `GET /api/pane`

Query `cousin`, `lines` (default 200). `{"text": "..."}`: `capture-pane -p -e` with colours kept and trailing blank lines trimmed.

### `GET /api/pane/stream`

Query `cousin`, `lines`. A server-sent event stream. The console polls `capture-pane` every half second and sends:

| event | data | when |
|---|---|---|
| `pane` | `{"text", "state", "ts", "changed": true}` | on connect, on every change, and after a geometry change |
| `geom` | `{"cols", "rows", "ts"}` | the pane size changed (checked every 2 s); a fresh `pane` follows |
| `heartbeat` | `{"ts"}` | 3 s without a change |
| `: tick` | comment | between polls, keeps the connection alive |

A `pane` frame is meant to be written into a freshly reset terminal. `text` is the capture followed by a short control tail: mouse mode on (`ESC[?1000h ESC[?1006h`) when the program tracks the mouse with SGR reports, the cursor moved to tmux's cursor cell, and the cursor shown or hidden to match. `state` is `{"alt", "mouse", "sgr", "cols", "rows", "cx", "cy", "cursor", "top"}` (alternate screen, mouse tracking, SGR mouse, pane size, cursor cell, cursor visible, number of frame lines above the screen's first row), or null when tmux gives nothing usable.

The mouse tail is there because a full-screen program runs on tmux's alternate screen, which has no history to capture. The only way to scroll it from the browser is to send the program its own wheel events, and xterm only produces those in mouse mode.

### `POST /api/pane/input`

Body `{"cousin": "wren", "data": "ls\r"}`: the raw bytes the browser terminal produced. They're turned into `tmux send-keys`: named keys for Enter, Tab, BSpace, Escape, arrows, Home/End, PageUp/PageDown, Delete/Insert, F1 to F4 and Ctrl-A to Ctrl-Z; text sent literally with `-l --` in 4000-character chunks; SGR mouse reports passed through only while the program tracks the mouse (to anything else they'd look like Escape plus text); every other escape sequence dropped. Input shares the chat injection lock, so a keystroke never lands in the middle of a chat message being typed in. `200 {"ok": true, "tokens": n}`, `500` with tmux's error.

### `POST /api/pane/resize`

Body `{"cousin", "cols", "rows"}`. Clamped to 20..400 columns and 5..200 rows, `400` if not integers. Resizes window 0. `200 {"ok": true, "cols", "rows"}`.

## The runner stream (a runner cousin's reasoning pane)

A runner cousin's view: its event stream live, an interrupt, and a say box. All three are for a runner cousin only; a tmux cousin is `409` (its view is the pane above). `404` unknown cousin.

### `GET /api/cousins/<slug>/stream`

A server-sent event stream over the runner's primary stream: the newest `data/stream/<session>.jsonl` whose first event is `runner` (`cousin-runner` writes it before anything else; a side session's stream never starts with one). A fresh connect starts at the newest 200 events, not the whole file. A reconnect resumes: its `Last-Event-ID` (or the `after` query parameter) is `<session>:<seq>`, and the stream continues right after that event; if that session is no longer the primary one (the runner restarted meanwhile), a `session` frame comes first and the new stream starts at its newest 200 events. `after=<seq>` alone applies to the current stream. The file is read from a byte offset, at most 1 MB at a time:

| event | data | when |
|---|---|---|
| `runner-event` | the event as written: `{"seq", "ts", "kind", "payload"}`; the frame's `id` is `<session>:<seq>` | the starting events, then each one as it is appended |
| `session` | `{"session"}` | the stream changed: the runner's first stream appeared (the pane was opened before the runner wrote anything), or the runner restarted (its new stream is read from its first event, the old one to its end first) |
| `: ping` | comment | 15 s without either |

`kind` is what the runner recorded: `state`, `turn_start`, `text`, `thinking` (`{"length", "text"[, "truncated"]}`, the text bounded at 8000 characters), `tool`, `tool_result`, `tool_call`, `result`, `user`, `error`, `auth`, `rate_limit`, `rollover`, `usage`, `system` and the rest the runner writes. `400` for an `after` that is neither `<seq>` nor `<session>:<seq>`.

### `POST /api/cousins/<slug>/interrupt`

No body. Puts an `interrupt` item in the cousin's inbox and waits up to 5 s for the runner to close it: `200 {"ok": true, "outcome": "delivered"}` when the live turn was interrupted, `{"ok": false, "outcome": "failed"}` when no turn was running or the agent refused the interrupt (the reason is in the inbox row's detail), `{"ok": false, "outcome": "queued"}` when the runner did not answer in time. `409 {"ok": false, "error": "no runner is running"}` when no runner holds the cousin's lock (nothing is put).

### `POST /api/cousins/<slug>/say`

Body `{"text": "..."}`. A `chat` item on the operator's thread (`operator:<[operator] name>`), sent as the configured operator whichever console user types it (the chat page sends as the operator the same way), put in the cousin's inbox: the runner writes it into a live turn, or takes it next. Not stored in `chat.db`: it is the pane's input, as typing into a tmux pane is. A login code while a login flow waits on this cousin is diverted first, as on every operator send path, and never delivered: `200 {"ok": true, "outcome": "diverted"}`. Otherwise `200 {"ok": true, "outcome": "queued"}`; `400` no text; `409` no operator configured.

## Jobs

Rows from `data/jobs.db`: `id`, `spawned_by`, `kind`, `title`, `description`, `status` (`running`, `done`, `failed`, `cancelled`), `started_at`, `finished_at`, `exit_code`, `result_summary`, `log_path`, `pid`, `command`. Jobs are created by `cousin-job`, not through the console.

### `GET /api/jobs`

Query: `status`, `spawned_by`, `kind`, `active_only` (`1`/`true`), `since_hours`, `limit` (default 200). `{"jobs": [...]}`, running first, then newest. At most once every 5 minutes a list call also tidies up: jobs `running` for more than 24 hours are marked `failed`, and the table is capped at 1000 rows. A failure there is logged, the list still answers.

### `GET /api/jobs/<id>`

`{"ok": true, "job": row}` or `404`.

### `GET /api/jobs/<id>/log`

Query `lines` (default 40), `from` (byte offset). `{"ok": true, "log", "log_path", "size", "next", "has_log"}`. Without `from` you get the last `lines` lines of the final 64 KB. With `from` you get up to 64 KB from that offset, cut at the last newline when the cap was hit, and `next` is where to ask next. Keep asking `from=<next>` to follow a log without repeats. A job with no log: `log: ""`, `log_path: null`, `has_log: false`. A log file that doesn't exist yet: a placeholder line.

### `POST /api/jobs/<id>`

Body with any of `status`, `result_summary`, `exit_code`, `title`, `description`. Setting `status: "cancelled"` on a running job with a pid sends it SIGTERM first. `200 {"ok": true, "job": row}` and a `job-update` event. `400` no fields or a bad status.

### `DELETE /api/jobs/<id>`

Removes the row, and the log file too if it's one `cousin-job` created in its own log directory. `200 {"ok": true}` and a `job-delete` event.

## Loops

The console reads loop state and queues requests; the loops daemon does the firing. See [loops](loops.md) for the model. Every loops response carries `"daemon": {"ok", "last_tick"?, "message"}`; when `ok` is false the message says `loops daemon has never run` or `loops daemon down (last tick NNs ago)`.

### `GET /api/loops`

`{"loops": [row], "errors": [...], "daemon": {...}}`. One row per `[[loops]]` entry of every cousin (disabled ones included), plus a `context-heartbeat` row per non-worker cousin:

| key | what it is |
|---|---|
| `cousin`, `name` | |
| `state` | `healthy` (fired at least once), `idle` (never fired), `disabled`, `failed` (a worker loop whose last job failed) |
| `interval` | `interval_seconds`, 0 for daily/cron loops |
| `schedule` | `{"interval_seconds", "daily_at", "cron", "days"}` |
| `prompt`, `enabled`, `hidden` | |
| `lastFireTs`, `lastTick` | unix time of the last fire and seconds since, 0 if never |
| `nextFireTs` | when it's due next, 0 when disabled |
| `drift` | how many seconds longer than the interval the last gap between fires was |
| `note` | first 60 characters of the prompt |
| `source` | always `framework` |

`errors` names each cousin.toml whose loops couldn't be read.

### `GET /api/loops/recent`

`{"fires": [{"cousin", "loop", "ago"}], "daemon"}`: the 20 newest fires, heartbeats included.

### `GET /api/loops/drift/<slug>/<name>`

`{"ok": true, "slug", "name", "interval", "n", "points": [{"t", "interval", "drift"}]}`: the gaps between consecutive fires from `data/loops-fires.jsonl`, at most the last 80. Fewer than two fires means `n: 0`.

### `GET /api/cousins/<slug>/loops`

`{"loops": [entry], "last_beat", "last_fires": {"<name>": ts}, "daemon", "errors"?}`. The entries are the `[[loops]]` tables as the loader normalises them.

### `POST /api/cousins/<slug>/loops`

Body `{"loops": [entry, ...]}`. Replaces the whole `[[loops]]` array after validating every entry (`400` naming the bad one). `200 {"ok": true, "slug", "loops"}` and a `loops-refresh` event. The daemon picks it up on its next tick.

### `POST /api/cousins/<slug>/loops/<name>/hidden`

Body `{"hidden": true}`. `200 {"ok": true, "slug", "loops"}`. `404` unknown loop. `400` if the cousin's loops have errors: fix those by hand first, because saving would drop them.

### `POST /api/cousins/<slug>/loops/<name>/fire`

Queues a fire request (`context-heartbeat` works too). `202 {"ok": true, "slug", "name", "request_id"}`. That means requested, not fired: if the daemon is down the request expires and says so in `cousin-loops requests`.

## Memory

### `GET /api/memory`

The old flat view: `{"tree": {"shared/": {file: entry}, "<slug>/": {file: entry}}}`, entry `{"size", "updated" (seconds ago), "preview" (first 400 characters)}`. Lists `shared/*.md` and the first 50 `memory/*.md` of each cousin.

The eight routes after it are the per-cousin memory explorer. Paths are relative to the cousin home. Absolute paths and `..` are `400`, a path that leads out of the home through a link is `403`, anything under `.secrets/` is `404`.

### `GET /api/memory/<slug>/overview`

`{"slug", "layers": [{"id", "title", "count", "updated", ...}], "insights": {...}}`. One entry per memory layer with counts and modification times (`updated` is epoch seconds or null), and insights: entries per truth level, sources, topics, most recalled files, memory and note files never recalled, dead links in MEMORY.md, daily files waiting to be folded, and so on.

### `GET /api/memory/<slug>/raw`

Raw memory entries, newest first: `{"entries", "total", "offset", "limit", "facets": {"levels", "sources", "topics"}}`. Query:

| param | meaning |
|---|---|
| `level` | comma list of truth levels, `L0_OPERATOR` to `L5_OBSOLETE` |
| `topic`, `q`, `source` | filters |
| `since`, `until` | `YYYY-MM-DD` |
| `tier` | `live` (default: daily files plus monthly digests), `daily`, `digest`, `archive`, `all`; anything else `400` |
| `limit`, `offset` | default 200, max 1000 |

Each entry is broken out into fields (`tier`, `file`, `topic`, `content`, `truth_level`, `level`, `source`, `timestamp`, `id`, `extra`, ...) and carries `ref: {"path", "line_no", "sha"}` for deleting it. Archive entries have `ref: null`: the archive stays whole.

### `GET /api/memory/<slug>/decisions`

The decisions log, newest first. Query `q`, `limit`, `offset`, `archives=1` to include rotated files. `{"entries": [{"timestamp", "topic", "decision", "reasoning", "file", "ref", "mirrors"}], "total", "offset", "limit"}`. `mirrors` are the raw entries `cousin-memory decide` wrote alongside the decision.

### `GET /api/memory/<slug>/files`

Query `layer`: `active`, `index`, `distilled`, `memory`, `notes`, `harness` or `legacy` (anything else `400`). `{"layer", "files": [{"path", "name", "size", "mtime", "recalls", "deletable", ...}]}`. Harness paths are relative to the harness auto-memory directory.

### `GET /api/memory/<slug>/file`

Query `path`, `start`, `count`, and `layer=harness` to read from the harness auto-memory directory (`404` if none is configured). Same answer as [the file reader](#get-apicousinsslugfilesread).

### `GET /api/memory/<slug>/trash`

`{"batches": [{"id", "deleted_at", "by", "items"}]}`, newest first. An item is `{"kind": "line", "path", "line_no", "sha", "line"}` or `{"kind": "file", "path", "size"}`.

### `POST /api/memory/<slug>/delete`

Moves to the trash, never destroys. One of these bodies:

```json
{"kind": "entry", "path": "memory/raw/2026-09-18.jsonl", "line_no": 12, "sha": "..."}
{"kind": "decision", "path": "data/decisions.jsonl", "line_no": 40, "sha": "...", "mirrors": true}
{"kind": "file", "path": "notes/old.md", "legacy": false}
```

What you can delete: lines in `memory/raw/*.jsonl` and `data/decisions.jsonl`; files under `memory/` (not `distilled/`, which is rebuilt from raw, not `raw/` files, the index files or the trash) and `notes/`; files under `legacy/` only with `legacy: true`. `mirrors: true` takes the decision's raw mirror entries along. A line that moved is found again by its hash; one that's gone is `409`.

The batch lands in `<home>/memory/.trash/<id>/` with one audit line per item in `memory/.trash/audit.jsonl`, attributed to the logged-in user (`console` with no login). Removing a raw entry reruns the distiller. `200 {"ok": true, "trash": manifest, "effects": {...}}` and a `memory-change` event.

### `POST /api/memory/<slug>/obsolete`

Body `{"topic": "...", "why": "...", "force": false}`. Appends an L5 entry for the topic, recorded as by the logged-in user with source `console`, then rebuilds the distilled views, which leave the topic out until a later entry brings it back. Nothing is removed from raw. `200` with the entry and `effects` (`distilled`, `obsolete_topics`, or `distill_error` if the rebuild failed; the mark is written either way) and a `memory-change` event with action `obsolete`. `400` when topic or why is missing, the reason is empty, or the topic has no raw entries and `force` is off.

### `POST /api/memory/<slug>/restore`

Body `{"id": "..."}`. Files go back to their path, lines back into their file at their old position. `200 {"ok": true, "restored": manifest, "effects": {...}}` and a `memory-change` event. `409` if something is already back in the way, `404` unknown id. The CLI does the same with `cousin-memory trash restore <id>`.

## Cousin files

A read-only browser over one cousin home. Same path rules as the memory explorer: `400` absolute or `..`, `403` out of the home, `404` for `.secrets/` at any depth. A link that points out of the home is listed with `outside: true` and never followed.

### `GET /api/cousins/<slug>/files`

Query `path` (a directory, default the home), `hidden=1` to include dotfiles. `{"path", "entries": [{"name", "path", "type": "dir"|"file"|"link"|"other", "size", "mtime", "outside"?, "target_type"?}], "truncated", "total"}`. One level, directories first, at most 2000 entries. `404` not a directory.

### `GET /api/cousins/<slug>/files/read`

Query `path`, `start` (1-based line, default 1), `count` (default 1000, max 5000). `{"kind": "markdown"|"text"|"image"|"binary", "path", "size", "mtime", "mime", ...}`. Markdown up to 2 MB comes back whole in `text`. Other text (and bigger Markdown) comes as `lines` from `start`, with `total_lines` and `more`. A file with a NUL or invalid UTF-8 in its first 8 KB is `binary` and has no content; images have none either (use download).

### `GET /api/cousins/<slug>/files/download`

Query `path`. Streams the bytes. Raster images inline with their type, everything else (SVG too) as an `application/octet-stream` attachment. Always `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox; default-src 'none'`.

## Shared memory review

Proposals to the shared tier wait in `shared/proposed/` as `<slug>__<file>.md`. See [memory](../memory.md).

### `GET /api/shared/list`

`{"canonical": [{"name", "size", "mtime", "sha"}], "pending": [{"name", "slug", "origin", "size", "mtime", "sha"}]}`. `sha` is the first 12 hex characters of SHA-256; for a pending file `slug` is the proposer and `origin` the file it would become.

### `GET /api/shared/content`

Query `scope` (`canonical` default, or `pending`) and `name`. `{"content"}`. `400` no name or bad scope, `404` not a file directly in that folder.

### `GET /api/shared/diff`

Query `file` (the shared file name) and `slug` (the proposer). `{"diff"}`. `400` a parameter missing, `404` no such proposal.

### `GET /api/shared/audit`

Query `n` (default 100). `{"entries": [...]}`, the last `n` lines of `shared/audit.jsonl`, newest first. Each has `ts`, `kind` (`propose`, `promote`, `reject`, ...), `actor`, `file`, and `proposer` or `reason` when there is one.

### `POST /api/shared/approve`

Body `{"slug": "wren", "file": "house-rules.md", "by": "ana"}`. Promotes Wren's proposal over the shared file. `200 {"ok": true, "file"}`.

### `POST /api/shared/reject`

Body `{"slug", "file", "reason"?, "by"?}`. Drops the proposal. `200 {"ok": true, "file"}`.

For both: the reviewer is the logged-in user, and `by` is only read (and then required, `400` without it) when there's no users file. A reviewer who isn't in `config/shared-reviewers.json`, or who is the proposer, gets `403`. A missing proposal is `404`.

## Tracker

Items from `data/tracker.db`: `{"id", "title", "domain", "state", "tags", "owner", "notes", "created_at", "updated_at"}`, `state` one of `open`, `active`, `blocked`, `done`, `dropped`. `tags` is a list of strings, the rest are strings. Ids are never reused. Every change emits `tracker-change`.

### `GET /api/tracker`

Query `owner`, `state`, `domain`, `tag`. `{"items": [...]}`, open items first, then most recently updated. `400` bad state.

### `POST /api/tracker`

Body `{"title", "domain"?, "state"?, "tags"?, "owner"?, "notes"?}`. `title` required, `state` defaults to `open`. `200 {"ok": true, "item"}`. `400` blank title or bad state.

### `GET /api/tracker/<id>`

`{"item"}` or `404`.

### `POST /api/tracker/<id>`

Any subset of the fields. `{"ok": true, "item"}`. `400` bad state, `404` unknown id.

### `DELETE /api/tracker/<id>`

`{"ok": true, "deleted": id}` or `404`.

## Host and the console itself

### `GET /api/version`

No login needed. `{"version", "commit", "repo_url", "commit_url"}`. Version and commit are read once when the console starts, so a console you forgot to restart after an update shows the old values. `commit` is null outside a git checkout.

### `GET /api/host`

```json
{"host": "box", "kernel": "6.12.0", "uptime": 86400,
 "cpu": {"pct": 12.5, "load1": 0.5, "load5": 0.4, "load15": 0.3},
 "mem": {"total": 31.2, "used": 9.8, "cached": 12.1},
 "disk": {"total": 460.0, "used": 120.3},
 "net": {"rx": 0.12, "tx": 0.03, "rx_total_gb": 41.2, "tx_total_gb": 3.1},
 "console_uptime": 3600}
```

Memory and disk in GB (used memory is MemTotal minus MemAvailable, disk is `/`), network in MB/s since the previous call, loopback and container interfaces left out. CPU `pct` is the 1-minute load over the core count. On a system without `/proc` the blocks read as zeros.

### `POST /api/admin/restart/framework`

Restarts the console by exiting. Answers `200 {"ok": true, "target": "console", "supervised": bool, "eta_seconds": 4}`, then exits 0 about 0.6 s later. `supervised` is true when it runs under systemd (it checks `INVOCATION_ID`) or under `cousin-supervisor` (`COUSIN_SUPERVISED`), which starts it again at once. If it's false, nothing will start it again: restart means stop.

## `GET /api/events`

The live stream every open tab holds. Server-sent events, each frame a `data:` line with `{"kind": "...", "data": ...}`. The first frame is always a `snapshot`. A `: ping` comment goes out after 25 s of silence.

The events come from two places: route handlers announce what they just did, and a poller inside the console re-reads the stores and sends the differences (jobs, timed flip requests and the tracker every 2 s; the fleet, the loops and the fire times every 15 s). If a tab falls behind by 200 frames it's dropped; it reconnects and gets a fresh snapshot, so nothing is lost.

| kind | data | sent when |
|---|---|---|
| `snapshot` | `{"cousins", "loops", "jobs", "daemon"}` | on connect; the same rows the GET routes return |
| `cousins-refresh` | `[row]` | every fleet poll, and after spawn, identity edits, auth switches and hive changes |
| `loops-refresh` | `[row]` | every loops poll, and after a loops save |
| `cousin-status` | `{"slug", "status": "starting" \| "stopping" \| "switching auth"}` | a start, stop, restart, dismiss or auth switch begins |
| `job-add`, `job-update` | job row | a job appeared or changed |
| `job-delete` | `{"id"}` | a job went away |
| `cousin-flip` | `{"slug", "phase", ...}` | see below |
| `loop-fire` | `{"cousin", "loop", "ts"}` | a loop's last fire time moved forward |
| `tracker-change` | `{"id", "op": "add" \| "update" \| "delete"}` | a tracker item changed, through the console or anything else |
| `memory-change` | `{"slug", "action": "trash" \| "restore" \| "obsolete", ...}` | a memory delete, restore or obsolete mark through the console |

`cousin-flip` phases: `scheduled` (with `fire_at`, and `delay_seconds` or `request_id`), `started`, `complete` (with `ok: true`, and `new_generation`, `boot_packet_tokens`, `degraded_sections` when the console ran it), `failed` (with `ok: false` and `error`), `cancelled`. Timed flips the daemon runs show up through the poller: a request that turns `done` is `complete`, `failed` or `expired` is `failed`.

## Static files

`GET /` serves `index.html`, and `GET /<file>` serves files from `cousin_lib/console_static/` with one of these suffixes: `.html .jsx .js .css .json .webmanifest .svg .png .ico`. Anything that resolves outside that directory is `403`, anything else missing is `404`. Everything is `Cache-Control: no-store`, and `index.html` gets `?v=<mtime>` stamped on its local `src`/`href` links, so a redeploy shows up on the next reload. The page loads React, Babel, marked, mermaid and xterm from a CDN; that's the only outside fetch.

Non-GET requests outside `/api/` are `404`.

## Hive (remote cousins)

These only work when `config/hive.toml` has `enabled = true`. Otherwise `GET /api/hive` says `{"enabled": false}` (plus `error` if the file is broken) and the others are `404`. The node side, everything under `/hive/`, is in [the hive API](hive-api.md): those routes skip the network guard and the login and use the hive token instead. No `/api/` route looks at an `Authorization` header, so a hive token opens nothing here.

### `GET /api/hive`

`{"enabled": false}`, or `{"enabled": true, "public_url", "checkin_seconds", "home_chat_url", "default_port": 8210}`. The spawn dialog only offers "Remote" when it's enabled.

### `GET /api/hive/nodes`

`{"nodes": [row]}`: the remote rows on their own, same shape as in `GET /api/cousins`.

### `POST /api/hive/nodes`

Build a remote cousin. Body:

```json
{"slug": "kestrel", "name": "Kestrel", "role": "watches the garage box",
 "port": 8210, "brain": "placeholder", "agent_cmd": "",
 "home_chat": false, "reachable": true}
```

`slug` must not be a local cousin (`409`). `role` is required, up to 500 characters; `name` up to 64. `brain` is `placeholder` or `agent`, and `agent` needs `agent_cmd`, the command line the node runs with the prompt on stdin. `home_chat` bakes `home_chat_url` from hive.toml into the node (`400` if hive.toml has none). `reachable` (default true) binds the node's chat server on 0.0.0.0 so the console can reach it; false keeps it on 127.0.0.1.

It mints (or reuses) the node's token and builds the archive into a private directory under `shared/hive/downloads/`. `201`:

```json
{"ok": true, "slug": "kestrel",
 "download_url": "https://example.invalid/hive/download/<nonce>",
 "download_path": "/hive/download/<nonce>", "expires_at": 1758201000.0,
 "expires_in": 900, "filename": "kestrel-node.tar.gz",
 "install": "tar xzf kestrel-node.tar.gz && cd kestrel-node && ./install.sh",
 "curl": "curl -fsSL -o kestrel-node.tar.gz '...' && tar xzf ...",
 "note": "..."}
```

The link works once and expires after 15 minutes, because the archive holds the node's token. The file is deleted after the download, at expiry, or when the console stops.

### `POST /api/hive/nodes/<slug>/revoke`

Revokes every live token the node has; from then on the queen answers it `401`. `200 {"ok": true, "slug", "revoked": n}`. `404` no live token.

### `DELETE /api/hive/nodes/<slug>`

Forget a revoked node: its node and token rows go, its memory and inbox rows stay. `200 {"ok": true, "slug", ...}`. `409` it still has a live token (revoke first), `404` unknown.

## Routes the older console had

If you're coming from the older console these routes are gone, and nothing in this one calls them: `/api/agents`, `/api/agents/<cousin>/<id>/log`, `/api/backlog`, `/api/backlog/<id>`, `/api/chat/presence`, `/api/chat/engagement`, `/api/chat/audio/...`, `/api/chat/image/...`, `/api/chat/video/...`, `/api/cousins/<slug>/budget`, `/api/peer-messages`, `/api/sidebar`, `/api/liveness`, `/api/network-devices`, `/api/logs`, `/api/admin/restart/cousin/<slug>` (use `POST /api/cousins/<slug>/restart`) and `POST /api/jobs` (jobs are created with `cousin-job`).
