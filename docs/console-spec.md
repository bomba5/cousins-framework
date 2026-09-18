# Console specification: the API contract

The web console is the operator's daily surface over the fleet: the
overview, the cousin cards with their inspector and editors, chat with
the live terminal pane, jobs, memory and the shared-tier review, loops
with drift, tokens, the tracker, settings and account, and the restart
panels. This page is the contract every console route is implemented
and tested against. It was written from a full read of the source
framework's console (its backend and its six browser-compiled React
files), keeping every route a retained view calls and dropping every
route only a dropped view called. `docs/ui-spec.md` states the
architecture the console lives under; this page states the wire.

Two rules of `docs/ui-spec.md` are restated here because every route
below is shaped by them:

- **A console that dies loses nothing but its pixels.** The console
  owns two things: browser sessions (in memory) and the users file.
  Everything else it shows is a projection of a store some other
  component owns (the filesystem registry, `jobs.db`, the loops
  daemon's state and request store, each cousin's chat server, the
  shared tier, `cousin.toml`), and every command it accepts writes
  through the library a CLI would use. There are no caches whose
  contents anything else trusts.
- **Contract first.** Every route a retained view calls is on this
  page before it is implemented; a route the views do not call is not
  ported. The dropped ones are listed at the end so nobody re-derives
  them from the source.

One exception to `docs/ui-spec.md` is made knowingly and stated here:
the source console compiles its JSX in the browser from CDN-loaded
React, Babel and a few rendering libraries, and the port keeps that so
the views port as-is without a maintainer build step. Those script
tags are **the one runtime network fetch a browser makes** for this
console; the backend itself fetches nothing from outside the install.
An adopter who cannot allow that fetch vendors the same files under
the static directory and edits the tags; nothing else changes.

## Views retained and dropped

Retained, with the routes each one calls (all detailed below):

| view | routes |
|---|---|
| overview (the host panel, cousin table, totals, recent fires) | `GET /api/host`, `GET /api/cousins`, `GET /api/loops`, `GET /api/loops/recent` |
| cousins: cards, inspector, role editor, identity editors (operator, scope, heartbeat), CLAUDE.md editor, loops editor, spawn, dismiss, flip, restart, hide | `GET/POST /api/cousins`, `DELETE /api/cousins/<slug>`, `POST .../start`, `.../stop`, `.../restart`, `.../role`, `.../operator`, `.../memory-scope`, `.../heartbeat`, `GET/POST .../claude-md`, `GET/POST .../loops`, `POST .../hidden`, `POST .../peer`, `GET/POST .../flip`, `POST .../flip/cancel` |
| chat with the live pane | `GET /api/messages`, `GET /api/search`, `POST /api/chat/send`, `/api/chat/archive`, `/api/chat/reactions`, `GET /api/chat/inbound/...`, `GET /api/chat/media/...`, `GET /api/pane`, `GET /api/pane/stream`, `POST /api/pane/input`, `/api/pane/resize` |
| jobs | `GET /api/jobs`, `GET /api/jobs/<id>`, `GET /api/jobs/<id>/log`, `POST /api/jobs/<id>`, `DELETE /api/jobs/<id>` |
| memory with the shared-tier review | `GET /api/memory`, `GET /api/shared/list`, `.../content`, `.../diff`, `.../audit`, `POST /api/shared/approve`, `/api/shared/reject` |
| loops with drift | `GET /api/loops`, `GET /api/loops/drift/<slug>/<name>`, `GET/POST /api/cousins/<slug>/loops`, `POST .../loops/<name>/fire`, `.../loops/<name>/hidden` |
| tokens | `GET /api/tokens`, `GET /api/cousins` |
| tracker | `GET/POST /api/tracker`, `POST/DELETE /api/tracker/<id>` |
| settings and account | `GET /api/auth/me`, `POST /api/auth/login`, `/api/auth/logout`, `/api/auth/change-password` |
| restart panels | `POST /api/admin/restart/framework`, `POST /api/cousins/<slug>/restart` |
| the shell (sidebar, unread dots, keyboard shortcuts, live updates) | `GET /api/events` |

Dropped, with the reason: the backlog view (excluded by the operator);
the audio recorder and the media filter (the media display came back
on 2026-09-18 by the operator's decision: inline images, looping muted
video previews, audio players, a media on/off toggle and a viewer with
prev/next; see "The chat media viewer" below); the GPU box controls (install-specific
remote machine; see the note under "what the plan expected"); the
embedded game; presence and engagement pings (no daemon-owned store,
deferred with that decision in `docs/ui-spec.md`); the legacy agents
tracker view and the agent-log rows inside the inspector (`cousin-job`
replaced it; the jobs view is the surface); the per-cousin "effort"
selector in the chat header (it types a vendor-specific slash command
into the pane; the framework hardcodes no vendor); the per-cousin
token budget field (a config key nothing reads, forbidden by the
dead-key rule in `docs/loops-spec.md`); the server-side sidebar-group
store (a browser preference the console must not own; it moves to the
browser's local storage); the operator-only peer-message history
panel (its store does not exist here; the destination cousin's own
history is the record).

One retained view has no source frontend at all: the tracker. The
source backend served tracker routes that no view called; the port
plan retains the tracker, so its routes are specified here from the
backend's behaviour and the view is written fresh in the frontend
task.

## Conventions

- Every JSON response carries `Content-Type: application/json` and
  `Cache-Control: no-store`. Malformed request JSON is `400
  {"ok": false, "error": "malformed JSON"}`, never an empty object.
- Success bodies for commands carry `"ok": true`; failures carry
  `"ok": false` and an `error` string. Read views carry their data
  keys without `ok` unless stated.
- Status codes: 200 read or command done, 201 created, 202 accepted
  (work continues in the background), 400 bad input, 401 no session
  where one is required, 403 the network guard or a refused
  operation, 404 unknown cousin, job, loop, proposal or item, 409 a
  state conflict (already running, session absent, flip in progress),
  502 an upstream chat server unreachable or answering non-JSON, 500
  a local failure with the message in `error`.
- A `<slug>` in a path must match `^[a-z][a-z0-9_-]{1,31}$`; anything
  else is `400 {"error": "bad slug"}` before any lookup. A loop
  `<name>` must match `^[a-z][a-z0-9_-]{0,31}$`.
- A GET never mutates and never shells out to another host.
- The network guard (`cousin_lib.server.netguard.NetGuard`, built from
  `config/net-allowlist.json`) runs before anything else on every
  request including DELETE and the SSE streams; a denial is `403`.
- The framework root comes from `--root` or `FRAMEWORK_ROOT` exactly
  as for every other entry point (`docs/configuration.md`). The
  console's own settings are flags: `--port`, `--host` (default
  loopback), `--tmux-bin`, `--tmux-socket`.

## The auth model

The guard is an address-trust boundary; the users file adds a
per-person one on top of it for installs whose console is reachable
by more than the operator.

- **Users file:** `config/console-users.json`, shape
  `{"<user>": {"salt": "<hex>", "hash": "<hex>", "iterations": N}}`.
  Hashing is PBKDF2-HMAC-SHA256 (stdlib; the source used the same),
  a random 16-byte salt per user, 200000 iterations by default. The
  file is written atomically with mode 0600. There is no `scope`
  field: no per-user authorization is enforced, so none is stored
  (`docs/ui-spec.md`, "authorization is not faked"). Provisioning is
  `cousin-console adduser <name>` (password read from a prompt, never
  argv); the same command with an existing name resets that user.
- **Configured means enforced.** When the file exists with at least
  one user, every `/api/*` route except `POST /api/auth/login` and
  `GET /api/auth/me` requires a session; static files need none
  (the login form is part of the page). There is no address-based
  bypass: not for loopback, not for a "trusted LAN". The source had
  both, and a bypass that wide is no auth. Local CLIs never call the
  console (the reverse-dependency rule), so nothing needs one.
- **Not configured means open, and said.** With NO users file (the
  file is absent) the console serves everyone the guard admits -
  loopback, the RFC1918 private ranges and `config/net-allowlist.json`,
  not loopback alone - `GET /api/auth/me` reports `configured: false`,
  and the account panel shows the `adduser` line instead of a password
  form. Absent is the only state that opens the console.
- **Broken means closed.** A users file that is present but unusable
  (unreadable, not JSON, not an object, holding no users, or with an
  entry that is not an object) is never read as "no users": every
  `/api/*` route answers `503 {"ok": false, "error": "<file> is present
  but unusable (...)..."}`, login included and any live session
  regardless; `GET /api/auth/me` answers `200` with `configured: true`,
  `user: null` and that `error`, so the page can say why. The error is
  printed to stderr (the unit's journal) at startup and on each new
  error, and the startup line says `CLOSED`. `cousin-console adduser`
  refuses to write over a broken file (exit 1, before any prompt).
  Fix: restore the file from a backup, or remove it and run `adduser`.
- **Session cookie:** `console_session`, a 32-byte random token,
  `HttpOnly; SameSite=Strict; Path=/`, `Secure` when the console
  runs behind TLS. Sessions live in the console's memory only: a
  restart logs everyone out, which is the stated cost of the console
  owning nothing durable but the users file. Idle sessions expire
  after 30 days.

### `POST /api/auth/login`
Body `{"user": str, "password": str}`. `200 {"ok": true, "user": str}`
and the cookie; `401 {"ok": false, "error": "bad credentials"}` after
a constant-time compare; `400` when either field is missing;
`409 {"ok": false, "error": "auth not configured"}` when there is no
users file; `503` when the users file is present but unusable.

### `POST /api/auth/logout`
`200 {"ok": true}`; clears the cookie and forgets the session.

### `GET /api/auth/me`
`{"user": str|null, "configured": bool, "users": [str]}`. `users` is
the sorted list of configured names when the caller is authenticated,
else `[]`.

### `POST /api/auth/change-password`
Body `{"old_password": str, "new_password": str}`. Requires a session
(`401` otherwise). `403 {"error": "current password incorrect"}`,
`400` when the new password is shorter than 8 characters. `200
{"ok": true, "user": str}`; other sessions of that user stay valid.

## Fleet: cousins

The fleet is read from the filesystem registry
(`FrameworkConfig.list_cousins()`) on every call. Each row is enriched
with liveness (the tmux session exists, checked through the same
binary and socket the injection module uses), the chat server's
`/health`, the activity line `cousin-memory activity` writes, the
newest reply timestamp for the unread dot, and today's token total
from the tokens seam below.

### `GET /api/cousins`
`{"cousins": [row]}` where a row is:

| key | type | source |
|---|---|---|
| `slug`, `name`, `role` | str | `cousin.toml [cousin]` |
| `type` | `"cousin"` or `"worker"` | `[cousin] type` |
| `port` | int or null | `[chat] port` |
| `host` | str or null | `[chat] host` (remote cousins; pane and chat proxy use it) |
| `home` | str | the home path |
| `tmuxSession` | str | `[chat] tmux_session`, default the slug |
| `operator` | str or null | `[operator] name`; null is a real state, never a defaulted name |
| `memoryScope` | str | `[memory] scope` |
| `heartbeat` | int | `[heartbeat] context_beat_seconds` (3600 default) |
| `flipAt` | str or null | `[lifecycle] flip_at` |
| `model` | str or null | what the next start renders into the agent command's `{model}`: `[runtime] model`, else `config/harness.toml [agent] default_model`, else null (a start with the placeholder would then fail naming both files; the row shows nothing rather than a guess) |
| `effort` | str or null | likewise for `{effort}`: `[runtime] effort`, else `[agent] default_effort`, else null; one of `low`, `medium`, `high`, `xhigh`, `max` |
| `hidden` | bool | `[cousin] hidden` (default false; the console is this key's consumer) |
| `auth` | str or null | `[runtime] auth`, the agent's auth mode (see `GET /api/cousins/<slug>/auth`); the default mode when unset, null when the file holds a value the framework does not know |
| `status` | `"running"` or `"stopped"` | tmux session exists (workers: always `"running"`, meaning enrolled) |
| `attention` | str or null | for a local running cousin, the first of `config/harness.toml attention_patterns` found in the pane's last 20 lines (the same capture `active` hashes), else null. "running" says a session exists, not that the agent works: a pane parked on the harness's login menu is flagged, and the card shows "needs attention" |
| `chat` | `"ok"`, `"down"` or `"none"` | `/health` reachable, not reachable, no port |
| `active` | bool | the pane's last 20 lines changed within 60 s (capture-pane hash) |
| `pid` | int or null | the agent process in the session: `tmux display-message -p -t =<session> '#{pane_pid}'` through the console's binary and socket, only for a local running cousin; null otherwise or when tmux prints nothing usable |
| `uptime_seconds` | int or null | the age of `pid` from `/proc/<pid>/stat` against `/proc/uptime`, else `ps -o etimes=`; null when `pid` is null or the process cannot be aged (never 0, which would read as "just started") |
| `activity` | str | first 200 chars of `data/last-activity.txt`, else `""` |
| `lastMsgTs` | int | unix time of the newest message of type `<slug>` in the operator's thread (proxied history, newest 20 rows), 0 when no operator, no port, or the server is down |
| `tokensSpent` | int | today's total from the tokens seam, 0 when unavailable |

Rows the source carried and this one does not: `main_tenant`,
`framework_managed`, `livenessTick`, `tokenBudget`, `auto_start`,
`cpu`, `mem`, `chatCount`, `lastTick`. The first four are
install-specific or dead keys; the rest were zeros or platform probes
the views only rendered as decoration. `model` and `effort` were
dropped with them at first and came back once the agent command grew
its `{model}` / `{effort}` placeholders: they are now `cousin.toml`
facts with an install-wide fallback, not vendor keys. The source's
`pid` (the chat server's, found by port) and `uptime` (0 when unknown)
came back as `pid` (the agent's, asked of tmux) and `uptime_seconds`
(null when unknown).

### `POST /api/cousins` (spawn)
Body: `{"slug": str, "name": str, "role": str, "voice": str,
"role_paragraph": str?, "port": int?, "operator": str?, "model":
str?, "effort": str?, "heartbeat": int?, "memory_scope": str?}`.
Calls `spawn.create_cousin`; `voice` is required because the template
refuses to render without it (`docs/spawn-and-template-spec.md`: an
unfilled voice is a spawn failure). The four optional runtime fields
are the ones `cousin-spawn --model / --effort / --heartbeat /
--memory-scope` take and land in `cousin.toml` as `[runtime] model`
and `effort`, `[heartbeat] context_beat_seconds` and `[memory] scope`;
an empty or absent field writes no key (the documented default
applies). `201 {"ok": true, "slug": str, "home": str, "port": int}`.
`400` on validation (`SpawnError` text in `error`: a bad effort or
scope, a non-positive heartbeat, a model name shlex would split),
`409` when the slug exists or an orphan directory squats it. Creating
and starting stay separate: the spawn modal follows a 201 with `POST
/api/cousins/<slug>/start`, as the source did. There is no
`token_budget` field (a dead key), and the agent binary itself stays
host configuration (`config/agent-cmd`): the model and effort only
fill that command's placeholders.

### `GET /api/spawn/options`
What the spawn dialog offers and preselects: `{"models": [str],
"default_model": str or null, "efforts": ["low", "medium", "high",
"max"], "default_effort": str, "memory_scopes": ["private", "shared",
"both"], "default_memory_scope": "private", "default_heartbeat":
3600, "heartbeat_bounds": [60, 2592000], "operator_max_chars": 64}`.
The bounds and the length are the ones the identity routes below
enforce, so the inspector's editors check the same limits. `models` and the two defaults read `config/harness.toml
[agent]` (`models`, `default_model`, `default_effort`); with no
`models` the built-in catalogue of three names is offered, with no
`default_model` the first catalogue entry is preselected, with no
`default_effort` `high` is. The heartbeat and scope defaults are the
`cousin.toml` defaults (`docs/configuration.md`). `500` with the
reason when `harness.toml` exists and cannot be read.

### `DELETE /api/cousins/<slug>` (dismiss)
Stops the cousin, archives the whole `cousins/<slug>/` tree to
`<root>/data/dismissed/<slug>-<YYYYmmdd-HHMMSS>.tar.gz`, then removes
the tree. **A failed archive refuses the delete** (`500`, `error`
starts with `refusing to delete`, the home is kept); an archive
directory inside the tree being deleted is refused the same way. `200
{"ok": true, "slug": str, "status": "deleted", "archive": str,
"left_in_place": [str]}` where `left_in_place` names harness-side
directories (`transcripts_dir`, `auto_memory_dir` from
`config/harness.toml`, when configured) the console did not touch:
the archive holds the home, and deleting a harness's own files is the
operator's act. `404` unknown cousin. Unlike the source there is no
"main tenant" refusal: no cousin is special to the console.

### `POST /api/cousins/<slug>/start`
Calls `spawn.start_cousin(home, agent_cmd=config/agent-cmd, ...)`,
the single tmux-creation site. `200 {"ok": true, "slug": str,
"status": "started" | "already running", "chat_server": "started" |
"reused"}`. `404` unknown, `500` with the reason when `config/agent-cmd`
is absent or tmux fails (the path is named in `error`).

### `POST /api/cousins/<slug>/stop`
Kills the tmux session and stops the chat server (`spawn.stop_cousin`,
added by the backend task: the pid the spawn wrote, else the process
bound to the cousin's port on this host). `200 {"ok": true, "slug":
str, "status": "stopped", "tmux": "stopped" | "already stopped",
"chat_server": "stopped" | "not running"}`. Idempotent.

### `POST /api/cousins/<slug>/restart`
Stop, a short settle, start. `200 {"ok": bool, "target":
"cousin/<slug>", "stop": {...}, "start": {...}}` with the two inner
results; the code is the start's.

### `POST /api/cousins/<slug>/role`
Body `{"role": str}` (at most 5000 chars, `400` otherwise). Rewrites
`[cousin] role` in `cousin.toml` through the same atomic re-parsed
write the rest of the framework uses (the source rewrote the file by
line matching). `200 {"ok": true, "slug": str, "role": str}`.

### `GET /api/cousins/<slug>/claude-md`
`{"ok": true, "slug": str, "content": str, "bytes": int, "path": str,
"missing": bool?}`; a missing file is `content: ""` with
`missing: true`, not an error.

### `POST /api/cousins/<slug>/claude-md`
Body `{"content": str}` (a string, at most 200000 chars). Writes a
timestamped backup of the previous file to
`<home>/data/claude-md-backups/CLAUDE-<unix>.md` first, then replaces
the file. `200 {"ok": true, "slug": str, "bytes": int}`. The console
edits an existing CLAUDE.md; it never generates one (the template is
the single identity source).

### `POST /api/cousins/<slug>/effort`
Body `{"effort": "low" | "medium" | "high" | "max"}`. Persists
`[runtime] effort` in `cousin.toml` through `spawn.persist_runtime`
(the same targeted, re-parsed, atomically renamed write the session
id uses). `200 {"ok": true, "slug": str, "effort": str,
"restart_required": true}`: the value renders into the agent command
at the next start, and the running agent keeps the one it started
with, so the client shows a restart hint rather than pretending the
change is live. `400` for any other value or a non-string, `404`
unknown cousin. The source injected an in-session command into the
pane as well; that was vendor-specific and is not ported.

### `POST /api/cousins/<slug>/model`
Body `{"model": str}`, one word of letters, digits and `._:/+-` (it is
rendered into an argv). Persists `[runtime] model` the same way. `200
{"ok": true, "slug": str, "model": str, "restart_required": true}`.
`400` empty, non-string or splittable, `404` unknown cousin.

### `POST /api/cousins/<slug>/operator`, `POST /api/cousins/<slug>/memory-scope`, `POST /api/cousins/<slug>/heartbeat`
The inspector's identity editors. Bodies `{"operator": str}`,
`{"memory_scope": "private" | "shared" | "both"}` and
`{"heartbeat": int}`; each is validated by
`spawn.check_identity_value` and written by `spawn.persist_identity`
into `[operator] name`, `[memory] scope` and `[heartbeat]
context_beat_seconds` with the targeted, re-parsed, atomically
renamed edit that keeps comments and every other table. The operator
is one non-empty line of at most 64 characters, no leading or trailing
whitespace, no control characters (clearing it is not offered here);
the heartbeat is whole seconds from 60 to 2592000 (thirty days). `200
{"ok": true, "slug": str, <key>: value, "restart_required": bool}`
and a `cousins-refresh` event. `restart_required` is true for the
operator only: the chat server loads `cousin.toml` once at its start
and uses the operator to tell operator messages from peers (a cousin
restart stops and starts it). The scope is read on each shared-tier
call and the loops daemon loads every `cousin.toml` on each tick, so
those two apply without a restart. `400` for any other value (the
file is left untouched), `404` unknown cousin.

### `POST /api/cousins/<slug>/hidden`
Body `{"hidden": bool}`. Sets or removes `[cousin] hidden` in
`cousin.toml` (false is the default and is not written). `200
{"ok": true, "slug": str, "hidden": bool}`. Hidden cousins are
filtered out of the sidebar and views unless the "show hidden"
setting is on; the flag has no other consumer.

### `GET /api/cousins/<slug>/auth`
`200 {"ok": true, "slug": str, "mode": str, "modes": [str], "default":
str, "configured": bool, "key": {"set": bool, "last4": str|null,
"error": str|null}, "key_env": str}` (`key_env` only when configured).
`modes` is the catalogue the inspector offers; the names live in
`cousin_lib/agent_auth.py` and nowhere else. `configured` is whether
`config/harness.toml [auth.api_key]` exists. `key` says whether the
cousin's `.secrets/api-key.env` is usable, its last four characters
when the key is at least 16 long, and why it is not usable; the key
itself is never in any response.

### `POST /api/cousins/<slug>/auth/key`
Body `{"key": str}`: a bare key or a `<key_env>=<key>` line. Writes the
key file (directory 0700, file 0600, renamed into place). `200` with
the `GET` body above, `400` for an unusable key or no `[auth.api_key]`;
the body never repeats the key.

### `POST /api/cousins/<slug>/auth`
Body `{"mode": str, "force": bool = false, "restart": bool = true}`.
`agent_auth.switch`: for the key mode, the key file is checked and the
isolated harness config dir rebuilt; a running agent restarts on the
same session (`[agent.resume]`). `200 {"ok": true, "mode", "previous",
"running", "restarted", "session_id"?, "auth": <the GET body>}`; `409
{"busy": true}` when the pane matches `busy_patterns` and `force` is
false; `400` for an unknown mode, an unusable key file, an isolated
dir holding a login, or a restart that cannot resume. A refusal
changes nothing.

### `POST /api/cousins/<slug>/peer`
Body `{"to": str, "text": str}`. Delivers the text to the destination
cousin the way `cousin-chat send` does: a proxied `POST /api/send`
on the destination's chat server with `user` = the source cousin's
display name, so it arrives as a `(Chat <Name>):` line and lands in
the destination's history. `200 {"ok": true, "to": str, "id": int}`.
`400` empty text or `to == slug`, `404` either cousin unknown, `502`
destination unreachable. The source injected straight into tmux and
wrote the destination's database itself; both were second
implementations of single-sited mechanisms and are not ported.

### `GET /api/cousins/<slug>/flip`
The state of the last flip the console ran plus any pending timed
flip: `{"ok": true, "status": "idle" | "running" | "done" | "failed"
| "stale_marker", "started_at": float?, "stages": [...]?, "result":
{...}?, "recovery": {...}?, "pending": {"request_id": int, "fire_at":
float, "seconds_until_fire": int}?}`. `stages` and `result` are the
structured result `flip.flip` returns (`docs/lifecycle-spec.md`);
`stale_marker` reports `flip.recover_in_progress` when a marker from
a crashed flip exists and no flip is running; `pending` reads the
loops daemon's request store (`kind = "flip"`, status pending) so a
flip scheduled by any client, or by the transcript-size guard, shows
here.

### `POST /api/cousins/<slug>/flip`
Body `{"confirm": bool?, "delay_seconds": int?}`.

- Without `delay_seconds` (or 0): runs `flip.flip(slug,
  confirm=confirm)` on a background thread and returns `202
  {"ok": true, "slug": str, "status": "running", "started_at":
  float}` immediately; progress arrives as `cousin-flip` events on
  `/api/events` and on the GET above. `409` when a flip is already
  running for that cousin.
- With `delay_seconds > 0`: submits a timed-flip request through
  `loops.submit_request("flip", cousin=slug, payload={"fire_at":
  now + delay, "reason": "console"})` and returns `202 {"ok": true,
  "slug": str, "request_id": int, "fire_at": float, "delay_seconds":
  int}`. The daemon carries the T-5m/T-1m/T-30s warning ladder and
  fires it (`docs/loops-spec.md`); a second request while one is
  pending is `409`. `400` when `delay_seconds` is not an integer.

### `POST /api/cousins/<slug>/flip/cancel`
Marks that cousin's pending timed-flip request `cancelled` in the
request store (a `loops.cancel_request(id)` the backend task adds; a
cancelled row is consumed by nobody and reported like any other
terminal status). `200 {"ok": true, "slug": str, "was_pending":
bool}`. A flip already running cannot be cancelled: `409`.

## Chat: a proxy over each cousin's chat server

Every chat route names the cousin (`cousin` query or body field),
resolves its `host`/`port` from the registry, and forwards to the
routes in `docs/chat-server-spec.md`. The console stores no message.
`404` unknown cousin, `502 {"ok": false, "error": str}` when the
server is unreachable or answers non-JSON; an upstream `400` passes
through with its body.

### `GET /api/messages?cousin=<slug>&user=<name>&limit=N&archived=0|1|all&before=N`
Forwards to `/api/history` with the same parameters (`limit` default
200; `since` is passed through when given). `user` is required
(`400`): there is no default operator. Response is the server's
`{"messages": [...], "total": int, "has_more": bool}` plus
`"cousin": str`. Each message is the server's row (`id`, `chat_user`,
`user`, `message`, `timestamp`, `type`, `archived`, `reply_to`,
`reply_to_user`, `reactions: [{user, emoji, tap_count}]`), and the
console adds `"attachment": {"url": str, "kind": "image" | "video" | "audio"}`:
`/api/chat/inbound/<slug>/<id>.<ext>` (kind `image`) for any message
whose id has a file under `<home>/chat/inbound/` (one directory
listing per request), else `/api/chat/media/<slug>/<folder>/<file>`
when the row's `attachment_path` names an existing file directly
inside `<home>/chat/images/`, `chat/audio/` or `chat/video/` with a
suffix that folder serves. `kind` is the row's `attachment_kind` in
display terms (a stored `voice` is `audio`), else the folder's. A
projection, not a store.

### `GET /api/search?cousin=<slug>&q=<text>&user=<name>&archived=0|1|all`
Forwards to `/api/search`. Response is the server's `{"messages":
[...]}` (newest first, capped at 50) plus `"cousin": str`, attachments
annotated as above. The source view read a `results` key; the ported
view reads `messages`. The source's `has=<media kind>` filter is not
ported.

### `POST /api/chat/send`
Body `{"cousin": str, "user": str, "message": str, "image": "data:..."?,
"reply_to": {...}?}`. `cousin` and `user` are required (`400`).
Forwards `{user, message, image?, reply_to?}` to `/api/send` with a
15 s timeout (an image can be megabytes). Response is the server's
`{"ok": true, "id": int, "timestamp": str}`. `audio` is not ported.

### `POST /api/chat/archive`
Body `{"cousin": str, "user": str, "keep": int}` (`keep` default 0).
Forwards to `/api/archive`; response is the server's `{"ok": true,
"archived": int}`. The source added a `remaining` count; the ported
view shows what the server reports.

### `POST /api/chat/reactions`
Body `{"cousin": str, "message_id": int, "user": str, "emoji": str,
"action": "tap" | "remove"?}`; `action` defaults to `"tap"`. Forwards
to the server's `POST /api/reactions` (the source server's route was
named differently; the console targets the public one). Response is
the server's `{"message_id": int, "op": "added" | "bumped" |
"removed", "reactions": [{user, emoji, tap_count}]}`.

### `GET /api/chat/inbound/<slug>/<file>`
Serves one inbound attachment from `<home>/chat/inbound/`. The file
name must be `<message id>.<ext>` with an extension in the image
allowlist; the resolved path must sit strictly inside that directory;
anything else is `404`. Content type from the extension,
`Cache-Control: private, max-age=3600`. Read-only.

### `GET /api/chat/media/<slug>/<folder>/<file>`
Serves one generated asset (docs/media-spec.md, "Storage") from
`<home>/chat/<folder>/`, `<folder>` one of `images`, `audio`, `video`.
The file name must start with a letter or digit and use only letters,
digits, `.`, `_` and `-`; its suffix must be in that folder's
allowlist (images: png, jpg, jpeg, gif, webp; audio: mp3, ogg, oga,
opus, wav, m4a, webm; video: mp4, webm, mov, m4v); the resolved path
must sit strictly inside the folder; anything else is `404`. A single
`Range: bytes=` header answers `206` with `Content-Range` (a browser
seeks a video this way), an unsatisfiable one `416`; otherwise `200`
with `Accept-Ranges: bytes`. `Cache-Control: private, max-age=3600`.
With the inbox route above, these are the only files the console
serves from a cousin home, all read-only.

### The chat media viewer
The chat view renders an attachment by its `kind` (from the file
extension when a row carries none): an image as a bounded thumbnail,
a video as a muted looping inline preview that plays only while on
screen, audio as a native player. A header toggle, "media on" /
"media off", hides every attachment behind a one-line placeholder
(`[image hidden]`, `[video hidden]`, `[audio hidden]`); the choice is
a browser preference under the `localStorage` key `fw_chat_media`
(`"0"` hides; absent or anything else shows). Clicking an image or a
video opens the viewer over the page: the full-size image or the video
with controls and sound, the thread's images and videos in order with
prev/next (arrow keys and on-screen buttons), a `n / total` counter
and an open-original link; Escape or a click outside closes it. It
calls no route beyond the two file routes above.

## The pane

The pane is the cousin's tmux session rendered in the browser and
typed into. All four routes resolve the session from the registry
(`tmuxSession`) and the binary and socket from the console's flags,
the same resolution the injection module uses; for a cousin with
`host` set they run the same commands over `ssh <host>` with the
remote user's default socket. `404` unknown cousin, `400` when the
cousin has no tmux session configured, `409` when the session is not
running.

### `GET /api/pane?cousin=<slug>&lines=N`
`{"text": str}`: `capture-pane -p -e -S -<lines>` with escapes kept
and trailing blank lines trimmed. `lines` default 200.

### `GET /api/pane/stream?cousin=<slug>&lines=N`
An SSE stream (`text/event-stream`, `Cache-Control: no-cache`,
`X-Accel-Buffering: no`). Events, each with a JSON `data` line:

| event | payload | when |
|---|---|---|
| `pane` | `{"text": str, "state": object or null, "ts": iso, "changed": true}` | once on connect (a full frame), and again whenever the frame must be resent (after a geometry change; every change when the server is polling). See "Frames" below |
| `delta` | `{"text": str, "ts": iso}` | new bytes since the last event, decoded with a boundary-safe UTF-8 decoder (an incomplete multi-byte character waits for its rest) |
| `geom` | `{"cols": int, "rows": int, "ts": iso}` | the pane's geometry changed (checked at least every 2 s, during bursts too); a fresh `pane` frame follows |
| `heartbeat` | `{"ts": iso}` | after 3 s without output |
| `: tick` | comment frame | keep-alive between polls |

#### Frames

A `pane` frame is written into a freshly reset terminal. `text` is
`capture-pane -p -e -S -<lines>` with trailing blank lines trimmed
(never above the cursor's line), followed by a control tail: `ESC [
? 1000 h ESC [ ? 1006 h` when the program tracks the mouse with SGR
reports, then the cursor moved onto tmux's cursor cell relative to
the frame's last line (`ESC [ n A` up, `ESC [ col G` to the column),
then `ESC [ ? 25 h` or `ESC [ ? 25 l` as tmux shows or hides the
cursor. `state` is `{"alt", "mouse", "sgr", "cols", "rows", "cx",
"cy", "cursor", "top"}` from one `display-message` (`alternate_on`,
`mouse_any_flag`, `mouse_sgr_flag`, `pane_width`, `pane_height`,
`cursor_x`, `cursor_y`, `cursor_flag`; `top` is the number of frame
lines above the screen's first row), null when tmux prints nothing
usable. A full-screen program runs on the alternate screen, which has
no history to capture: the browser scrolls it by sending the
program's own wheel reports, which a terminal only produces while in
mouse mode, and a capture never contains the program's mode-setting
escapes, hence the tail. While the program does not track the mouse,
a wheel scrolls the client's own scrollback and sends nothing.

How the server obtains bytes is an implementation choice this contract
does not fix: a `pipe-pane` tap (the source's local path) or
`capture-pane` polling at 0.5 s (the source's remote path, which
emits only `pane` frames). A client must accept either mix. The
stream ends when the client disconnects; a tap, if one was started,
is stopped and its file removed.

### `POST /api/pane/input`
Body `{"cousin": str, "data": str}`: the raw bytes a terminal emulator
produced. The server maps them to tmux `send-keys`: named keys for
`Enter`, `Tab`, `BSpace`, `Escape`, `Up/Down/Left/Right`, `Home/End`,
`PageUp/PageDown`, `DC/IC`, `F1..F4`, `C-a..C-z`; SGR mouse reports
(`ESC [ < ... M|m`) forwarded literally, and only while the pane's
program tracks the mouse with SGR reports (to any other program a
report is an Escape and text, which a modal editor reads as leaving
insert mode); every other CSI or SS3 sequence dropped, never forwarded as
Escape; printable runs sent with `-l --` in chunks under tmux's
message ceiling (the `-l` and `--` are load-bearing: a line starting
with `-` is otherwise parsed as flags). All sends take the
process-wide injection lock so a chat delivery and a keystroke never
interleave. `200 {"ok": true, "tokens": int}`; `500` with tmux's
stderr on failure.

### `POST /api/pane/resize`
Body `{"cousin": str, "cols": int, "rows": int}`; clamped to 20..400
and 5..200, `400` when not integers. `resize-window` on window 0.
`200 {"ok": true, "cols": int, "rows": int}`.

## Jobs

A projection of `cousin_lib.jobs` (`<root>/data/jobs.db`). Row keys
are the public schema: `id`, `spawned_by`, `kind`, `title`,
`description`, `status` (`running`, `done`, `failed`, `cancelled`),
`started_at`, `finished_at`, `exit_code`, `result_summary`,
`log_path`, `pid`, `command`. The source named the end column
`ended_at`; the ported view reads `finished_at`.

### `GET /api/jobs?status=&spawned_by=&kind=&active_only=0|1&since_hours=N&limit=N`
`{"jobs": [row]}`, running first then newest. `limit` default 200.
`kind` and `since_hours` are filters `jobs.list_jobs` gains in the
backend task. Every list call also runs the rate-limited reaper (at
most once per 5 minutes): rows `running` for more than 24 hours are
marked `failed` with an auto-reap note, and the table is capped at
1000 rows, oldest finished rows and their console-minted logs
rotating out. Both are maintenance the store's owner accepts from any
reader; a failure there is logged, never a 500 on the poll.

### `GET /api/jobs/<id>`
`{"ok": true, "job": row}` or `404`.

### `GET /api/jobs/<id>/log?lines=N&from=BYTE`
`{"ok": true, "log": str, "log_path": str, "size": int}`. Without
`from`: the last `lines` (default 40) of the final 64 KB. With `from`:
the bytes from that offset, at most 64 KB, so a follower prints each
line once. A job without a log path returns `log: ""`; a path not yet
present returns a placeholder line. `404` unknown job.

### `POST /api/jobs/<id>`
Body: any of `status`, `result_summary`, `exit_code`, `title`,
`description`. `status: "cancelled"` on a running job with a known
pid sends SIGTERM before the row changes; a terminal status sets
`finished_at`. `200 {"ok": true, "job": row}`, `400` no fields or a
status outside the set, `404`.

### `DELETE /api/jobs/<id>`
Removes the row and its log file only when the path is under the
console-minted log directory. `200 {"ok": true}`. (The source
overloaded POST with a `_delete` flag; state changes here are POST or
DELETE, so it is a DELETE.)

Not ported: `POST /api/jobs` (creation). Cousins register jobs through
`cousin-job`, which writes the database directly; nothing registers a
job through the console (the reverse-dependency rule).

## Loops

Loop state is the loops daemon's (`docs/loops-spec.md`): `[[loops]]`
in each `cousin.toml`, `data/loops-state.json` (`last_tick`,
`last_beat` by slug, `last_fires` keyed `"<slug>|<name>"`) and the
request store. The console reads them and submits requests; it never
fires anything.

Every loops response carries `"daemon": {"ok": bool, "last_tick":
float?, "message": str}` from `loops.daemon_status()`, and the views
show the message whenever `ok` is false ("loops daemon has never run",
"loops daemon down (last tick NNs ago)").

### `GET /api/loops`
`{"loops": [row], "daemon": {...}}`. One row per `[[loops]]` entry of
every cousin plus one synthetic `context-heartbeat` row per live
non-worker cousin:

| key | type | meaning |
|---|---|---|
| `cousin`, `name` | str | |
| `state` | `"healthy"`, `"idle"`, `"disabled"`, `"failed"` | fired at least once; never fired; `enabled` false; a worker loop whose last job row is `failed` |
| `interval` | int | `interval_seconds` (0 for daily/cron) |
| `schedule` | `{"interval_seconds": int, "daily_at": str, "cron": str, "days": [str]}` | the schedule form as configured |
| `prompt`, `enabled`, `hidden` | str, bool, bool | |
| `lastFireTs` | int | unix time of the last fire, 0 if never |
| `lastTick` | int | seconds since the last fire, 0 if never |
| `nextFireTs` | int | the next due time computed from the schedule and the last fire with the daemon's own due logic; 0 when disabled |
| `drift` | int | seconds the last fire ran late against its due time (0 when unknown) |
| `note` | str | the first 60 chars of the prompt |
| `source` | `"framework"` | always; the source also emitted `"systemd"` rows for install timers, not ported |

Malformed `cousin.toml` files are reported, not skipped: an
`"errors": [str]` key names each cousin whose loops could not be read.

### `GET /api/loops/recent`
`{"fires": [{"cousin": str, "loop": str, "ago": int}], "daemon":
{...}}`, the 20 most recent fires newest first, from `last_beat`
(as `context-heartbeat`, workers excluded) and `last_fires`.

### `GET /api/loops/drift/<slug>/<name>`
`{"ok": true, "slug": str, "name": str, "interval": int, "n": int,
"points": [{"t": int, "interval": int, "drift": int}]}`: consecutive
fire-to-fire intervals and their drift against the configured
interval, at most the last 80. The series is read from the daemon's
append-only fire log, `<root>/data/loops-fires.jsonl` (one
`{"ts": float, "cousin": str, "loop": str}` line per delivered fire,
written by the tick at the same point it commits `last_fires`; the
backend task adds the writer to `loops.py`). Absent the file, or with
fewer than two fires, `n` is 0 and the modal says so. `400` bad slug
or name.

### `GET /api/cousins/<slug>/loops`
`{"loops": [entry], "last_beat": int, "last_fires": {"<name>": float},
"daemon": {...}}` where `entry` is a `[[loops]]` table as
`loops.load_cousin_loops` returns it (`name`, one schedule form,
`prompt`, `enabled`, `hidden`).

### `POST /api/cousins/<slug>/loops`
Body `{"loops": [entry]}`: replaces the whole `[[loops]]` array. Each
entry is validated as the loops spec validates it (`name` pattern and
uniqueness, exactly one schedule form, non-empty `prompt`,
`enabled` truthiness, `days` a list of three-letter weekdays), `400`
naming the offending index otherwise. Written atomically with a
re-parse. `200 {"ok": true, "slug": str, "loops": [entry]}`. The
daemon reads the file on its next tick; no request row is needed for
an edit.

### `POST /api/cousins/<slug>/loops/<name>/hidden`
Body `{"hidden": bool}`; round-trips through the save above. `200
{"ok": true, "slug": str, "loops": [...]}`, `404` unknown loop.

### `POST /api/cousins/<slug>/loops/<name>/fire`
Submits `loops.submit_request("fire", cousin=slug, payload={"loop":
name})` (`context-heartbeat` is accepted: the daemon delivers its
beat composition for that name). `202 {"ok": true, "slug": str,
"name": str, "request_id": int}`. The row's status is visible in
`cousin-loops requests` and expires loudly if the daemon is down;
the view's toast says "fire requested", not "fired".

## Memory and the shared-tier review

### `GET /api/memory`
`{"tree": {"shared/": {<file>: entry}, "<slug>/": {<file>: entry}}}`
where `entry` is `{"size": int, "updated": int, "preview": str}`
(bytes, seconds since modification, the first 400 characters, or a
marker when unreadable). `shared/` lists `<root>/shared/*.md`; each
cousin key lists `<home>/memory/*.md` (the first 50, sorted). The
source also looked into a harness-specific directory for one cousin;
here a cousin without `memory/` lists empty. Read-only.

### `GET /api/shared/list`
`{"canonical": [{"name": str, "size": int, "mtime": int, "sha": str}],
"pending": [{"name": str, "slug": str, "origin": str, "size": int,
"mtime": int, "sha": str}]}`: `shared_tier.list_shared()` names,
stat'ed, `sha` the first 12 hex chars of SHA-256; a pending name
`<slug>__<stem>.md` yields `slug` and `origin = <stem>.md`.

### `GET /api/shared/content?scope=canonical|pending&name=<file>`
`{"content": str}`; `400` without `name`, `404` when the resolved path
is not a file strictly inside the scope's directory.

### `GET /api/shared/diff?file=<canonical name>&slug=<proposer>`
`{"diff": str}` from `shared_tier.diff_proposal`; `400` missing
parameters, `404` no such proposal.

### `GET /api/shared/audit?n=N`
`{"entries": [entry]}`, the last `n` (default 100) lines of
`<root>/shared/audit.jsonl` newest first; an entry carries `ts`,
`kind` (`propose`, `promote`, `reject`, ...), `actor`, `file`, and
`proposer` or `reason` when present.

### `POST /api/shared/approve` and `POST /api/shared/reject`
Body `{"slug": str, "file": str, "reason": str?, "by": str?}`. Calls
`shared_tier.promote(file, proposer=slug, by=<reviewer>)` or
`shared_tier.reject(...)`. The reviewer is the session user; when
auth is not configured the body must carry `by` (`400` otherwise). A
reviewer the tier refuses (not in `config/shared-reviewers.json`, or
the proposer reviewing themselves) is `403` with the tier's message;
a missing proposal `404`. `200 {"ok": true, "file": str}`. The
source's git commit of the shared directory is not ported: the audit
log is the record.

## Tokens

Token counts come from the harness transcripts, the one place usage
is recorded, through the seam `config/harness.toml` already defines
(`transcripts_dir`; see `docs/configuration.md`). For each cousin
with a persisted session id, the console scans
`<transcripts_dir>/<session_id>.jsonl` incrementally (byte offset
remembered per process) and sums, per calendar day, each message's
`usage` block: `input_tokens`, `output_tokens`, `cache_read_input_tokens`
and the cache-creation counts. The field names are the seam's
contract; a transcript without them contributes zero.

### `GET /api/tokens`
`{"available": bool, "reason": str?, "cousins": [{"slug": str,
"name": str, "series": [{"day": "YYYY-MM-DD", "total": int,
"output": int}]}]}`, the last 14 days per cousin. When
`config/harness.toml` is absent or has no `transcripts_dir`,
`available` is false, `reason` says which, and `cousins` is empty;
the tokens view renders that sentence instead of zeros. There is no
budget, burn-rate guess or fake sparkline: the source's synthetic
series are removed with the budget key.

## Tracker

The framework-wide in-flight work tracker (`cousin_lib/tracker.py`,
contract in `docs/tracker-spec.md`; the console serves the library's
shapes verbatim). Items live in `<root>/data/tracker.db`; an item is
`{"id": int, "title": str, "domain": str, "state": str, "tags": [str],
"owner": str, "notes": str, "created_at": iso, "updated_at": iso}` with
`state` in `open`, `active`, `blocked`, `done`, `dropped`. Ids never
recycle.

- `GET /api/tracker?owner=&state=&domain=&tag=` returns `{"items":
  [item]}`, open items first, then by `updated_at` descending.
- `POST /api/tracker` with `{"title": str, "domain"?, "state"?, "tags"?,
  "owner"?, "notes"?}` (`title` required, `state` default `open`) returns
  `200 {"item": item}`; `400` on a blank title or a state outside the set.
- `POST /api/tracker/<id>` with any subset of the fields (state included,
  validated) returns `{"item": item}`; `400` no fields or bad state,
  `404` unknown id.
- `DELETE /api/tracker/<id>` returns `{"ok": true, "deleted": id}`;
  `404` unknown id.

## Host, restart

### `GET /api/version`
Public (no session needed, like `GET /api/auth/me`). `200 {"version":
str, "commit": str|null}`: the framework version from the checkout's
`pyproject.toml` (else the installed distribution's metadata) and the
checkout's short git commit (null outside a git checkout), both read
once when the console starts. The top bar shows them dim beside the
brand, so a console that was not restarted after a bump shows the old
values.

### `GET /api/host`
`{"host": str, "kernel": str, "uptime": int, "cpu": {"pct": float,
"load1": float, "load5": float, "load15": float}, "mem": {"total":
float, "used": float, "cached": float}, "disk": {"total": float,
"used": float}, "net": {"rx": float, "tx": float, "rx_total_gb":
float, "tx_total_gb": float}, "console_uptime": int}`. `host` is the
running machine's hostname at request time (never a literal), memory
in GB from `MemTotal` minus `MemAvailable`, disk for `/`, network as
MB/s since the previous call. Each block degrades to zeros on a
platform without the `/proc` files.

### `POST /api/admin/restart/framework`
Restarts the console itself. The console answers `200 {"ok": true,
"target": "console", "supervised": bool, "eta_seconds": 4}` and then
exits with status 0 from a detached timer 0.6 s later, after the
response has flushed. `supervised` is true when the process runs
under a supervisor that restarts it (a systemd unit with
`Restart=always`, detected by the `INVOCATION_ID` environment
variable); when false the response says so and the view warns that
"restart" will be "stop". The console never invokes a service manager
by name: which one owns it is the install's business
(`docs/operations.md`). The shell polls `GET /api/cousins` until it
answers again.

## `GET /api/events`: the live stream

An SSE stream every dashboard tab holds. Frames are `data: <json>`
with `{"kind": str, "data": ...}`; a comment frame `: ping` every 25 s
keeps proxies from closing an idle stream. On connect the first frame
is a snapshot; after it, deltas. The events are projections of
durable state produced by a poller inside the console that diffs the
stores (every 2 s for jobs and the request store, 15 s for the fleet
and loops) and by the console's own command handlers at the moment
they act; a dropped connection costs a reconnect and a fresh
snapshot, never data, and the client backs off from 1 s to 15 s.

| kind | data | produced when |
|---|---|---|
| `snapshot` | `{"cousins": [row], "loops": [row], "jobs": [row], "daemon": {...}}` | on connect (the same rows the GET views return, `model` and `effort` included even in the bare registry rows served before the fleet routes are wired; the source also sent `agents`, dropped) |
| `cousins-refresh` | `[row]` | the fleet poll finished (drives the unread dot via `lastMsgTs`) |
| `loops-refresh` | `[row]` | the loops poll finished |
| `cousin-status` | `{"slug": str, "status": "starting" | "stopping"}` | a start/stop/restart handler begins; the next refresh confirms |
| `job-add`, `job-update` | `row` | a job row appeared or changed since the last poll |
| `job-delete` | `{"id": int}` | a row disappeared |
| `cousin-flip` | `{"slug": str, "phase": "scheduled" | "cancelled" | "started" | "complete" | "failed", "fire_at": float?, "delay_seconds": float?, "ok": bool?, "new_generation": int?, "boot_packet_tokens": int?, "degraded_sections": [str]?, "error": str?}` | the flip handlers, and the request-store poll for timed flips the daemon consumed (`done` -> `complete`, `failed`, `expired` -> `failed` with the row's reason) |
| `loop-fire` | `{"cousin": str, "loop": str, "ts": float}` | a `last_fires` entry advanced since the last loops poll |
| `tracker-change` | `{"id": int, "op": "add" | "update" | "delete"}` | a tracker mutation through the console |

The client applies `snapshot`, `cousins-refresh`, `loops-refresh` and
`cousin-status` to its top-level state, re-dispatches `cousin-flip`
as a DOM event the flip modal subscribes to, and leaves the rest to
the views that poll their own routes.

## Static files

`GET /` serves `index.html`; `GET /<file>` serves the committed files
under `cousin_lib/console_static/` for the suffix allowlist `.html
.jsx .js .css .json .webmanifest .svg .png .ico`, with the resolved
path required to sit strictly inside that directory (`403` on
traversal, `404` otherwise) and `Cache-Control: no-store` so a
redeploy is never masked by a browser cache. `index.html` is served
with a `?v=<mtime>` query stamped onto each local `src`/`href` for the
same reason. The page is installable (manifest and icons without
brand marks). Everything behind the CDN tags (React 18, Babel
standalone, marked, mermaid, xterm with its fit addon) is the one
runtime network fetch stated at the top.

## Private literals to genericize

The source static files carry install specifics that must not enter
the port. The gate's denylist scan over those files reports 14 lines
in the retained files (and 4 in the embedded game, which is dropped);
a wider read finds 12 more lines with an operator's first name, other
cousins' names or slugs, a region and a locale that the denylist does
not cover. Each occurrence, by file and line in the source, with its
replacement:

| file:line | what it is | replacement |
|---|---|---|
| `app.jsx:11` | the backlog nav entry | removed (dropped view) |
| `app.jsx:24-40` | the sidebar-groups fetch and save | removed; groups and assignments live in the browser's local storage |
| `app.jsx:136` | a local-storage key built with an operator's first name | built with the chat user (the cousin's `operator` field or the `?user=` parameter) |
| `app.jsx:269-270` | comment lines naming a cousin slug and two operators | reworded generically |
| `app.jsx:275` | the default active cousin is a cousin slug | the first cousin in the fleet, else none |
| `app.jsx:502` | a hard-coded locale for the clock | the browser's locale |
| `app.jsx:528`, `532` | the agents and backlog view mounts | removed |
| `chat.jsx:35` | the default chat user is an operator's first name | `?user=`, else the cousin's `operator`, else the session user; with none the composer is disabled with a one-line reason |
| `chat.jsx:120` | presence heartbeat posts | removed |
| `chat.jsx:248` | the effort selector (levels hard-coded, an in-session vendor command injected into the pane) | kept as a select whose levels come from `GET /api/spawn/options`; it persists through `POST /api/cousins/<slug>/effort` and shows "restart to apply" |
| `chat.jsx:325` | the media-kind filter dropdown (`all` / images / videos / audio) beside it | removed with the media views |
| `chat.jsx:407` | engagement posts | removed |
| `chat.jsx:470` | the `has=` media filter on search | removed |
| `chat.jsx:489` | a comment naming a region and its VPN | reworded |
| `chat.jsx:499` | a comment naming a cousin | reworded |
| `chat.jsx:663` | a comment with a host name and port | reworded |
| `chat.jsx:792` | an English word inside the media lightbox that matches a denylisted slug | removed with the lightbox |
| `chat.jsx:1028` | a comment naming the operator | reworded |
| `chat.jsx:1280`, `1521` | `chatUser` falling back to an operator's first name | `chatUser` only |
| `cousins.jsx:305` | the agent-log fetch inside the inspector | removed |
| `cousins.jsx:358` | the token-budget save | removed (dead key) |
| `cousins.jsx:403` | the peer-message history fetch | removed |
| `cousins.jsx:923-931` | the spawn body with a vendor model catalogue and memory scope | the spawn body above (`voice` added; `model`, `effort`, `heartbeat` and `memory_scope` offered from `GET /api/spawn/options`, never from a list in the file) |
| `cousins.jsx:956`, `957`, `960` | spawn-modal placeholders using a real cousin's name and slug | the repository's fictional example cousin |
| `data.jsx:8` | a vendor model catalogue | removed; the catalogue is `config/harness.toml [agent] models`, served by `GET /api/spawn/options` |
| `data.jsx:17` | the host seed is a host name | `""` (the overview shows what `/api/host` returns) |
| `data.jsx:43-46` | the agents fetch helper | removed |
| `styles.css:1` | a header comment with a host name | reworded |
| `styles.css:30` | a comment naming a cousin | reworded |
| `styles.css:414` | an avatar rule keyed on one cousin's slug | a generic `.msg.cousin` rule (message `type` not `user`) |
| `styles.css:529-531` | three log-colour rules keyed on cousin slugs | removed; colour comes from a hash of the slug |
| `views.jsx:3-133` | the legacy agents view | removed |
| `views.jsx:136` | tag colours keyed on three cousin slugs | the accent colour for the fleet-wide tag; a hue from a hash of the slug for log lines only |
| `views.jsx:934` | a comment naming two cousins | reworded |
| `views.jsx:1118` | a fake token sparkline seeded per one slug | removed with the fake data |
| `views.jsx:1158-1629` | the backlog view and its modal | removed |
| `views.jsx:1960` | the host fallback literal | `h.host || "host"` |
| `index.html` | none | (the title and manifest name are generic) |
| `ui.jsx` | none | |

The frontend task's static-file test scans the ported files with the
gate's denylist and with the patterns above (an RFC1918 address, a
home path, the operator and cousin names an install would recognise),
and fails on any hit.

## Backend behaviours that move

The source backend did more than serve views; each of those roles has
an owner here, and the console does not duplicate it.

- **The scheduler role becomes the loops daemon.** The heartbeat
  loop and its tick, worker firings, one-shot delivery, the
  timed-flip walker with its warning ladder, the in-memory
  `last_beat`/`last_fires` dictionaries and their persisted state
  file, the manual-fire injection, the scheduler watchdog: all of it
  is `cousin-loops` (`docs/loops-spec.md`). The console reads
  `data/loops-state.json` and the request store and submits `fire`
  and `flip` requests; the two flip cases are stated under the flip
  routes (immediate through `flip.flip`, delayed through a request
  row). Auto-starting cousins at boot, the shared-directory watcher
  that git-committed direct edits, the embedding reindex thread, the
  PATH-symlink installer and the pre-warmed caches are not ported:
  they are install operations or artefacts of the source's caching,
  and `docs/operations.md` owns unattended operation.
- **MCP provisioning becomes spawn's.** The source wrote the
  per-cousin MCP registry and `.mcp.json`, pre-approved the harness's
  folder-trust and MCP settings, linked the harness project directory
  into the tree, and generated a CLAUDE.md inline, all from the
  console's create handler. Here `spawn.create_cousin` renders the
  template (the single identity source) and provisions the registry
  and `.mcp.json` (`docs/spawn-and-template-spec.md`,
  `docs/mcp-spec.md`); approval is the operator's act (`cousin-mcp
  approve <slug>`) and the console never edits harness settings.
  Per-cousin harness hooks are `docs/session-hooks.md`.
- **The dismissal archive is the delete path.** Stop, tar the home
  to `data/dismissed/`, refuse the delete when the archive fails,
  then remove: specified under `DELETE /api/cousins/<slug>` above,
  with harness-side directories left in place and named in the
  response instead of removed.
- **Peer messages go through the chat server.** The source's direct
  tmux injection plus a direct row insert into the destination's
  database is replaced by a proxied `/api/send`.
- **Effort and token budget are not ported** (a vendor slash command;
  a dead key), and **the sidebar store moves to the browser**.

## Source routes not ported

For the record, so the list is checked rather than rediscovered:
`/api/agents`, `/api/agents/<cousin>/<id>/log`, `/api/backlog`,
`/api/backlog/<id>`, `/api/chat/presence`, `/api/chat/presence/<slug>`,
`/api/chat/engagement`, `/api/chat/engagement/<slug>`,
`/api/chat/audio/...`, `/api/chat/image/...`, `/api/chat/video/...`,
`/api/cousins/<slug>/budget`,
`/api/peer-messages`, `/api/sidebar`, `/api/liveness` (no view called
it), `/api/network-devices`, the three GPU-box routes,
`/api/admin/restart/cousin/<slug>` (the views use
`/api/cousins/<slug>/restart`), and `POST /api/jobs` (creation).
`/api/logs` was ported and later removed: its only caller was the
inspector's "tail logs" drawer, which read `<home>/data/chat-server.log`
and was retired; that file is still written and is read by the chat
watchdog and by an operator on the host.

### What the plan expected and the source did not contain

Two entries in the port plan's dropped list name surfaces the source
frontend does not have: the GPU box controls exist only as three
backend routes with no view calling them, and the tracker has backend
routes but no view. Both are handled as stated above (routes dropped;
routes kept and the view written fresh). The plan also describes the
memory view as a per-cousin search; the source view is a file tree
with previews plus the shared-tier review, and that is what is
specified, since a search route no view calls is not ported.

## Stated limits

- **Identity is address-asserted below the users file.** With auth
  not configured, the guard is the only boundary, as for the chat
  server; do not expose the console past a network where every
  client is trusted.
- **Best-effort live.** A view can lag its store by its poll
  interval; when the console and a store disagree, the store wins.
- **The pane is a terminal over HTTP, not a PTY.** Keystrokes are
  batched 40 ms client-side and mapped to key names server-side;
  applications relying on exotic escape sequences may not receive
  them, by design (a dropped sequence never becomes a stray Escape).
- **Sessions do not survive a console restart.** Stated under the
  auth model; the cost of owning nothing durable but the users file.
