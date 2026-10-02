# Console API

Every HTTP route the web console serves, for when you want to script against it or debug what the browser is doing. For what the pages do and how to use them, read [the console guide](../console.md).

The console is one Python process (`cousin-console`, default `127.0.0.1:8600`). The browser page is static files plus these routes. The console keeps almost nothing of its own: it reads [cousin](../glossary.md#cousin) homes, the jobs database, the loops state and the tracker on every call, it serves a local cousin's chat from the cousin's own `chat.db`, and it proxies a remote hive node's chat to the node's chat server.

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
- Status codes: `200` done, `201` created, `202` accepted and still running in the background, `400` bad input, `401` no session, `403` refused, `404` unknown thing (or an unknown path), `405` known path with the wrong method, `409` state conflict, `500` local failure with the reason in `error`, `502` an upstream chat server (a hive node's) is unreachable or answered something that isn't JSON, `503` broken users file.
- A `<slug>` has to match `^[a-z][a-z0-9_-]{1,31}$`, otherwise `400 {"error": "bad slug"}` before anything is looked up. A loop `<name>` matches `^[a-z][a-z0-9_-]{0,31}$`.
- A trailing slash on a path is accepted.
- Paths under `/hive/` ([the hive API](hive-api.md)), `/peer/` ([external peers](#external-peers)) and `/plugins/` ([the plugin proxy](#pluginsnamepath-the-proxy)) are handled first. Any other path not under `/api/` is a static file (GET only).

## Auth

### `POST /api/auth/login`

Body `{"user": "ana", "password": "..."}`. Answers `200 {"ok": true, "user": "ana"}` with the `Set-Cookie`. Errors: `400` a field missing, `401 bad credentials`, `409 auth not configured` (no users file). An unknown user costs the same PBKDF2 work as a known one, so the timing doesn't tell you who exists.

### `POST /api/auth/logout`

Drops the session and clears the cookie. `200 {"ok": true}`.

### `GET /api/auth/me`

`{"user": "ana" | null, "configured": bool, "users": [...]}`. `users` lists every configured name, but only when you're logged in. With a broken users file: `{"user": null, "configured": true, "users": [], "error": "..."}`.

### `POST /api/auth/change-password`

Body `{"old_password": "...", "new_password": "..."}`. Needs a session. `200 {"ok": true, "user": "ana"}`. `400` new password under 8 characters, `403 current password incorrect`. The session that changed the password stays logged in; every other session of that user ends.

## Telegram

Per-cousin provisioning of the Telegram bridge ([telegram](../telegram.md)). The bot token is write-only: it is stored at `config/telegram/<slug>.token` (mode 0600) and no answer ever contains it. The bridge process is a `cousin-supervisor` child (`telegram:<slug>`) that follows its cousin's [runner](../glossary.md#runner): it starts with the cousin when `[telegram] enabled` is true and the config is complete, and stops with it. These routes write `cousin.toml` and ask the [supervisor](../glossary.md#supervisor) to rescan, and never start a bridge themselves.

A status: `{"slug", "enabled", "token_set", "operators": [{"user_id", "name"}], "pending": [{"user_id", "username", "first_name", "at"}], "running", "ready": null | "<why the bridge cannot run>"}`. `pending` lists the last five people who wrote to the bot and were refused, so they can be added without looking up a numeric id. Anyone who messages the bot can appear there.

Discovery: while no bridge runs, which is always the case before the first operator is added, the GET (when there are no operators), the token save and the check call `getUpdates` once. The call has no offset, so it confirms nothing, and every refused sender it finds is added to `pending` (`telegram_admin.discover`). This GET therefore calls Telegram and writes `data/telegram-pending.json`. It never runs beside a live bridge.

### `GET /api/cousins/<slug>/telegram`

The status.

### `POST /api/cousins/<slug>/telegram/token`

Body `{"token"}`. Stores it and checks it with Telegram (`getMe`); answers the status plus `check: {"ok", "bot"?, "error"?}`. `400` for something that is not a bot token. A running bridge restarts on the new token; `bridge` says what happened to it, as in the `enabled` answer.

### `POST /api/cousins/<slug>/telegram/operators`

Body `{"operators": [{"user_id", "name"}]}`, the whole list. `400` for a non-numeric or repeated id or a bad name. A running bridge restarts on the new list; `bridge` says what happened to it, as in the `enabled` answer.

### `POST /api/cousins/<slug>/telegram/enabled`

Body `{"enabled": true|false}`. Writes the switch and asks the supervisor to rescan; its rescan starts, stops or restarts the bridge. `bridge` says what happened: `"supervised"` once the supervisor took the rescan, a line saying no supervisor runs (the bridge starts with it), a line saying the supervisor did not answer (the bridge follows its rescan when it does), or why it refused. With no supervisor running, the console stops a bridge itself when the config no longer runs; it never starts one. A cousin with no [runner](../glossary.md#runner) kind gets no bridge: the config is written and `bridge` says why.

### `POST /api/cousins/<slug>/telegram/check`

`getMe` with the stored token: `{"ok", "bot"?, "error"?}`.

## Accounts

The accounts runner cousins run on (`config/accounts.toml`, [configuration](../configuration.md#accountstoml)), and the implicit `host` login. This is the operator's console, so the operator entering a credential here is fine; nothing in the framework obtains one on its own. No answer, log line or event ever carries a key, a token or a sign-in code: a secret file is described as `{"set", "last4", "error"}` only (`last4` for a value of 16 characters or more), as in the auth key box.

An `<name>` matches `^[a-z0-9][a-z0-9_-]{0,31}$`, otherwise `400 bad account name`; `host` is the host's own `~/.claude` login, with no entry.

The routes that write an entry or take, mint or cancel a credential (add, edit, remove, `key`, `login`, `token`, `code`, `cancel`) want a logged-in console user: on a console with no users file they are `403` (add one with `cousin-console adduser <name>`), though the reads stay open. `key`, `login`, `token` and `code` are also `403` when the console itself runs inside a cousin (`COUSIN_HOME` or `COUSIN_SLUG` in its environment or an ancestor's), the guardrail `cousin-account` applies. Who started a login, cancelled one, wrote a key or changed an entry is appended to `data/accounts/audit.jsonl` (`{"ts", "kind": "login-start" | "login-cancel" | "key" | "entry-added" | "entry-edited" | "entry-removed", "actor", "account", "flow"?, "provider"?}`), never a value.

A login is the account's long operation: `console/longop.py` under the key `account:<name>`, one at a time per account, with its stages and `cousin-op` events (slug `account:<name>`, never the URL). Its state is `GET /api/accounts/<name>/op`, the same shape as a cousin's op. A login belongs to the console session that started it: only that session is shown its URL and may post its code or cancel it (`403` for any other). While it runs, the account's edit, remove and `key` are `409`.

### `GET /api/accounts`

`{"ok": true, "error": null | "<why config/accounts.toml cannot be read>", "accounts": [row], "kinds": [...], "fields": {"<kind>": ["<key>", ...]}, "timeout": 600}`. A row: `{"name", "kind", "implicit", "where" (the config dir, secret file or data dir, relative to the root), "lanes" (the runner kinds it runs on, accounts.check_lane), "cousins" (the runner cousins that name it), "entry" (its table in accounts.toml, which holds no secret; null for host), "secret" (`{set, last4, error}` for claude-token and anthropic-key, else null)}`. With a broken file the list is `host` alone. `fields` names the keys each kind's entry takes.

### `GET /api/accounts/<name>/status`

Is it logged in, with no model call: `claude auth status --json` under the account, or for opencode the account's auth.json read (no process). One check per account at a time: `409` while one runs. `{"ok", "name", "kind", "loggedIn", "method", "error", "action"}`: `ok` is logged in with the method the kind wants; `action` (null when ok) is the line that fixes it. opencode adds `providers` (the ones it can run on), `missing` (the ones whose key auth.json lacks; `opencode`, keyless for its free models, never) or `endpoint`. Never the email or organisation the CLI reports. `404` unknown account.

### `POST /api/accounts`

Add an entry. Body `{"name", "entry": {"kind", ...}}`, the entry's keys as in accounts.toml (a list of strings for `providers`, whole numbers for `endpoint_context` and `endpoint_output`; left out means the default). Written by `accounts.write_entry`: every other line of the file kept, the result checked by the same rules the runner reads it by (paths under the root, one of providers or endpoint, no credentials in an endpoint, no provider or endpoint model naming Claude, since Claude cousins run on the Agent SDK only) before the atomic rename. The console keeps an entry's paths where the framework puts them: `secret_file` and `data_dir` under `.secrets/`, `config_dir` under `data/accounts/` (another place is a hand edit of the file), and an `endpoint` whose query or fragment names a credential-like parameter (`key`, `token`, `secret`, `pass`, `auth`, `sig`, `cred`, `session`) is refused. `201 {"ok": true, "account": row}`. `400` refused (the reason), `409` the name exists. An `accounts-change` event follows; the page re-reads the list on it.

### `POST /api/accounts/<name>`

Replace an entry whole. Body `{"entry"}`. Refused (`400`, naming the cousin) when a cousin on the account could not run with the new entry on its [lane](../glossary.md#lane). `200 {"ok": true, "account": row}`, `404` no such entry, `400` for `host`.

### `POST /api/accounts/<name>/remove`

Body `{"confirm": "<name>"}`, the name typed again. `200 {"ok": true, "removed"}`. `400` without the confirmation, `409` while a cousin names the account, `404` no such entry.

### `POST /api/accounts/<name>/key`

A write-only key. Body `{"key"}` for claude-token (a token you minted elsewhere) and anthropic-key: written through `console/secrets.py` to the account's secret file (0600, its directory 0700, read back as the runner reads it), which must be under `.secrets/` (otherwise `400`: write it by hand); `.secrets/` is made, or tightened to, 0700. A list row shows a secret file's last four only when it is under `.secrets/`. `200 {"ok": true, "name", "secret": {set, last4, error}}`. For opencode, body `{"provider", "key"}`: merged into the account's auth.json as an API key (`accounts.store_api_key`); a provider the account does not name (`opencode` included), or one naming Claude or Anthropic, is `400`. `200 {"ok": true, "name", "provider", "status"}` (the status above). `400` for a claude-login account (log it in), a key that is not one line of printable characters, or a local-endpoint account.

### `POST /api/accounts/<name>/login`

Start a login, `202 {"ok": true, "op"}` (`409` while one runs on the account). Body `{"timeout"?}` (seconds, 30 to 1800, default 600).

- claude-login: `claude auth login --claudeai` under the account's config dir (`accounts.login_flow`). The sign-in URL shows at `GET .../flow`; the code the page shows goes to `POST .../code`. `claude auth status`, not the screen, decides. `host` needs `{"confirm_host": true}`: it re-logs `~/.claude`, which every cousin without an account and your own `claude` use.
- opencode: body `{"provider", "method", "timeout"?}`, an OAuth method by its opencode label (`accounts.opencode_login_flow`). The URL and opencode's instruction line show at `GET .../flow`; nothing is pasted back, and the account's auth.json holding the provider decides. A browser method's callback goes to localhost on the console's host, so it completes only there; a headless or device method completes anywhere. A provider or method naming Claude or Anthropic is `400`, as is a missing method (an API key goes to `.../key`).

`400` for another kind (a claude-token account uses `token`).

### `POST /api/accounts/<name>/token`

Mint a long-lived token for a claude-token account: `claude setup-token` in a throwaway config dir (`accounts.token_flow`), the URL and code as for a login; the token is read from the CLI's screen, verified (`claude auth status` reads `oauth_token`) and saved 0600 to the secret file. It is never served. `202 {"ok": true, "op"}`, `400` another kind.

### `GET /api/accounts/<name>/flow`

The running login beside its op: `{"ok", "name", "op", "kind": "login" | "token" | "opencode-login" | null, "provider", "mine", "url", "url_is_https", "instructions", "awaiting_code"}`. `mine` says the login belongs to this session; `url` and `instructions` are set only for that session, while the op runs and once the CLI has shown them; `url_is_https` says whether the page may make it a link (it does only for `https://`). `awaiting_code` is true while the CLI waits for the code.

### `GET /api/accounts/<name>/op`

The account's long operation (a login), running or the last one: `{"ok": true, "op": null | {...}}`, the shape of `GET /api/cousins/<slug>/op`.

### `POST /api/accounts/<name>/code`

The code the sign-in page shows, write-only, from the session that started the login (`403` from another). Body `{"code": "<code>#<state>"}`. `200 {"ok": true, "taken": true}`. It is handed to the waiting CLI in memory, once: the window closes for good after the first code, a timeout or a cancel, and a second or late code is `409` and never delivered. It is never written, logged, echoed or sent as an event, and never goes through a chat. `400` for something that is not the whole `code#state` (the window stays open), `409` when no login waits for a code.

### `POST /api/accounts/<name>/cancel`

End the running login, from the session that started it (`403` from another): the code window closes and the CLI is sent SIGTERM (only while its pty is open). The op fails with `cancelled by the operator`. `200 {"ok": true, "cancelled": true}`, `409` when none runs.

### `POST /api/cousins/<slug>/check-auth`

Body `{"validate"?: bool}`. The cousin's long operation (`202 {"ok": true, "op"}`, kind `check-auth` or `validate`, `409` while one runs): `cousin-runner --home <home> --check-auth`, run in a child process without any auth variable, is its `status` stage (no model call). With `validate: true` the one child runs `--check-auth --validate`, which checks the status once and then spends one smallest model turn on a throwaway client (never the cousin's own session): the second stage, `one model turn`, skipped when the status failed. Each stage's detail is the runner's own line; a failure is the op's `error`. `400` for a tmux cousin, or `validate` off the `sdk` lane.

## MCP and policy

A cousin's MCP tool registry, its `.mcp.json` servers, `cousin-mcp`'s diagnostics and its `policy.toml`, plus the install's default registry (`cousin_lib/console/routes_mcp.py`). Every edit is checked by the parser that reads the file: the registry by `mcp_server.parse_registry` (strict, the runner's reading; on a runner cousin also the in-process handlers, `runner/tools.missing_handlers`), `.mcp.json` by `runner/mcp_config.parse`, `policy.toml` by `runner/policy.Policy.parse`. A TOML file is edited through `console/toml_edit.write_file_keys`, so every line the change does not touch is kept.

- **etag.** Every read answers `etag` (a hash of the file, `"absent"` when there is none). A write sends it back; a file that changed meanwhile is `409 {"error", "etag", "stale": true}` and nothing is written. The model can rewrite all of these files. The check is not a lock against the model: a write the model makes between the console's etag check and its rename (a window of milliseconds) is overwritten by the console's.
- **Applies at the next start.** Every write answers the fresh read plus `restart_required: true`. The runner reads `.mcp.json`, `policy.toml` and its registry once, at start; a tmux cousin's harness reads `.mcp.json` and the registry at session start. The inspector offers `POST /api/cousins/<slug>/restart`.
- **Secrets in `.mcp.json`.** The runner hands the servers to the agent CLI as a `--mcp-config` command-line argument, which any user on the host can read. A value that looks like a secret (a known key prefix, an opaque mixed-case token, eight or more literal characters under a name that says secret, a password in a URL, a secret-named flag's value) is refused with `problems: [{"server", "field", "key", "reason", "suggest"}]`, where `suggest` is the `${VAR}` reference to write instead. A literal already in the file is never answered: it reads as `value: null, masked: true` (a command as `command: null, command_masked`, a url as `url: null, url_masked`), and a save that sends it back as null is refused until it is replaced. A reference to an account variable (`accounts.AUTH_VARS`) is refused: the runner skips such a server.
- **The handoff.** `policy.toml` can never deny `mcp__cousin__handoff`, in `deny_tools` or in `ask` (enforced as a deny), exactly or as a prefix (`mcp__cousin__*`, `*`): `400` naming the entry. Every generation ends through it.

### `GET /api/cousins/<slug>/mcp/registry`

`{"ok": true, "scope": "cousin", "file", "exists", "etag", "source": "own" | "install" | "example" | "shipped", "shown", "lane", "restart_note", "ceiling", "timeout", "max_output", "limits", "tools": [{"name", "kind", "enabled", "description", "commands", "editable"}], "enabled_count", "error", "skipped": [{"name", "reason"}]}`. With no registry in the home, `exists` is false and the tools shown are the one the cousin reads instead (`shown`). `error` is the strict parser's (the runner refuses to start with it); `skipped` the tools `cousin-mcp` would skip. Read from the raw TOML, so a registry over its ceiling still lists its tools. `restart_note` is the same fixed line every route in this section carries: "applies at the next start: restart the cousin to use it now".

### `POST /api/cousins/<slug>/mcp/registry`

Body `{"etag", "ceiling"?, "timeout"?, "max_output"?, "tools"?: {"<name>": true|false}}`. Numbers are whole, within `limits` (ceiling 1 to 50, timeout 5 to 3600 s, max_output 1000 to 1000000 characters). Only what differs is written: a tool's `enabled` key, a top-level number. `400` for a bad value, an unknown tool (adding a tool is a file edit), a result the strict parser refuses (more enabled tools than the ceiling) or, on a runner cousin, a tool with no in-process handler. `409` with no registry in the home. A registry that is not UTF-8 text is `400`.

### `POST /api/cousins/<slug>/mcp/registry/copy-default`

Writes the install default (`mcp_server.shipped_default_registry`, the operator filled in, as spawn does) into the home. Body `{}` when there is none yet; `{"replace": true, "etag"}` to overwrite one (`409` otherwise). The browser asks for a typed "replace".

### `GET /api/mcp/registry`

The install default, `config/mcp-registry.toml`, in the same shape (`scope: "install"`), plus `example_exists`. Absent, it shows `config/mcp-registry.toml.example`.

### `POST /api/mcp/registry`

As the cousin's, on `config/mcp-registry.toml`; `409` while it does not exist.

### `POST /api/mcp/registry/copy-example`

Copies `config/mcp-registry.toml.example` (else the checkout's) to `config/mcp-registry.toml`. `{}` when absent, `{"replace": true, "etag"}` to overwrite.

### `GET /api/cousins/<slug>/mcp/servers`

`{"ok": true, "file": ".mcp.json", "exists", "etag", "lane", "restart_note", "parse_error", "reserved": "cousin", "servers": [...], "kept": [{"name", "reason"}], "last_event"}`. A server: `{"name", "type": "stdio" | "http" | "sse", "command", "command_masked", "args": [{"value", "masked"}], "env": [{"name", "value", "masked"}]}` or `{"name", "type", "url", "url_masked", "headers": [{"name", "value", "masked"}]}`, plus `ignored_keys` (keys the runner drops, kept on save), `account_vars`, `unset_vars` (a `${VAR}` with no default that the console's environment does not set: a hint, the runner's environment decides) and `masked`. `kept` lists the entries this editor does not model (the reserved `cousin`, an entry of no known shape): a save keeps them as they are. `last_event` is the newest `mcp_config` event of the runner's primary [stream](../glossary.md#stream) (`{"ts", "seq", "payload": {"file", "servers", "skipped"}, "stream"}`), or null.

### `POST /api/cousins/<slug>/mcp/servers`

Body `{"etag", "servers": [...], "drop"?: ["<kept name>"], "replace_broken"?: true}`, every editable server, in the GET's shape (an arg may be a plain string; empty lists may be left out). Names are 1 to 64 of `A-Za-z0-9_-`, unique, never `cousin` (which can never be dropped either). `400 {"problems"}` for any refusal; nothing is written. Keys no server shape declares, and the file's other top-level keys, are kept; a server whose type changed loses the old type's keys. A masked value sent back as null is refused with the `${VAR}` to write. A file that does not parse needs `replace_broken` (`409` otherwise); on a harness lane (`tmux-legacy`, or the `tmux` runner kind) the replacement keeps a fresh `cousin` entry, the one spawn writes. The new text must load every server through `mcp_config.parse`.

### `GET /api/cousins/<slug>/mcp/selftest`

`cousin-mcp --selftest` as data: `{"ok", "registry", "ceiling", "timeout", "max_output", "tools": [{"name", "kind", "commands", "operators"?}], "commands": [{"command", "where", "found"}], "missing", "skipped", "sdk": {"present", "versions"}, "missing_handlers", "error"}`. `missing_handlers` only on a runner cousin.

### `GET /api/cousins/<slug>/mcp/last-connection`

`{"last": null | {"state": "connected" | "failed" | "unrecorded", "when", "session_id", "detail", "stderr", "earlier", "path"}}`: what the harness recorded the last time it connected the cousin's MCP server (`mcp_logs.last_connection`), each text cut at 4000 characters.

### `GET /api/cousins/<slug>/mcp/status`

`{"lane", "mcp_json", "settings_file", "approved": true | false | null, "reason"}`: whether the harness settings file `config/harness.toml` names trusts this home and enables `cousin` for it.

### `POST /api/cousins/<slug>/mcp/approve`

`cousin-mcp approve`: trusts the home and enables `cousin` in that settings file (`mcp_server.approve_registration`). It rewrites the whole file (through a symlink to its target, the mode kept): a live harness session keeps its own copy and writes it back, so approve while that cousin's session is stopped; the browser asks for a typed "approve" and says so. `409` on a lane that does not read the approval (only `tmux-legacy` and the `tmux` runner kind do; an sdk, opencode or fake runner serves `cousin` in-process), with no `.mcp.json`, no `settings_file`, or a settings file that cannot be edited. Read at the cousin's next session start.

### `GET /api/cousins/<slug>/policy`

`{"ok": true, "file": "policy.toml", "exists", "etag", "error", "lane", "restart_note", "protected": "mcp__cousin__handoff", "deny_tools", "deny_bash_patterns", "ask", "outbound_filter", "template", "last_event"}`. `error` is `Policy.parse`'s (the runner refuses to start with it). `template` holds `templates/policy.toml.example`'s values, for "fill from the template". `last_event` is the start's `policy` `describe` event.

### `POST /api/cousins/<slug>/policy`

Body `{"etag", "deny_tools", "deny_bash_patterns", "ask", "outbound_filter", "confirm_loosening"?, "replace_broken"?}`. The lists are non-empty one-line strings (a repeat is dropped). Each pattern is compiled (`400 {"problems": [{"key", "index", "entry", "reason"}]}`); the whole text must pass `Policy.parse`; the handoff rule above. A change that removes a `deny_tools`, `deny_bash_patterns` or `ask` entry, or turns `outbound_filter` off, answers `409 {"needs_confirm": true, "removed": {...}}` until it is sent again with `confirm_loosening: true`. Only the keys that change are rewritten; an absent file is created with a short header. A file the runner cannot read (not UTF-8, not TOML, or not a policy) needs `replace_broken` (`409` otherwise); what it lists as TOML still counts for the loosening check. On opencode the plugin runs the patterns as JavaScript RegExp: the browser warns about a pattern JavaScript cannot compile.

## Agent and cousin settings

A cousin's `[agent]` table, per runner lane, and the rest of its `cousin.toml` the inspector edits (`cousin_lib/console/routes_agent.py`). Each kind of key has one write path:

- **`[agent]`** (the keys the cousin's runner reads): `spawn.persist_agent_values`, the same path `POST /api/cousins/<slug>/model` and `/effort` take on a runner cousin. Every change is checked by `agent_settings.validate`, with the runner's own checks (`runner/main.effort_of`, `accounts.check_lane` and the tmux kind's refusal of a key or token account, `refuse_claude_name` and the opencode model checks, the `[agent.sessions]` parser, `opencode.shell_env`, the pane's hard deny for `env_allow`), key by key and then for the table as a whole; an `sdk` model then passes one smallest [turn](../glossary.md#turn), in a child process, on the account and effort the same change writes (never the ones still in the file); `agent_settings.apply` writes every change in one atomic write whose hook re-checks the parsed table. A value the file already holds is not a change.
- **The other keys** (`cousin.name`, `cousin.peer_visible`, `memory.proactive_recall`, `memory.recall_keyword_only`, `memory.review_batch`, `memory.review_model`, `lifecycle.flip_at`, `agent.commit_attribution`): `toml_edit.write_keys`, with a hook that runs the same check on the parsed result (and `config.commit_attribution` on the `[agent]` table).
- **Read-only**: `[chat]` port, host and tmux_session (a changed port breaks every peer), `[session]` start and end hooks (shell commands). The kind (`[agent] runner`) is switched by a migration, never here.
- A write is refused with `409 {"busy": true}` while a long operation, a [flip](../glossary.md#flip) or a clean stop runs on the cousin. A change that needs a restart answers `restart_required: true`; the inspector offers `POST /api/cousins/<slug>/restart`.

### `GET /api/cousins/<slug>/agent`

`agent_settings.describe` for the cousin's lane: `{"lane", "kinds", "settings": {"<key>": {"value", "set", "default", "type", "readonly", "hint", "restart", "choices"?, "suggestions"?, "min"?, "max"?, "deprecated"?}}, "errors": {"<key>": "<reason>"}, "held"}`. `lane` is `[agent] runner`, or `tmux-legacy` (then `settings` is empty: that lane's model and effort are `[runtime]`'s). `kinds` are `delivery.RUNNER_KINDS`. `account` choices are the accounts that run on the lane; `effort` choices the levels; `model` suggestions the harness catalogue (`sdk`, `tmux`) or the account's `"<provider>/"` (`opencode`). `sessions` also carries `kinds` and `always_primary` (`operator`, `system`: never `own`). `env_allow` (the `tmux` kind) also carries `base`, the names the pane always gets, and `deny_prefixes`, the hard deny that beats it. `api_key_file` is listed only when set, read-only and `deprecated`. `commit_attribution` (every lane reads it; unset is the install default) is listed too; the inspector edits it in the cousin settings, beside its install default. Also `tmux_lane` (the name of the lane that is not a runner kind), `model_rule` (`{"required", "catalogue", "provider_model", "placeholder", "hint"}`, null where the lane reads no model) and `model_change_spends_turn` (a new model is checked with one model turn first). `errors` is what is wrong with the file as it stands (the runner would refuse to start). `held`: `<home>/run/held` stands (a stop keeps the runner down until its next start).

### `POST /api/cousins/<slug>/agent`

Body `{"changes": {"<key>": value | null}}` (null removes the key; `sessions` is `{"<kind>": "primary" | "own"}`, only the kinds that change). `200 {"ok": true, "changed": [...], "restart_required", "agent": <the GET>}`. `400 {"error", "errors": {"<key>": "<reason>"}}` for an unknown key, a read-only one (`runner`, `opencode_bin`, `api_key_file`), a key the lane does not read, or a value or table the runner would refuse; nothing is written. `400` on a `tmux-legacy` cousin. A change carrying a new `sdk` model answers `202 {"ok": true, "op"}` instead: a long operation of kind `agent-settings` (`GET /api/cousins/<slug>/op`, the `cousin-op` event) that runs the validating turn (up to 90 s), then the write; its result is `{"changed", "restart_required", "note"?}`, and a refusal fails it with `result: {"ok": false, "error", "errors": {"<key>": "<reason>"}}` (a model that did not pass: the API's own words under `model`; anything else under `changes`), nothing written. A `commit_attribution` change on a `tmux` cousin also rewrites its harness settings, as the settings route below does.

### `GET /api/cousins/<slug>/settings`

`{"fields": {"<table.key>": {"value", "set", "default", "type", "restart", "hint"}}, "readonly": {"chat.tmux_session", "session.start_hooks", "session.end_hooks"}, "readonly_why", "errors"}`. `lifecycle.flip_at` also has `install` (`config/harness.toml default_flip_at`, else `04:00`; null for never) and `effective` (`config.flip_time`). `agent.commit_attribution` also has `install: {"value", "source"}` (`config/harness.toml [agent]`, or the built-in default, true) and `effective`, the value the cousin runs with.

### `POST /api/cousins/<slug>/settings`

Body `{"changes": {"<table.key>": value | null}}`: `cousin.name` a non-empty line of at most 64 characters; `cousin.peer_visible`, `memory.proactive_recall`, `memory.recall_keyword_only` and `agent.commit_attribution` booleans; `memory.review_batch` a whole number, 0 or more; `memory.review_model` one word (the model's argv rule); `lifecycle.flip_at` `"HH:MM"` or `"never"`. Null removes the key (the default applies); the name cannot be removed. `200 {"ok": true, "changed", "restart_required", "settings": <the GET>, "note"?}`: `restart_required` is true when a changed key is read at start (the name, the recall keys, `commit_attribution`); `peer_visible`, the review keys and `flip_at` are read as they are used. A `commit_attribution` change on a `tmux-legacy` or `tmux` cousin also rewrites its `.claude/settings.json` (`harness_settings.apply_project_settings`, what `cousin-spawn --repair-settings` runs), where that lane reads it; `note` says so, names any of the operator's own keys there (`includeCoAuthoredBy`, `attribution`, not the framework's) that say the opposite and win, or says why the file was left (its own refusal). Any other failure of that step is `500 {"error", "written": {"agent.commit_attribution": value}}`: cousin.toml already holds the value, the harness settings do not. `400 {"errors"}` for a bad value, a read-only key or an unknown one; nothing is written.

## Plugins

Install-level extensions ([plugins](../plugins.md)), declared in `config/plugins.toml` and turned on per cousin in `cousin.toml [plugins] enabled` (`cousin_lib/console/routes_plugins.py`). The framework ships none; on an install without plugins every list below is empty and the console shows no plugin UI.

### `GET /api/plugins`

`{"ok": true, "plugins": [row], "problems": [{"name", "reason"}], "supervisor"}`. A row per valid plugin the install does not disable, by name: `name`, `description`, `version`, `dir`, `mcp`, `service`, `console` (whether the manifest declares each part), `port`, `title` (the page's), `placement` (the page's, `"pane"` or `"chat"`; null without one), `health` (the manifest's path or null), `enabled_by` (the slugs that enable it), `supervisor` (`{"state", "pid", "reason", "restarts"}` of the `plugin:<name>` child, null when no supervisor runs or it holds none) and `healthy` (a 1 s GET of the health path: true on 2xx, false otherwise, null without one). `problems` names every entry skipped (`name` null: the file itself). `supervisor`: a supervisor's snapshot was read.

### `GET /api/cousins/<slug>/plugins`

`{"ok": true, "slug", "enabled", "problem", "skipped", "available", "plugins", "note"}`: `enabled` is `[plugins] enabled` as written, `problem` why the table is unusable (else null), `skipped` the enabled names that do not load and why, `available` the install's rows (as above, without the live keys), `plugins` the row's `plugins`, and `note` the tmux kind's gap (its pane reads `.mcp.json` itself, so a plugin's MCP server does not reach it), null on the other kinds.

### `POST /api/cousins/<slug>/plugins`

Body `{"enabled": ["<name>", ...]}`: every name a plugin of this install. Written to `cousin.toml [plugins] enabled` (sorted) with `toml_edit`, every other line kept. `200` with the GET's body plus `changed`, `restart_required` (true when it changed: the runner reads the list at start) and `reload` (the supervisor's answer to the rescan it is asked for, which starts or stops a plugin's service; null when no supervisor runs). `400` for a name the install does not have or a list that is not one, nothing written; `409 {"busy": true}` while a long operation runs on the cousin.

### `/plugins/<name>/<path...>` (the proxy)

Not under `/api/`, behind the same guard and login (a session; `401` without one, `503` with a broken users file). `GET`, `POST` and `HEAD` are forwarded to `http://127.0.0.1:<port>/<path...>`, the query kept, where `<port>` is the plugin's declared `[service] port`; nothing else is reachable through it. Request: hop-by-hop headers, `Cookie` and `Authorization` dropped, `X-Forwarded-Prefix: /plugins/<name>` added, the body bounded at 1 MB (`413` before it is read). Response: the status and headers passed through (hop-by-hop dropped), the body streamed and flushed chunk by chunk, so an event stream (`text/event-stream`) stays open with no buffering. A plugin the install does not have, or one with no `[service]`, is `404`; a service that does not answer is `502` with one line. Other methods: `405`.

## Kind switch and migration

`cousin-migrate` from the console (`cousin_lib/console/routes_migrate.py`), through the migrate library only: the tmux-lane migration to the sdk runner (plan, apply, check, rollback; the runbook is [migrating](../migrating.md)) and the kind switch between the `sdk` and `tmux` runner kinds (`--to`). apply and rollback change a live cousin: each is the cousin's long operation (`GET /api/cousins/<slug>/op`), its steps reported as stages as the library writes them to `data/migration.json` or `data/kind-switch.json`, and they run one at a time across the whole fleet: another cousin's migration, switch or rollback makes them `409 {"busy": true}`, as does a flip, a clean stop or another op on the cousin. A plan or a check with `validate` spends one model turn, so it is an op too (kinds `migrate-plan`, `migrate-check`); without it, it answers at once. Who started an op is its `params.by`. Not supported from the console: adopt (a tmux-kind start adopts its pane on its own) and `--all --keep-going` (switch one cousin at a time); the state lists them in `deferred`.

### `GET /api/cousins/<slug>/migrate`

`{"ok", "slug", "lane" ("tmux-legacy" or the [agent] runner kind), "refusal", "kinds" (the switch's kinds), "steps", "switch_steps", "migration", "switch", "loginScreen", "supervisor", "running", "deferred"}`. `refusal` is the lane refusal line for a cousin with no `[agent] runner` (the panel shows it instead of a migrate button), else null. `migration` and `switch` are the records (`state`, `steps`, `rollback_steps`, `warnings`, the times, `from`/`to` for a switch, ...), never the saved cousin.toml bytes or mode; null when there is none. `loginScreen` is `{"screen", "kind", "reason"}` from `data/login-required.json` (a tmux-kind runner writes the screen its pane waits on: `trust`, `login`, `onboarding`, `bypass`, `mcp_approval`), never its detail; null when there is none. `supervisor` says a cousin-supervisor answers for the root. `running` is `{"slug", "kind"}` of the migration, switch or rollback running on the fleet, or null. `deferred` is `[{"id", "label", "why"}]`. `person_screens` names the screens a tmux-kind pane can wait on a person at, `pane_answers` the ones the console's pane answers (the rest are done in a terminal), `pane_kinds` the runner kinds whose cousin has a pane the console shows, and `op_kinds` this section's op kinds: the browser keeps no copy of them.

### `POST /api/cousins/<slug>/migrate/plan`

2.0.0: a plan, apply or migration rollback without `to` (the 1.x tmux-lane migration) is refused before anything runs: `409` with the lane refusal line for a cousin with no runner, `400` naming the kinds for a runner cousin. The rest of this section describes 1.x for the record.

Body `{"account"?, "validate"?}` for the tmux-lane migration, `{"to": "sdk" | "tmux"}` for the kind switch (an account or validate with `to` is `400`). Writes nothing. `200 {"ok": true, "plan"}`: the library's plan, `{"slug", "checks": [{"check", "ok", "detail"}], "steps", "ready", ...}` (the migration adds `account`, `carry`, `cli`, `notes`; the switch `from`, `to`, `warnings`). With `validate: true` it is an op (`202 {"ok": true, "op"}`, kind `migrate-plan`) whose result is `{"plan"}`: one smallest model turn on the model, effort and account the runner will run. `400` a bad `to`, account name or flag.

### `POST /api/cousins/<slug>/migrate/apply`

Body as for the plan, plus `"confirm": true` (`400` without). `202 {"ok": true, "op"}`, kind `migrate` or `kind-switch`, `params.steps` the stages it plans: `plan, close, handover, import, toml, start, verify` for the migration (`plan` runs the checks again, with the model turn when `validate`: a carried model is never written unvalidated), `trust` (tmux only), `close, toml, cursor, start, notice, verify` for the switch. A plan that is not ready fails the op with the reasons and changes nothing; a failed step fails it with the step and its detail, to be rolled back. A switch step that raises something the library does not word marks `data/kind-switch.json` failed at that step (`failed`, `error`: the step and the exception's type; its text goes to the console's stderr only). While the switch's verify runs, the pane is watched: when it waits on a person, the verify stage says so, `waiting for the operator to accept the trust dialog in the pane (screen: trust)` for the trust dialog, and the browser offers the pane (below) to answer it in. `409` when no cousin-supervisor runs for the root, or busy (above).

### `POST /api/cousins/<slug>/migrate/check`

Body `{"since"?: "<ISO time>", "validate"?: bool}`. The exit criterion, `200 {"ok": true, "check"}`: `inbox` (done, failed, open, stale), `inbox_readable`, `tool_calls`, `unrecorded`, `hook_errors`, `config`, `mismatches`, `warnings`, `cli`, `chat_ok`/`chat`, `ok`. With `validate` it is an op (kind `migrate-check`) whose result is `{"check"}`, with `validate_ok` and `validate`. `400` a `since` that is not an ISO time.

### `POST /api/cousins/<slug>/migrate/rollback`

Body `{"which": "migration" | "switch", "confirm": true, ...}`. `"switch"` takes `to`, the kind the switch came from (the library refuses another: the op fails saying which), and no force. `"migration"` takes `force` ([inbox](../glossary.md#inbox) rows still waiting, or an inbox that cannot be read), which asks a second time: `"force_confirm": true` too, else `400`. `202 {"ok": true, "op"}`, kind `migrate-rollback` or `kind-switch-rollback`, its stages the library's rollback steps. A refusal from the library (already rolled back, rows waiting, a runner that will not let go) fails the op with its words. `409` when no cousin-supervisor runs for the root.

## Lifecycle

`cousin-reincarnate` and `cousin-transplant` from the console (`cousin_lib/console/routes_lifecycle.py`), through `cousin_lib.lifecycle`, each a long operation. The library keeps its own audit (`data/lifecycle/audit.jsonl`) and snapshots (`data/lifecycle/<slug>/<ts>/`); a refusal changes nothing. The console adds two rows to that audit per op, `{"ts", "op", "step": "console-request" | "console-result", "by": "console", "actor" (the console user, null without a users file), "op_id", ...the op's parameters, "ok"?, "error"?}`.

### `GET /api/lifecycle/modes`

`{"ok", "modes": [{"id", "confirm": "second" | "typed", "phrase"?, "what"}], "op_kinds", "role_max"}`: the transplant modes (soul-donation, body-swap, merge), what each does and how it is confirmed (`phrase`, with `{donor}` and `{recipient}`, for a typed one), the op kinds, and reincarnate's role limit.

### `GET /api/cousins/<slug>/lifecycle`

`{"ok", "slug", "held": null | {"op_id", "recipient", "mode", "since"}}`: whether the cousin is held as a running transplant's donor (the op itself is the recipient's).

### `POST /api/cousins/<slug>/reincarnate`

Body `{"new_role", "confirm": true}`: one line of at most 200 characters. `202 {"ok": true, "op"}`, kind `reincarnate`, stages `snapshot`, `bequest` (it rides the flip's own handoff request, under the runner's handoff deadline), `rewrite` (the role in CLAUDE.md and cousin.toml), `flip`. The op fails when the flip does. `400` a bad role or no confirm; `409` busy.

### `POST /api/lifecycle/transplant`

Body `{"donor", "recipient", "mode", "confirm"}`. `confirm` is `true` for `merge`; the modes that replace what the recipient is are typed: `"donate <donor> <recipient>"` for `soul-donation`, `"swap <donor> <recipient>"` for `body-swap`. It runs as the recipient's op (kind `transplant`, `params.donor`) while the donor is held (`longop.exclusive`), so nothing else starts on either until it ends: stages `snapshot`, `apply`, `flip <donor>`, `flip <recipient>`. `400` an unknown mode, the same cousin twice, or no (or a wrong) confirm; `404` an unknown cousin; `409` a flip, a clean stop or an op on either.

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
| `home` | the cousin's home directory |
| `tmuxSession` | the tmux session the cousin runs in: on `tmux-legacy`, `[chat] tmux_session`, default the slug; on the `tmux` runner kind, the runner's own `tmux-<slug>`; null on every other runner kind (`sdk`, `opencode`, `fake`), which has no tmux session |
| `operator` | `[operator] name`, or null |
| `memoryScope` | `private` or `shared` (an older `both` in cousin.toml reads as `shared`) |
| `heartbeat` | context heartbeat in seconds (default 3600) |
| `flipAt` | `[lifecycle] flip_at`, or null |
| `model`, `effort` | what the next start will use: a runner cousin's `[agent] model` and `effort`, else null (the CLI's own default); on `tmux-legacy`, the `[runtime]` value, else `config/harness.toml [agent]` default, else null |
| `hidden` | `[cousin] hidden` |
| `status` | `running` or `stopped`. Runner cousin (`[agent] runner`): a runner holds its lock (`run/runner.lock`). [Worker](../glossary.md#worker): always `running`. A cousin on `tmux-legacy` (which 2.0.0 refuses to start): its tmux session exists, or with `[chat] host` its chat server answers. |
| `attention` | always null: the pane check that set it read `attention_patterns`, a key 2.0.0 removed (a login wait is `loginRequired`) |
| `chat` | `console` for a runner cousin, whose chat the console serves itself; for any other cousin `ok`, `down` or `none` (no port) from an upstream chat server's `/health` |
| `active` | the last 20 pane lines changed in the last 60 seconds; for a runner cousin, a live turn (`running` or `waiting_permission`) |
| `pid`, `uptime_seconds` | the agent process in the tmux pane and its age (a runner cousin: the `cousin-runner` process); null when unknown, never 0 |
| `activity` | first 200 characters of `data/last-activity.txt` |
| `lastMsgTs` | unix time of the cousin's newest reply in your [thread](../glossary.md#thread) (the `[operator] name`, last 20 rows), 0 if none |
| `tokensSpent` | today's token total, 0 when token counting isn't set up |
| `runner` | null for a tmux cousin. A runner cousin: `{"alive", "state", "since", "session", "kind", "pid", "unsupported"}` from its own stores: `alive` whether a runner holds its lock, `state` the last state its primary event stream recorded (with `since`, that event's time; it stays the last one recorded after the runner is gone, so read it with `alive`), `kind` (`sdk`, `fake` or `opencode`), `pid` and `unsupported` (the contract items the runner declares it does not support) from the `runner` event `cousin-runner` writes at start |
| `supervisor` | `{"state": ...}` for a runner cousin the running `cousin-supervisor` holds as a child (`running`, `backoff`, `failing`, `stopped`), read from its `run/supervisor.json`; null when no supervisor runs (or the file is stale) or it holds no child for this cousin. Null is unknown, never stopped |
| `lane` | the `[agent] runner` kind when it is one of the runner kinds, else `tmux-legacy` |
| `account` | a runner cousin's `[agent] account`, `host` when it names none; null on `tmux-legacy` |
| `autoStart` | a runner cousin's `[agent] auto_start` (the supervisor starts it with itself unless false); null on `tmux-legacy` |
| `held` | `<home>/run/held` exists: a stop holds the runner down until the next start |
| `loginRequired` | the runner waits for a login or billing fix: `{"reason", "action", "since"}` from `data/login-required.json` (`action` is the line to run), else null. The file's `detail` is never shown |
| `plugins` | the [plugins](../plugins.md) this cousin enables (`[plugins] enabled`) that the install has, valid and not disabled: `[{"name", "title", "description", "tab"}]`, `tab` `{"title", "url", "placement"}` for a plugin with a `[console]` page (`url` is `/plugins/<name><page>`, the proxy below; `placement` `"pane"`, a tab over the pane, or `"chat"`, a strip over the chat), else null; `[]` when none, and always on an install without plugins |
| `removedKeys` | the keys 2.0.0 no longer reads that this cousin's `cousin.toml` still carries (`[chat] port`, `[runtime] model`, ...): `[{"where", "key", "line"}]`, `line` saying what to do; `[]` when none (and on a file that does not parse). Inert, never a refusal: the card names them until `cousin-migrate tidy <slug> --yes` removes them. The install's own (`config/harness.toml`, ...) are in `cousin-supervisor status` ([configuration](../configuration.md#removed-in-200)). A remote row has none |

With the hive on, remote nodes follow the local rows. They carry the same keys (the local-only ones null or 0) plus `remote: true`, `remoteState` (`online`, `offline`, `pending` = built but never checked in, `revoked`), `online`, `revoked`, `checkedIn`, `lastSeen`, `version`. For a remote row `status` is `running` when online, and `chat` is derived (`ok` online, `down` offline, `none` before the first checkin), never probed. If a slug is both local and a node, the local row wins.

### `POST /api/cousins`

Spawn a cousin. Body:

```json
{"slug": "wren", "role": "research helper", "voice": "dry, short answers",
 "name": "Wren", "role_paragraph": "...", "operator": "ana",
 "model": "claude-opus-5", "effort": "high", "heartbeat": 3600,
 "memory_scope": "private", "runner": "sdk", "account": "metered"}
```

`slug`, `role` and `voice` are required (the CLAUDE.md template won't render without a voice). The rest are optional; empty means the default applies and no key is written. `runner` (`sdk`, `tmux`, `opencode` or `fake`; `tmux-legacy` is refused) and `account` go to `[agent]`, and so do `model` and `effort`, checked by the lane first: the account must run on it and a key it does not read (an effort off the `sdk` lane, a model on `fake`) is a `400`; left out, the install's `COUSIN_DEFAULT_RUNNER` and `COUSIN_DEFAULT_ACCOUNT` apply (unset: `sdk` on `host`). An account must be `host` or in `config/accounts.toml`. `201 {"ok": true, "slug", "home"}` and a `cousins-refresh` event. `400` bad input (the message says which; a `port` is refused: no cousin has a chat port in 2.0.0), `409` the slug exists or a leftover directory squats it. This only creates the cousin; the page follows it with `/start`.

### `GET /api/spawn/options`

What the spawn dialog offers:

```json
{"models": [...], "default_model": "...", "efforts": ["low","medium","high","xhigh","max"],
 "default_effort": "high", "memory_scopes": ["private","shared"],
 "default_memory_scope": "private", "default_heartbeat": 3600,
 "heartbeat_bounds": [60, 2592000], "operator_max_chars": 64,
 "runners": ["sdk", "fake", "opencode", "tmux"], "default_runner": null,
 "accounts": [{"name": "host", "kind": "claude-login", "lanes": ["sdk", "fake", "tmux"]}, ...],
 "accounts_error": null, "lane_keys": {"sdk": ["runner", "account", ...], ...}}
```

`runners` are the runner kinds, `delivery.RUNNER_KINDS`; `default_runner` is `COUSIN_DEFAULT_RUNNER`, else `sdk`. `accounts` lists `host` and `config/accounts.toml`'s entries with their kind and the kinds each runs on, by the runner's own rule (`agent_settings.check_lane`: the `tmux` kind never offers a key or token account), and for an opencode account `models`, the `"<provider>/"` suggestions it offers (`accounts_error` says why the file could not be read, and the list is then `host` alone). `lane_keys` names the `[agent]` keys each kind reads, and `lane_models` how each kind that reads a model takes it: `{"required", "catalogue", "provider_model", "placeholder", "hint"}` (`catalogue`: the `models` list is a valid suggestion there; `provider_model`: it takes `"<provider>/<model>"`, opencode only). `tmux_lane` names the retired legacy lane (`tmux-legacy`), which a spawn refuses.

`models`, `default_model` and `default_effort` come from `config/harness.toml [agent]`. No `models` there means the built-in list; no `default_model` means the first model in the list. `500` if harness.toml exists but can't be read.

### `DELETE /api/cousins/<slug>`

Dismiss. Stops the cousin (a cousin with no runner is not stopped, since nothing 2.0.0 starts can be running for it: the result carries `"stop": "skipped"` and `note`, the refusal line), tars the whole home (minus `.secrets/`) to `data/dismissed/<slug>-<YYYYmmdd-HHMMSS>.tar.gz`, then deletes the home. If the archive fails, nothing is deleted and you get `500` with `refusing to delete: ...`. `200 {"ok": true, "slug", "status": "deleted", "archive", "left_in_place": [...]}`. `left_in_place` lists the harness directories (`transcripts_dir`, `auto_memory_dir` from harness.toml) it didn't touch; clean those yourself if you want them gone.

### `POST /api/cousins/<slug>/start`

A runner cousin (`[agent] runner`: `sdk`, `tmux`, `opencode` or `fake`) is started by the running `cousin-supervisor` ([commands](../commands.md)): no `config/agent-cmd`, no chat server. `200 {"ok": true, "slug", "status": "started" | "already running"}`; already running means a runner holds the cousin's lock. `503` when no supervisor runs for the install (the message says to run `cousin-supervisor run`). `409` when the supervisor answers that the cousin's runner is still stopping (try again once it is down), and when a runner the supervisor did not start (one started by hand) holds the lock while the cousin is held: a start would only add a second runner waiting for that lock, so none is asked for. `500` with its reason for any other refusal. Every refusal follows the `starting` event with a `cousin-status` `start failed` carrying the error. A cousin with no runner kind is `409 {"ok": false, "error": "<the lane refusal: 2.0.0 has no legacy tmux lane>"}`, before any tmux call.

### `POST /api/cousins/<slug>/stop`

Body (optional): `{"clean": true}` (the default).

A cousin with no runner kind is `409` with the lane refusal, before any tmux call; a [worker](../glossary.md#worker)'s stop is `200 {"ok": true, "slug", "status": "stopped", "worker": "no session", "note"}`. Emits `cousin-status` `stopping`. `400` when `clean` is not a boolean.

A runner cousin always stops through the supervisor, clean or not: a clean stop is the runner's own SIGTERM path (it finishes the turn in hand, then exits, which can take up to about 35 seconds). The route does not wait for that: `202 {"ok": true, "slug", "status": "stopping", "runner": "stopping", "supervisor": "running"}` once the runner is signalled, and the row's `supervisor.state` turns `stopped` when it is down. When there was nothing to stop the answer is `200` with `"status": "stopped"` and `"runner": "stopped" | "not running"`, `"supervisor": "running" | "not running"`. When the supervisor refused the stop the answer is `502 {"ok": false, "slug", "error": "cousin-supervisor refused the stop: <reason>", "runner": "unknown", "supervisor": "running"}`, never `stopped`. With no supervisor running and a runner started by hand still holding the cousin's lock, nothing can signal it from here: `503 {"ok": false, "slug", "error", "runner": "running", "supervisor": "not running", "held": true}`, the error saying so and that the hold is written. The stop holds the runner down until the next start, across a supervisor or container restart too (`<home>/run/held`), and also with no supervisor running: the route writes the hold itself and adds `"held": true`. A restart whose start is refused because no supervisor runs removes that hold again.

### `POST /api/cousins/<slug>/restart`

Immediate stop (as `{"clean": false}`), wait about a second, start. `200 {"ok": true, "target": "cousin/<slug>", "stop": {...}, "start": {...}}`. If the start fails you get the start's status code with `ok: false` and its error body under `start`.

A runner cousin that is running is signalled and answered at once, `202 {"ok": true, "slug", "status": "stopping", "runner": "stopping", "supervisor": "running", "target": "cousin/<slug>"}`; the console starts it again in the background once the supervisor reports it down, and says how that went with a `cousin-status` event (`started` or `start failed`) and a `cousins-refresh`. With nothing to stop it is the `200` above.

### `POST /api/cousins/<slug>/role`

Body `{"role": "..."}`, up to 5000 characters. Rewrites `[cousin] role` in place, keeping every other line. `200 {"ok": true, "slug", "role"}`.

### `GET /api/cousins/<slug>/claude-md`

`{"ok": true, "slug", "path", "content", "bytes"}`. A missing file is `content: ""`, `bytes: 0`, `missing: true`, not an error.

### `POST /api/cousins/<slug>/claude-md`

Body `{"content": "..."}`, up to 200000 characters. The old file is copied to `data/claude-md-backups/CLAUDE-<unix>.md` first. `200 {"ok": true, "slug", "bytes"}`. The running agent reads it at its next start or flip.

### `POST /api/cousins/<slug>/model`

Body `{"model": "claude-opus-5"}`. A tmux cousin's goes to `[runtime] model` in cousin.toml; a runner cousin's to `[agent] model`, the key its runner reads. It has to be one word of letters, digits and `._:/+-`, because it goes into the agent's argv. On the runner lane it is validated first, as `cousin-migrate` validates: on `sdk`, one smallest model turn on the cousin's own account, run in a child process (up to 90 s) so the console's own environment is never touched; on `opencode`, `"<provider>/<model>"` on a provider the account holds, never a Claude model. `200 {"ok": true, "slug", "model", "restart_required": true}`: the running agent keeps the model it started with. `400` empty or splittable, or a model that did not pass (the reason, the API's own words on `sdk`); nothing is written then. The cousin's current value is `200` with `"restart_required": false`, no validating turn, no write and no `cousins-refresh`. On a runner cousin this is the agent settings' own route logic (see [Agent and cousin settings](#agent-and-cousin-settings)): an `sdk` model change answers `202 {"ok": true, "op"}`, the cousin's `agent-settings` long operation, whose result carries `changed` and `restart_required` (or `errors`); every other case is `200` as above. On any lane, `409 {"busy": true}` while a long operation, a flip or a clean stop runs on the cousin.

### `POST /api/cousins/<slug>/effort`

Body `{"effort": "high"}`, one of `low`, `medium`, `high`, `xhigh`, `max`. A tmux cousin's goes to `[runtime] effort`, a runner cousin's to `[agent] effort`. `200 {"ok": true, "slug", "effort", "restart_required": true}`; the cousin's current value is `"restart_required": false` and no `cousins-refresh`. `400` any other value, or a runner cousin whose kind reads no effort. `409 {"busy": true}` while a long operation, a flip or a clean stop runs on the cousin.

### `POST /api/cousins/<slug>/operator`

Body `{"operator": "ana"}`. Written to `[operator] name`. One line, 1 to 64 characters, no leading or trailing spaces, no control characters. `200 {"ok": true, "slug", "operator", "restart_required": true}`: the runner reads it at start to tell your messages from other cousins', so restart the cousin. `400` leaves the file untouched.

### `POST /api/cousins/<slug>/memory-scope`

Body `{"memory_scope": "shared"}`, `private` or `shared`. `both`, an older name, is accepted and stored as `shared`. Written to `[memory] scope`. `200 {"ok": true, "slug", "memory_scope" (the stored value), "restart_required": false}`: it's read on every shared-tier call.

### `POST /api/cousins/<slug>/heartbeat`

Body `{"heartbeat": 1800}`, whole seconds from 60 to 2592000 (30 days). Written to `[heartbeat] context_beat_seconds`. `200 {"ok": true, "slug", "heartbeat", "restart_required": false}`: the loops daemon reads it on its next tick.

The three identity routes above also emit `cousins-refresh`.

### `POST /api/cousins/<slug>/hidden`

Body `{"hidden": true}`. Sets or removes `[cousin] hidden`. `200 {"ok": true, "slug", "hidden"}`. Only the console reads this flag.

### `POST /api/cousins/<slug>/peer`

Send a message from one cousin to another. Body `{"to": "kestrel", "text": "..."}`. It's written to Kestrel's chat store and inbox in-process, with `user` set to Wren's display name, so it lands as `(Chat Wren): ...` like `cousin-chat send`. `200 {"ok": true, "to", "id"}`. `400` empty text or `to` is the same cousin, `404` either cousin unknown, `502` when Kestrel has no runner kind (no transport to it).

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

Body `{"delay_seconds": 0}`.

- No delay: runs the flip on a background thread and answers `202 {"ok": true, "slug", "status": "running", "started_at"}` straight away. Progress comes as `cousin-flip` events. `409` if one is already running, or a long operation runs on the cousin (below). A clean stop is refused the same way.
- `delay_seconds > 0`: queues a flip request for the loops daemon, which sends the T-5m / T-1m / T-30s warnings and fires it. `202 {"ok": true, "slug", "request_id", "fire_at", "delay_seconds"}`. `409` if a timed flip is already pending.

`400` if `delay_seconds` isn't a non-negative integer.

### `POST /api/cousins/<slug>/flip/cancel`

Cancels a pending timed flip. `200 {"ok": true, "slug", "was_pending": bool}`. A flip that is already running can't be cancelled: `409`.

### `GET /api/cousins/<slug>/op`

The cousin's long operation (a kind switch, an account login: whatever a route runs through `console/longop.py`), running or the last one finished since the console started: `200 {"ok": true, "op": null}` before any, else `{"ok": true, "op": {"id", "slug", "kind", "status": "running" | "done" | "failed", "started_at", "finished_at", "params", "stages": [{"name", "status": "running" | "done" | "failed" | "skipped", "detail", "at"}], "result", "error"}}`. One operation runs per cousin at a time, and never beside a flip or a clean stop: a route that starts one answers `202 {"ok": true, "op"}`, or `409 {"busy": true}`. While one runs, the cousin's start, stop, restart and dismiss are `409` too. A failure an operation words for the operator is its `error`; any other exception is `"failed: <ExceptionType>, see the console log"`, its text on the console's stderr only. Progress comes as `cousin-op` events. `404` unknown cousin.

### `GET /api/tokens`

Token use per cousin per day for the last 14 days (UTC), read from the harness transcripts:

```json
{"available": true, "cousins": [{"slug": "wren", "name": "Wren",
  "series": [{"day": "2026-09-18", "total": 812345, "output": 20311}]}]}
```

Each cousin also carries `cache`: the prompt-cache hit rate, `cache_read / (cache_read + cache_creation + input)` from the usage the model reported, `{"rate": <the 14 days>, "days": [{"day", "read", "creation", "input", "rate"}]}`, one row per day oldest first. `rate` is null when the window (or the day) holds no usage; a result that carried no usage adds nothing to either side, so it is left out rather than counted as a miss. A runner cousin on `[agent] runner = "sdk"` or `"opencode"` is read from its own `data/usage.db` only, with no transcript seam needed (an opencode cousin's rows, lane `opencode`, are the tokens its provider reported and the cost opencode reported for them).

It needs `transcripts_dir` in `config/harness.toml`. Every transcript in the cousin's transcripts directory touched in the window is read, its sessions (a cousin that flips daily has one per day) and their subagents, and each message counts once (the harness writes one line per content block, each repeating the usage). The total adds input, output, cache read and cache creation tokens. Without the config: `{"available": false, "reason": "...", "cousins": []}`. The transcripts are read incrementally, so the first call after a console start is the slow one.

## Chat

No cousin on this machine runs a chat server of its own. For a runner cousin (`[agent] runner`, its home on this machine) the console answers these routes itself over the cousin's `data/chat.db`, through `server/chat_api.py`, with the bodies and `400` texts of the [chat API](chat-api.md). Its send stores the row and delivers it to the cousin's inbox (a `chat` item on the sender's thread, an image handed on as its file), then fires the cousin's chat hooks; a reaction tells the cousin with a `reaction` item. The console stores no messages. The cousin comes from `cousin` in the query or body. `400` bad slug, `404` unknown cousin. Any other cousin is forwarded upstream by its host and port: `502 {"ok": false, "error": ...}` if that server is unreachable, there is no port, or it answers non-JSON (a local cousin with no runner kind has none to answer); any JSON answer comes back with its own status.

For a slug that isn't local but is a hive node, the console proxies to where the node last checked in from and sends the node's token as a bearer. A revoked node is `404`, one that never checked in is `502`. Remote cousins have no pane, no inbox files and no media folders on this machine.

### `GET /api/messages`

Query: `cousin`, `user` (both required), `limit` (default 200), `before`, `since`, `archived` (`0`, `1`, `all`; default `0`). Answers the chat API's `/api/history` body, `{"messages", "total", "has_more"}`, plus `"cousin"`. Each message gets an `attachment: {"url", "kind"}` when there is a file for it: an inbound image under `chat/inbound/<id>.<ext>` (kind `image`), or the row's `attachment_path` when it sits in `chat/images/`, `chat/audio/` or `chat/video/`. `kind` is `image`, `audio` or `video` (a stored `voice` shows as `audio`).

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

Only a tmux-kind runner cousin has a pane (below); the `sdk`, `opencode` and `fake` kinds have none. For any other cousin all four fall back to the retired legacy lane's lookup: the tmux session `[chat] tmux_session` (default the slug) through the console's `--tmux-bin` and `--tmux-socket`, or over `ssh <host>` with the remote user's default socket for a cousin with `[chat] host`. Nothing 2.0.0 starts runs there, so that is normally `409 session not running`. Errors: `404` unknown cousin, `400` no tmux session configured, `409 session not running`.

A tmux-kind runner cousin (`[agent] runner = "tmux"`) is addressed where its runner keeps its pane instead: the framework's own socket (`<root>/run/tmux.sock`) and the session `tmux-<slug>`, matched exactly. Its runner types into that pane itself, from its own process, so `input` there answers only a one-screen dialog the runner never types into: the trust, bypass and MCP approval dialogs (the kind switch's trust step is the case this is for). The login and onboarding flows take several screens, a URL and a code: do them in a terminal attached to the pane (`tmux -S <root>/run/tmux.sock attach -t tmux-<slug>`); input on them is `409` saying so. On an answerable dialog:

- Only a closed key set goes in: Up, Down, Left, Right, Enter, Escape, Tab, Backspace, one digit, `y` or `n`. A run of text or a paste, a control key or a mouse report is `409` and nothing is sent (the text is never echoed).
- A request ends at its first Enter or Escape, where the screen changes: the keys up to it go in, the rest is `409 {"sent", "refused"}` with "`<n>` keys went in".
- Keys go in one at a time under a per-pane lock, the screen read again before each; one that finds the dialog gone stops there (`409` with `sent`).
- For 0.3 s after an Enter or Escape nothing goes in (`409`, nothing sent): type again once the screen has settled. The browser's pane sends one request at a time here.

Anywhere else `input` is `409` and the chat is the way in. `resize` is `409`: the runner reads its screen at a fixed size.

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

`kind` is what the runner recorded: `state`, `turn_start`, `text`, `thinking` (`{"length", "text"[, "truncated"]}`, the text bounded at 8000 characters), `tool`, `tool_result`, `tool_call`, `result`, `user`, `error`, `auth`, `rate_limit`, `rollover`, `usage`, `system` and the rest the runner writes. A background task's lifecycle is four `system` subtypes (the `sdk` runner only, since 1.27; before it they carried the subtype alone): `task_started` (`{"task_id", "description", "task_type", "tool_use_id"}`), `task_progress` (`{"task_id", "last_tool_name", "usage": {"total_tokens", "tool_uses", "duration_ms"}}`), `task_updated` (`{"task_id"[, "status"]}`, the status only when the update carries one; `completed`, `failed` or `killed` ends the task) and `task_notification` (`{"task_id", "status", "summary"}`, the summary bounded at 300 characters); never the task's prompt or output. `400` for an `after` that is neither `<seq>` nor `<session>:<seq>`.

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

Body with any of `status`, `result_summary`, `exit_code`, `title`, `description`. Setting `status` to `cancelled`, `done` or `failed` ends the job's processes, as `cousin-job` does: every live member of its process group gets SIGTERM, and whatever is still alive 3 seconds later gets SIGKILL. A running row that recorded a pid but no process group (an older row) also has that pid sent SIGTERM on `cancelled`. `200 {"ok": true, "job": row}` and a `job-update` event. `400` no fields or a bad status.

### `DELETE /api/jobs/<id>`

Removes the row, and the log file too if it's one `cousin-job` created in its own log directory. `200 {"ok": true}` and a `job-delete` event.

## Loops

The console reads loop state and queues requests; the loops daemon does the firing. See [loops](loops.md) for the model. The three reads (`GET /api/loops`, `GET /api/loops/recent` and `GET /api/cousins/<slug>/loops`) carry `"daemon": {"ok", "last_tick"?, "message"}`; drift, save, hidden and fire do not. When `ok` is false the message says `loops daemon has never run` or `loops daemon down (last tick NNs ago)`.

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

The routes after it are the per-cousin memory explorer. Paths are relative to the cousin home. Absolute paths and `..` are `400`, a path that leads out of the home through a link is `403`, anything under `.secrets/` is `404`.

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

Each entry is broken out into fields (`tier`, `file`, `topic`, `content`, `truth_level`, `level`, `source`, `timestamp`, `id`, `cite`, `extra`, ...), where `cite` is where an operator-stated entry says it came from (null when it carries none), and carries `ref: {"path", "line_no", "sha"}` for deleting it. Archive entries have `ref: null`: the archive stays whole.

### `GET /api/memory/<slug>/decisions`

The decisions log, newest first. Query `q`, `limit`, `offset`, `archives=1` to include rotated files. `{"entries": [{"timestamp", "topic", "decision", "reasoning", "file", "ref", "mirrors"}], "total", "offset", "limit"}`. `mirrors` are the raw entries `cousin-memory decide` wrote alongside the decision.

### `GET /api/memory/<slug>/files`

Query `layer`: `active`, `index`, `distilled`, `memory`, `notes`, `harness` or `legacy` (anything else `400`). `{"layer", "files": [{"path", "name", "size", "mtime", "recalls", "deletable", ...}]}`. Harness paths are relative to the harness auto-memory directory.

### `GET /api/memory/<slug>/file`

Query `path`, `start`, `count`, and `layer=harness` to read from the harness auto-memory directory (`404` if none is configured). Same answer as [the file reader](#get-apicousinsslugfilesread).

### `GET /api/memory/<slug>/tensions`

`{"tensions": [{"topic", "claims": [...]}]}`, newest first: the cousin's authored topics with two or more live claims of different content (`cousin-memory tensions`). Each claim is a raw entry with its `id`, `valid_from` and `valid_to` (null while live). Nothing judges whether the claims are opposite; the operator settles a tension by retiring one claim, `cousin-memory obsolete <topic> --why <reason> --entry <id>` or `POST /api/memory/<slug>/obsolete` with `entry`.

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

Body `{"topic": "...", "why": "...", "force": false, "entry": "<id>"}` (`entry` optional). Appends an L5 entry for the topic, recorded as by the logged-in user with source `console`, then rebuilds the [distilled](../glossary.md#distilled) views, which leave the topic out until a later entry brings it back. With `entry`, a claim's id from the tensions list, the mark retires that one claim and the topic stays; an id that is not one of the topic's claims is a `400`. An operator-level claim is the operator's to retire: `403` for anyone but the operator account (see [Memory operator actions](#memory-operator-actions)) when `entry` names an L0 claim, or, without `entry`, when the topic has a live L0 claim. Nothing is removed from raw. `200` with the entry and `effects` (`distilled`, `obsolete_topics`, or `distill_error` if the rebuild failed; the mark is written either way) and a `memory-change` event with action `obsolete`. `400` when topic or why is missing, the reason is empty, or the topic has no raw entries and `force` is off.

### `POST /api/memory/<slug>/restore`

Body `{"id": "..."}`. Files go back to their path, lines back into their file at their old position. `200 {"ok": true, "restored": manifest, "effects": {...}}` and a `memory-change` event. `409` if something is already back in the way, `404` unknown id. The CLI does the same with `cousin-memory trash restore <id>`.

### Memory operator actions

The routes below are the operator's side of `cousin-memory`, `cousin-self-portrait`, `cousin-reason` and `cousin-callback`, on one cousin. They call the same library functions the CLIs call. A review verdict, a self-portrait commit and a change to the shared reviewer list are a person's act and need a logged-in console user (`403` without logins or without a session). The operator's word needs the operator account (`403` for anyone else): an operator-level write, retiring an operator-level claim (`obsolete`), and dropping one at review. The console has no roles, so the operator account is the logged-in user whose name is the cousin's `[operator] name`, case aside; with no `[operator] name` nobody is.

### `GET /api/memory/<slug>/search`

Query `q` (required, else `400`), `top` (default 10, at most 50), `collection` (`memory`, `notes`, `harness` or `raw`; anything else `400`). The library search (`cousin-memory search`): keyword always, meaning when `config/embedding.toml` sets it up, fused by rank. `{"query", "hits", "semantic": "on"|"off"|"broken", "notice"}`. Each hit is `{"collection", "rel", "score", "similarity", "snippet", "legs"}`: `rel` is relative to the home (to the harness directory for a harness hit, with `layer: "harness"`; a raw hit is `memory/raw/<file>#<line>` and carries `entry` with its topic, content, level, timestamp, cite and id), and `legs` says which search found it, `keyword`, `semantic` or both. `notice` is the degrade line when meaning was promised and could not serve. A console search is not recorded in the cousin's recall log.

### `GET /api/memory/<slug>/writer`

What the write forms may offer the current user: `{"user", "operator", "can_write_operator", "levels", "cite_preview"}`.

### `POST /api/memory/<slug>/remember`

Body `{"topic", "fact", "level"?, "note"?}`. One fact into raw memory (`cousin-memory remember`). `level` is `operator`, `tool`, `conclusion` (default) or `hypothesis`; `framework` and `obsolete` are `400` (the framework writes its own entries, and obsolete is the retire action). `operator` from anyone but the operator account is `403`. The cite is filled here, never taken from the body: `console user <name>, <UTC time>` (`console (no login), <time>` without logins), then `; <note>` when a note (at most 300 characters) is given. `200 {"ok": true, "line"}` and a `memory-change` event.

### `POST /api/memory/<slug>/decide`

Body `{"topic", "decision", "reasoning", "level"?, "note"?}`. Logs the decision in `data/decisions.jsonl` and its raw copy (`cousin-memory decide`), levels and cite as for remember. `200 {"ok": true, "line"}`.

### `GET /api/memory/<slug>/history`

Query `topic` (required). `{"topic", "claims"}`: the topic's claims, oldest first, each a raw entry with `id`, `valid_from`, `valid_to` (null while live) and `retired_by` (`cousin-memory history`).

### `GET /api/memory/<slug>/review`

What the review gate holds: `{"held": [claim, ...], "batch", "operator", "is_operator"}`, `batch` being `[memory] review_batch`.

### `POST /api/memory/<slug>/review`

Body `{"verdicts": {"<id>": "keep"|"drop"}, "why"?}` (`why` at most 500 characters). The operator's verdicts (`cousin-memory review --keep/--drop`), recorded as by `console:<user>`; needs a logged-in user. A drop is an entry-level obsolete mark and has no undo, so an operator-level entry is dropped by the operator account only. Per id, an id that isn't held or may not be dropped is reported in `errors` and stays held. One or two verdicts answer at once: `200 {"ok": true, "done": {id: verdict}, "errors": {id: reason}, "effects": {"distilled"}}`, with a rebuild of the distilled views when anything was settled and a `memory-change` event. More run as the cousin's long operation of kind `memory-review` (`202 {"ok": true, "op"}`, `409` while something else runs), whose result is that same body. `400` when `verdicts` is not a non-empty map of ids to keep or drop.

### `POST /api/memory/<slug>/maintain`

Body `{"action": "distill"|"compact-raw"|"compact-index"|"reindex", "dry_run"?}`. Runs as the cousin's long operation of kind `memory-<action>` (`202 {"ok": true, "op"}`, `409` while another operation, a flip or a clean stop runs; follow it at [`GET /api/cousins/<slug>/op`](#get-apicousinsslugop)). `distill` rebuilds `memory/distilled/`, `compact-raw` folds old daily raw files (lossless), `compact-index` retires the oldest MEMORY.md pointers over the budget (`dry_run: true` only reports `would_retire`), `reindex` rebuilds the keyword index and, when configured, the semantic one. The op's result carries the library's report.

### `GET /api/memory/<slug>/portrait`

`{"candidate", "committed", "candidate_exists", "committed_exists", "backup_exists", "candidate_sha", "diff"}`: the reviewed identity layer (`self-portrait.md`), its candidate (`.self-portrait-candidate.md`) and the unified diff between them. `candidate_sha` is the first 16 hex characters of the candidate's SHA-256.

### `POST /api/memory/<slug>/portrait/synthesize`

Drafts a candidate from the cousin's own sources (no model call). A candidate that exists may hold edits: `409` unless the body says `{"replace": true}`. A candidate path that is a symlink (dangling or not) is `403`, checked before anything is written; the same holds for the candidate write and the commit. `200` with the portrait state.

### `POST /api/memory/<slug>/portrait/candidate`

Body `{"text"}` (at most 64 KiB). Replaces the candidate. `200` with the portrait state.

### `POST /api/memory/<slug>/portrait/commit`

Body `{"confirm": "<slug>", "sha": "<candidate_sha>"}`. Promotes the candidate (`cousin-self-portrait commit`; the previous portrait becomes `.self-portrait.md.bak`). The identity gate: a logged-in user (`403`), the slug typed back (`400`), and the candidate still the one read (`409` with the current `candidate_sha` when it changed). `404` no candidate.

### `GET /api/memory/<slug>/callbacks`

Query `limit` (default 200, at most 1000). `{"callbacks": [{"time", "cycle", "category", "moment"}]}`, newest first. Read-only.

### `GET /api/memory/<slug>/capsules`

Query `n` (default 50, at most 500). `{"capsules": [{"id", "timestamp", "topic", "conclusion", "evidence", "rejected", "confidence", "truth_level"}]}`, newest first. Read-only.

## Cousin files

A read-only browser over one cousin home. Same path rules as the memory explorer: `400` absolute or `..`, `403` out of the home, `404` for `.secrets/` at any depth. A link that points out of the home is listed with `outside: true` and never followed.

### `GET /api/cousins/<slug>/files`

Query `path` (a directory, default the home), `hidden=1` to include dotfiles. `{"path", "entries": [{"name", "path", "type": "dir"|"file"|"link"|"other", "size", "mtime", "outside"?, "target_type"?}], "truncated", "total"}`. One level, directories first, at most 2000 entries. `404` not a directory.

### `GET /api/cousins/<slug>/files/read`

Query `path`, `start` (1-based line, default 1), `count` (default 1000, max 5000). `{"kind": "markdown"|"text"|"image"|"binary", "path", "size", "mtime", "mime", ...}`. Markdown up to 2 MB comes back whole in `text`. Other text (and bigger Markdown) comes as `lines` from `start`, with `total_lines` and `more`. A file with a NUL or invalid UTF-8 in its first 8 KB is `binary` and has no content; images have none either (use download).

### `GET /api/cousins/<slug>/files/download`

Query `path`. Streams the bytes. Raster images inline with their type, everything else (SVG too) as an `application/octet-stream` attachment. Always `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox; default-src 'none'`.

## Shared memory review

Proposals to the [shared tier](../glossary.md#shared-tier) wait in `shared/proposed/` as `<slug>__<file>.md`. See [memory](../memory.md).

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

### `GET /api/shared/reviewers`

`{"configured", "reviewers", "error", "user", "you_review", "can_edit"}`: the list in `config/shared-reviewers.json`, whether the logged-in user is on it and may change it (resolved the way the promote check resolves names; null without a login), and `error` when the file is present but unreadable.

### `POST /api/shared/reviewers`

Body `{"reviewers": ["ana", ...]}`: replaces the list (at most 50 names of at most 64 printable characters; duplicates, case aside, are dropped), keeping the file's other keys. Who may promote is a perimeter: once the list names anyone, only a logged-in user on it may change it; while it is empty or absent any logged-in user may start it (`403` otherwise). The read, the check and the write hold a lock (`config/shared-reviewers.json.lock`), and each change is a `reviewers` row in `shared/audit.jsonl` with `reviewers` and `previous`. `409` while the file is unreadable: fix or remove it by hand. `200` with the new state.

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

Restarts the console by exiting. Answers `200 {"ok": true, "target": "console", "supervised": bool, "eta_seconds": 4}`, then exits 75 (`EX_TEMPFAIL`) about 0.6 s later: non-zero on purpose, because the shipped unit restarts on failure only. `supervised` is true when it runs under systemd (it checks `INVOCATION_ID`), whose `Restart=on-failure` brings it back, or under `cousin-supervisor` (`COUSIN_SUPERVISED`), which starts it again at once and does not count the exit against it. If it's false, nothing will start it again: restart means stop.

## System (the System view)

The supervisor, one-shot schedules, console users, backup and the install-wide config (`console/routes_system.py`, the view in `system.jsx`). Every change is a `POST`; a refusal is `{"ok": false, "error"}` with the status named below, and nothing is written.

### `GET /api/system/supervisor`

`cousin-supervisor status` over its socket: `{"ok": true, "running": true, "supervised", "pid", "started", "children": [{"name", "kind", "state", "pid", "restarts", "since", "reason", "last_exit", "actions": {"start", "stop", "restart", "confirm"?, "why"?}}]}`. `running` is false when no supervisor runs for the root, null when one took the connection but did not answer; `reason` says which. `supervised` is true when the console itself is a supervisor child.

### `POST /api/system/supervisor/start`

`{"child": "loops" | "runner:<slug>"}`: the supervisor's `start`. A runner cousin is held exclusively for the call, as the fleet's start does (`409` while a flip or an operation runs on it). `200` the supervisor's answer; `400` the console (it restarts through `POST /api/admin/restart/framework`, never stops from its own page) or a bridge (`telegram:<slug>` follows its runner); `404` no such cousin; `409` the supervisor refused, with its reason; `503` no supervisor.

### `POST /api/system/supervisor/stop`

As start, with the supervisor's `stop`, not waiting (`state: "stopping"`), `by: "console <user>"`. The loops daemon needs `"confirm": "loops"` (`400` without): stopping it stops every heartbeat, loop and scheduled prompt.

### `POST /api/system/supervisor/reload`

The supervisor's rescan: `200 {"ok": true, "added", "removed"}`.

### `GET /api/system/schedules`

Every cousin's pending one-shots (`cousin-schedule`), oldest first: `{"ok": true, "schedules": [{"id", "cousin", "target_ts", "when", "prompt", "status"}]}`. `?all=1` adds fired and cancelled ones, newest first, 200 at most.

### `GET /api/cousins/<slug>/schedules`

The same for one cousin (`?all=1` for its history, 100 at most).

### `POST /api/cousins/<slug>/schedules`

`{"when": "in 30m" | "tomorrow 06:30" | "YYYY-MM-DDTHH:MM", "prompt"}`: `schedule.add`. `201 {"ok": true, "schedule"}`; `400` a time that does not parse or is past, an empty prompt, one over 8000 characters, or a cousin that already has 20 pending (the cap).

### `POST /api/cousins/<slug>/schedules/<id>/cancel`

`200 {"ok": true, "id", "status": "cancelled"}`; `404` no pending schedule with that id for that cousin.

### `GET /api/system/users`

`{"ok": true, "configured", "users": [names], "me"}`. Never a hash or a salt.

### `POST /api/system/users`

`{"name", "password"}`: a new console user. The name is taken as `cousin-console adduser` takes it (any non-empty string without NUL, at most 1024 characters, kept exactly; percent-encode it in a path); the password is 8 to 1024 characters, kept as typed. `201 {"ok": true, "user", "users"}`; `409` the user exists. The first user closes the console to everyone without a session, so that request is also logged in as it (`Set-Cookie`, `"logged_in": true`). The password is never returned or logged.

### `POST /api/system/users/<name>/password`

`{"password"}`: reset another user's password (8 to 1024 characters); their sessions end. `400` your own name (that goes through `POST /api/auth/change-password`, which asks for the current one); `404` no such user.

### `POST /api/system/users/<name>/remove`

`{"confirm": "<name>"}`, the name typed again. `400` without it or for your own user; `409` the last user (the console would have none); `404` no such user. Their sessions end at their next request.

### `POST /api/system/backup`

`{"dest": "/abs/dir", "slugs": ["wren"] | "all"}`: `cousin-backup` for each cousin, each a long operation (`kind: "backup"`, `GET /api/cousins/<slug>/op`, the `cousin-op` event) and a `backup` row in the jobs store. `dest` must be absolute, an existing directory the console can write, and neither it nor any `<dest>/<slug>` may resolve (symlinks followed) inside the install root at all (`400` otherwise). `<dest>/<slug>` is created `0700` before the snapshot (never through a link, and never when the cousin's own `cousin.toml` names another slug: that op fails and says so), the snapshot is written into exactly that directory (`backup.snapshot(home, target=...)`), and every directory under it ends `0700`, every file `0600`. A copy that resolves anywhere else, or a `<dest>/<slug>` replaced during the copy, fails the op with the exact path where the copy landed, removes nothing, and appends `{"action": "backup-misplaced", "user", "cousin", "expected", "landed"}` to `data/system/audit.jsonl`. A destination other users can write is refused unless it is sticky. A race that swaps `<dest>/<slug>` for a link between the checks and the copy is out of scope: winning it takes write access to the destination, so the operator's own uid, which can already read and write the homes and `config/`; the check after the copy reports it. `202 {"ok": true, "dest", "ops": {slug: op}, "busy": {slug: reason}}`; `409` when every cousin asked for is busy. A snapshot lands in `<dest>/<slug>/<YYYY-MM-DD>/`.

### `GET /api/system/config`

`{"ok": true, "files": {...}}`: the install config files by name, each `{"path", "exists", "error", "applies", "restart"}` plus its values:

- `media`: `kinds.<image|voice|video>` = `{url, model, timeout_s, key_file, key: {set, last4, error}}` or null; `key` is read as media reads it (a plain read under the root);
- `embedding`, `hive`: `values` by key (`recall.min_score` for a subtable key);
- `peers`: `peers.<slug>` = `{url, send_path, name, sender, reach, token_file, inbound_token_file, token, inbound_token, shadowed, unknown_reach}`; a token is read as `chat.read_secret` reads it, so a file group or others can read shows its refusal in `error`;
- `outbound_filter`, `law`: `{content, sha}`;
- `allowlist`: `{allow, sha, client, builtin}`, and `restart` naming the console;
- `commands`: `worker-cmd` and a leftover `agent-cmd` (no runner kind reads it) as `{path, exists, content}`, shown only: no route writes them.

A secret's value is never in the answer: `{set, last4, error}` only, `last4` for a value of 16 characters or more. A key or token file outside `config/` is never read (`set: null`).

### `POST /api/system/config/<name>`

`<name>` is `media`, `embedding`, `hive` or `peers`. `{"changes": [{"table", "key", "value"} | {"table", "key", "remove": true}], "remove_tables": [...]}`: `table` is `""` for a top-level key, `image`/`voice`/`video` for media, `recall` or `options` for embedding, `peers.<slug>` for a peer. A key is removed only by `"remove": true`: a missing, `null`, empty or non-finite value is `400`, so a mistyped number never deletes the key. A table in `remove_tables` that the file defines without a `[table]` header of its own (an inline table, dotted keys) is `400`: remove it by hand. Only the keys the editor lists are accepted (`url`, `model`, `timeout_s` for media; the documented keys for the others); a path to a secret (`key_file`, `token_file`, `inbound_token_file`) is never set here. The edited text is checked by the file's own loader (`hive.hive_config`, `chat.load_external_peers`, the embedding reader, which needs `url`) and written through `toml_edit`, every other line kept, the file created `0600` when absent. `200 {"ok": true, "file"}`; `400` with the loader's reason; `404` another name.

### `POST /api/system/config/<name>/<target>/secret`

`{"value", "which"?}`: a media kind's key (`<name>` `media`, `<target>` the kind) or a peer's token (`peers`, the peer's slug, `which` `outbound` or `inbound`). Written by `secrets.write_secret_file` to `config/media-keys/<kind>.key` or `config/peer-tokens/<slug>[.inbound].token` (`0600` in a `0700` directory), then `key_file`, `token_file` or `inbound_token_file` points at it. When that TOML write fails, the secret file is put back as it was. `200 {"ok": true, "secret": {set, last4, error}}`; `400` a value that is not one printable line; `404` a peer that is not in the file.

### `POST /api/system/config/<name>/<target>/secret/clear`

Removes the key from the file and deletes the secret file when it is the one this route writes.

### `POST /api/system/outbound-filter`

`{"content", "base_sha"}`: the whole of `config/outbound-filter.json`, refused (`400`) unless it is a JSON object whose `terms`, `protected` and `trusted_peers` are lists of non-empty strings and whose `surfaces` maps each name to an object with an `add` list of them: the loader reads an unparsable file as an inert filter and a string as its characters. Keys it ignores are kept. `409` when the file changed since `base_sha` was read. The old file is copied to `data/config-backups/` first (the last 20 kept). `200 {"ok": true, "backup", "sha"}`.

### `POST /api/system/law`

`{"content", "base_sha"}`: `config/law.md`, the same way (256 KiB at most). Each cousin reads it into its boot packet at its next start or flip.

### `POST /api/system/allowlist`

`{"allow": [cidr], "base_sha"}`: `config/net-allowlist.json`'s `allow`, every other key kept. Each entry must be a network the guard reads (`192.0.2.0/24`, not `192.0.2.1/24`) and not `/0`; `400` when the new list would no longer admit the requesting address. The console reads it when it starts: the answer's `restart` names it and the console's restart route.

### `GET /api/system/agent-defaults`

`config/harness.toml [agent]`: `{"values": {"default_model", "default_effort", "commit_attribution": {"value", "source"}}, "choices": {"effort", "models"}, "exists", "error", "applies"}`. `source` is `config/harness.toml [agent]`, the built-in default (`commit_attribution` true) or unset.

### `POST /api/system/agent-defaults`

Any of `default_model` (one word, as the agent command renders it), `default_effort` (one of the levels), `commit_attribution` (a boolean), and `"remove": [keys]` to drop keys; a `null` or empty value is `400`. Checked by `config.agent_config`, `commit_attribution` and `harness_config` on the edited text, then written through `toml_edit`. `200` the new values; `400` a bad value; `409` no `config/harness.toml` (the route never creates it). A cousin reads these when it starts or is spawned.

## `GET /api/events`

The live stream every open tab holds. Server-sent events, each frame a `data:` line with `{"kind": "...", "data": ...}`. The first frame is always a `snapshot`. A `: ping` comment goes out after 25 s of silence.

The events come from two places: route handlers announce what they just did, and a poller inside the console re-reads the stores and sends the differences (jobs, timed flip requests, the tracker and meetings every 2 s; the fleet, the loops and the fire times every 15 s). If a tab falls behind by 200 frames it's dropped; it reconnects and gets a fresh snapshot, so nothing is lost.

| kind | data | sent when |
|---|---|---|
| `snapshot` | `{"cousins", "loops", "jobs", "daemon"}` | on connect; the same rows the GET routes return |
| `cousins-refresh` | `[row]` | every fleet poll, and after spawn, identity edits and hive changes |
| `loops-refresh` | `[row]` | every loops poll, and after a loops save |
| `cousin-status` | `{"slug", "status", "error"?}` | `starting` when a start or restart begins, then `start failed` (with `error`) when the start is refused, or `started` when a restart's background start succeeds; `stopping` when a stop, restart or dismiss begins, then `stop failed` when the stop is refused |
| `job-add`, `job-update` | job row | a job appeared or changed |
| `job-delete` | `{"id"}` | a job went away |
| `cousin-flip` | `{"slug", "phase", ...}` | see below |
| `cousin-op` | `{"slug", "id", "kind", "phase": "started" \| "stage" \| "done" \| "failed", "stage"?, "error"?}` | a long operation started, reported a stage, or finished (`GET /api/cousins/<slug>/op`) |
| `loop-fire` | `{"cousin", "loop", "ts"}` | a loop's last fire time moved forward |
| `tracker-change` | `{"id", "op": "add" \| "update" \| "delete"}` | a tracker item changed, through the console or anything else |
| `memory-change` | `{"slug", "action", ...}` | a memory change through the console: `trash` (a delete) and `restore` (with `id`), `obsolete`, `remember` and `decide` (with `topic`), `review` (verdicts settled), a maintain action by its name (`distill`, `compact-raw`, `compact-index`, `reindex`), `portrait-synthesize` and `portrait-edit`, and `portrait-commit` (with `by`) |
| `meeting-change` | `{"id", "op": "open" \| "post" \| "skip" \| "delete" \| "close" \| "update"}` | a meeting opened, got a post or a skip, was deleted or closed through the console; `update` when the poller (every 2 s) sees a meeting's row or entry count change, as when a cousin speaks through the CLI |
| `accounts-change` | `{"name", "what": "added" \| "edited" \| "removed" \| "key"}` | an account entry or key changed through the console (never the key) |

`cousin-flip` phases: `scheduled` (with `fire_at`, and `delay_seconds` or `request_id`), `started`, `complete` (with `ok: true`, and `new_generation`, `boot_packet_tokens`, `degraded_sections` when the console ran it), `failed` (with `ok: false` and `error`), `cancelled`. Timed flips the daemon runs show up through the poller: a request that turns `done` is `complete`, `failed` or `expired` is `failed`.

## Static files

`GET /` serves `index.html`, and `GET /<file>` serves files from `cousin_lib/console_static/` with one of these suffixes: `.html .jsx .js .css .json .webmanifest .svg .png .ico`. Anything that resolves outside that directory is `403`, anything else missing is `404`. Everything is `Cache-Control: no-store`, and `index.html` gets `?v=<mtime>` stamped on its local `src`/`href` links, so a redeploy shows up on the next reload. The page loads React, Babel, marked, mermaid and xterm from a CDN; that's the only outside fetch.

Non-GET requests outside `/api/`, `/hive/`, `/peer/` and `/plugins/` are `404`.

## Hive (remote cousins)

These only work when `config/hive.toml` has `enabled = true`. Otherwise `GET /api/hive` says `{"enabled": false}` (plus `error` if the file is broken) and the others are `404`. The node side, everything under `/hive/`, is in [the hive API](hive-api.md): those routes skip the network guard and the login and use the hive token instead. No `/api/` route looks at an `Authorization` header, so a hive token opens nothing here.

### `GET /api/hive`

`{"enabled": false}`, or `{"enabled": true, "public_url", "checkin_seconds", "home_cousin", "default_port": 8210}` (`home_cousin` null when unset). The spawn dialog only offers "Remote" when it's enabled.

### `GET /api/hive/nodes`

`{"nodes": [row]}`: the remote rows on their own, same shape as in `GET /api/cousins`.

### `POST /api/hive/nodes`

Build a remote cousin. Body:

```json
{"slug": "kestrel", "name": "Kestrel", "role": "watches the garage box",
 "port": 8210, "brain": "placeholder", "agent_cmd": "",
 "home_chat": false, "reachable": true}
```

`slug` must not be a local cousin (`409`). `role` is required, up to 500 characters; `name` up to 64. `brain` is `placeholder` or `agent`, and `agent` needs `agent_cmd`, the command line the node runs with the prompt on stdin. `home_chat` sets `TELL_HOME=1` in the node's `node.env`, so its `[tell-home: ...]` reaches `home_cousin` through this queen (`400` if hive.toml names no `home_cousin`; the legacy `home_chat_url` is not read). `reachable` (default true) binds the node's chat server on 0.0.0.0 so the console can reach it; false keeps it on 127.0.0.1.

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

## External peers

### `POST /peer/send`

Another install's cousin writes to one of these cousins. Outside `/api/`: no console session is read, and a session or a bearer token opens nothing here. The network guard applies. `Authorization: HMAC <sender>:<hex>`: `<sender>` names an entry of `config/external-peers.toml`, and `<hex>` is the HMAC-SHA256, keyed with that entry's `inbound_token_file`, of the lines `<sender>`, `<to>`, `<sent_at>` to three decimals, `<msg_id>` and the hex sha256 of the message (`chat.peer_signature`). The entry is the sender; the secret never travels.

```json
{"to": "wren", "message": "the greenhouse report is ready", "msg_id": "kestrel-7f3a2c1b", "sent_at": 1790000000.5}
```

`to` is one of the peer's `reach` and a local, peer-visible cousin. `msg_id` is 8-128 letters, digits, `-` or `_`; `sent_at` is finite epoch seconds within 300 s of this host's clock. The message, stripped of control characters, is stored in that cousin's chat under the peer's `name` and delivered to it. `200 {"ok": true, "to", "id"}`; `401 {"error": "unauthorized"}` for every refusal before the signature verifies, one answer for all of them: no signature, a wrong one, not a peer's, a body that is not a JSON object, an unusable `config/external-peers.toml` or secret file (the console's log says which); after it, `400` bad body, stale, future or non-finite `sent_at`, empty or overlong (16000) message; `403` a peer `name` the cousin would take for its operator or a local cousin, or one the framework reserves for its own senders (`fw-hook`, `runner`, `framework`, `unknown`, `system`, `schedule`); `404` outside the peer's `reach`, no such cousin, or one that is not peer-visible (one answer for all three); `409` that peer already delivered that `msg_id` (kept 15 minutes); `413` a body over 64 KiB; `429` past 30 messages a minute from one peer; `502` the delivery failed (the id is freed for a retry); `503 {"error": "external peers unavailable"}` when the signed entry is unusable: no `reach`, or a local cousin's slug (the console's log says why); `504` the delivery timed out (the id is kept: it may have landed). The receiving cousin's chat hooks run in the console's process for this route.

## Routes the older console had

If you're coming from the older console these routes are gone, and nothing in this one calls them: `/api/agents`, `/api/agents/<cousin>/<id>/log`, `/api/backlog`, `/api/backlog/<id>`, `/api/chat/presence`, `/api/chat/engagement`, `/api/chat/audio/...`, `/api/chat/image/...`, `/api/chat/video/...`, `/api/cousins/<slug>/budget`, `/api/peer-messages`, `/api/sidebar`, `/api/liveness`, `/api/network-devices`, `/api/logs`, `/api/admin/restart/cousin/<slug>` (use `POST /api/cousins/<slug>/restart`) and `POST /api/jobs` (jobs are created with `cousin-job`).
