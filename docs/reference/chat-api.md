# Chat server API

The HTTP API of the per-cousin chat server, for when you're writing a client, a bridge or a script that talks to one cousin directly. For how chat works day to day, read [the chat guide](../chat.md).

Every cousin with a `[chat] port` runs its own chat server:

```sh
cousin-chat-server --home cousins/wren
cousin-chat-server --home cousins/wren --no-terminal-delivery   # store only, never type into tmux
```

It reads `cousin.toml` for the slug, display name, port, `[chat] host` (bind address, default `127.0.0.1`), `[chat] tmux_session` (default the slug) and `[operator] name`, which is you. It won't start without a port, when the port can't be bound, or, with terminal delivery on, when there's no `tmux` on the PATH. `COUSIN_TMUX_SOCKET` in its environment picks a tmux socket. The console, `cousin-chat`, `cousin-reply` and the Telegram bridge are all plain HTTP clients of this server.

## Who can call it

Every request first goes through the same network guard as the console: loopback, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, plus the ranges in `config/net-allowlist.json`. Anything else is `403 {"error": "address not allowed"}`, sent with a CORS header so a browser shows the 403.

That's the only check. There is no login, and `user` on a send is whatever the caller says it is. Don't expose a chat server to a network where you don't trust every machine.

## Conventions

- JSON responses with `Cache-Control: no-store` and `Access-Control-Allow-Origin: *`.
- A body that isn't a JSON object is `400 {"error": "malformed JSON body"}` or `400 {"error": "JSON body must be an object"}`. Other client errors are `400 {"error": "..."}` too.
- Unknown POST paths are `404 {"error": "not found"}`. Unknown GET paths fall through to the static files.

## The message row

Messages live in `<home>/data/chat.db` (SQLite, WAL). Every route that returns messages returns rows like this:

```json
{"id": 412, "chat_user": "ana", "user": "ana", "message": "morning",
 "timestamp": "2026-09-18T07:02:11.402113+00:00", "type": "user",
 "archived": 0, "reply_to": null, "reply_to_user": null,
 "attachment_kind": null, "attachment_path": null,
 "reactions": [{"user": "ana", "emoji": "+1", "tap_count": 2}]}
```

| field | meaning |
|---|---|
| `id` | increasing integer, the paging cursor |
| `chat_user` | the thread key: the other party's name, lowercased, spaces turned into `_`. `Ana Lima` and `ana lima` are one thread |
| `user` | who wrote it (display name) |
| `type` | `user` for inbound messages, the cousin's slug for its own replies |
| `timestamp` | UTC ISO 8601 |
| `archived` | 0 or 1 |
| `reply_to` | whatever JSON the client sent as `reply_to`, stored as a string, untouched |
| `reply_to_user` | on a reply, who it was addressed to |
| `attachment_kind`, `attachment_path` | on a reply with media: `image`, `voice` or `video`, and the absolute path of the file |
| `reactions` | added by history; search rows don't carry it |

A thread is everything between the cousin and one other party, in both directions: your messages are stored under your name, the cousin's replies to you under `reply_to_user`, which is also you.

## `GET /health`

`200 {"status": "ok", "slug": "wren", "port": 8611}`. The console uses this for the `chat` column.

## `POST /api/send`

An inbound message for the cousin.

```sh
curl -s localhost:8611/api/send -H 'Content-Type: application/json' \
  -d '{"user": "ana", "message": "can you check the backups?"}'
```

Body: `user` and a non-empty `message` (both required), plus optional `reply_to` (any JSON) and `image` (a `data:image/<type>;base64,...` URI). Answers `200 {"ok": true, "id": 413, "timestamp": "..."}` once the row is stored. It doesn't wait for the terminal.

What happens, in order:

1. The row is stored (`type: "user"`, thread = `user`).
2. If `user` is you (`[operator] name`), the message is checked for corrections ("stop", "don't", "instead", ...) and any hit goes to `data/corrections.jsonl`. Failures here never fail the send.
3. If there's an `image`, it's decoded to `<home>/chat/inbound/<id>.<ext>` (png, jpg, jpeg, gif or webp; any other type is saved as `.bin`). A bad image doesn't fail the send either; the line says `[image attached, decode failed]` instead.
4. The delivery line is composed and typed into the tmux session on a background thread (see below). If the message is from you (`[operator] name`) it may get a memory recall suffix.
5. `data/.last-user-msg` is touched. Its mtime is the "time since last message" for the next delivery.
6. Chat hooks from `<home>/chat-hooks.json` run (see below).

## `POST /api/<slug>_reply`

The cousin's own outbound message. The slug is in the path so a reply sent to the wrong cousin's server gets a `404` instead of landing in someone else's history. `cousin-reply` is the normal way to call it.

```sh
echo "backups are fine" | cousin-reply --user ana
```

Body:

```json
{"message": "here's the chart", "reply_to_user": "ana", "reply_to": {...},
 "attachment": {"kind": "image", "path": "/srv/cousins/wren/chat/images/chart.png"}}
```

`reply_to_user` is required; there is no default recipient. You need a non-empty `message` or an `attachment` with both `kind` and `path` (a caption-less picture is fine). The row is stored under the recipient's thread with `user` = the cousin's display name and `type` = the slug. Nothing is typed into the pane. `200 {"ok": true, "id", "timestamp"}`.

The server stores the attachment path as given; it doesn't check the file. The console only serves attachments that sit directly in `chat/images/`, `chat/audio/` or `chat/video/` of the home. Generated media uses this field. `cousin-reply --image` works differently: it posts the reply, then copies the picture to `chat/inbound/<reply id>.<ext>`, which the console also shows on that message. `cousin-reply` defaults `reply_to_user` to `[operator] name` and sends `--reply-to N` as `{"id": N}`.

## `GET /api/history`

One thread, oldest first.

| param | meaning |
|---|---|
| `user` | required, the other party's name |
| `limit` | default 200 |
| `before` | page backward: the newest `limit` rows with `id < before` |
| `since` | poll forward: the oldest `limit` rows with `id > since` |
| `archived` | `0` (default, live rows), `1` (archived only), `all` |

With neither `before` nor `since` you get the newest `limit` rows. If you pass both, `since` wins. Non-integer numbers are `400`.

Answer: `{"messages": [...], "total": n, "has_more": bool}`. `total` counts the whole thread (with the archive filter). `has_more` is counted, not guessed: whether there are more rows in the direction you're paging. To scroll back, pass the smallest `id` you have as `before`; to poll for new ones, pass the largest as `since`.

## `GET /api/search`

`q` (required) is matched as a substring against the message text and the `reply_to` JSON. `user` narrows to one thread; without it all threads are searched. `archived` works as in history. Answer: `{"messages": [...]}`, newest first, at most 50.

## `POST /api/reactions`

```json
{"message_id": 412, "user": "ana", "emoji": "+1", "action": "tap"}
```

All four are required. `message_id` must be an integer, `action` `tap` or `remove`. A tap on a reaction you've already made bumps its `tap_count` rather than taking it away (tapping three times means "really"); only `remove` deletes. Answer:

```json
{"message_id": 412, "op": "added" | "bumped" | "removed",
 "reactions": [{"user": "ana", "emoji": "+1", "tap_count": 1}]}
```

An add or a bump also types a line into the cousin's pane:

```
[fw-reaction] msg-id=412 emoji=+1 user=ana tap_count=2 op=bumped
```

## `POST /api/archive`

`{"user": "ana", "keep": 20}`. Archives every live row of that thread except the newest `keep` (default 0, must be a non-negative integer). Answer `{"ok": true, "archived": n}` with the number of rows that actually changed. Archived rows are still there; ask for them with `archived=1` or `all`.

## Static files

Any other GET serves a file from `<home>/www/`, if the cousin has one. Allowed suffixes: `.html .css .js .png .jpg .jpeg .gif .webp .svg .ico`. The resolved path has to stay inside `www/` (links and `..` are resolved first). Anything else is `404`.

## What the cousin sees

An inbound message becomes one line typed into the cousin's tmux session:

```
[now: 2026-09-18 07:02 UTC | dt-since-msg: 12m] (Chat ana): can you check the backups?
```

- `dt-since-msg` is the minutes since the previous inbound message, from the mtime of `data/.last-user-msg`. On the first message ever it's left out.
- Newlines in the message are flattened to spaces. A newline would submit the paste halfway.
- An inbound image adds `[image attached -> Read /path/to/cousins/wren/chat/inbound/413.png]`, so the cousin can open it.
- Messages from another cousin arrive the same way, `(Chat Kestrel): ...`. That's how a cousin tells you from a peer: your name is `[operator] name`.

The typing itself: the line is pasted with `send-keys -l` (a line over 12000 bytes goes through `load-buffer` and `paste-buffer` instead, since tmux refuses a command over its ~16 KB message size), the server waits a moment scaled to the length (0.1 s plus a bit per character, at most 0.6 s) so the terminal takes the whole paste, sends Enter, then checks the bottom three lines of the pane. If the text is still sitting in the input box it sends Enter once more. One lock covers all typing in the process, so two messages (or a message and a reaction line, or a keystroke from the console's pane) never interleave.

Two guards from `config/harness.toml` run before anything is typed:

- `attention_patterns`: if the pane shows one of these strings (a login or trust menu, say), the line is not typed at all. Typing into a menu picks options. The server logs `tmux delivery SKIPPED` to stderr.
- `[input_mode]` with `normal_marker` and `insert_keys`: if the pane shows the marker (a modal editor in command mode), the insert keys are sent first so the text lands as text.

Delivery is best effort. The send answers once the row is stored; if typing fails (session gone, tmux down) the server logs `tmux delivery FAILED` to stderr and the sender never hears about it. The message is still in the history. Check the chat server's log if a cousin seems deaf.

## Memory recall on delivery

When the sender is you (`[operator] name`) and the message is long enough, the server searches the cousin's own memory and appends one suffix to the typed line:

```
[fw-recall] possibly relevant from your memory: Backup schedule (memory:backups.md); Disk layout (notes:2026-08-01-disks.md) - cousin-memory search for details; ignore if not.
```

Titles and paths only, never file contents. The stored message never gets the suffix. The knobs:

| where | key | default |
|---|---|---|
| `config/embedding.toml [recall]` | `min_chars` | 24 |
| | `min_score` (semantic similarity a hit needs) | 0.45 |
| | `top` | 3 |
| `cousin.toml [memory]` | `proactive_recall` | true |
| | `recall_keyword_only` | false |

Without `config/embedding.toml` there's no semantic score, so nothing is appended unless the cousin sets `recall_keyword_only = true`, in which case any keyword hit counts. The search runs before the send answers, so a slow embedding service slows the send down (by at most the embedding timeout). Any error means the line goes out without a suffix.

## Chat hooks

`<home>/chat-hooks.json` lets a cousin react to a kind of message without having to notice it every time:

```json
[
  {"pattern": "(?i)\\bprice\\s+check\\b", "user": "ana",
   "handler": "shell:scripts/quote-prices.sh", "desc": "quote the tariff"},
  {"pattern": "(?i)\\bdebug\\s+health\\b", "user": "*",
   "handler": "inject:[fw-hook] consider running the health check"}
]
```

`pattern` is a Python regex searched in the message. `user` is compared case-insensitively; `*` or leaving it out matches anyone. `desc` is ignored. Every matching entry fires, in file order, after the message is stored and its line typed.

- `shell:<path>` runs the script detached, working directory the home, with `COUSIN_HOOK_USER`, `COUSIN_HOOK_MESSAGE`, `COUSIN_HOOK_PATTERN`, `COUSIN_SLUG` and `COUSIN_HOME` set. Output goes to `<home>/data/chat-hooks.log`. A relative path is taken from the home. The script has to be inside the home or the framework root, otherwise it's refused with a line on stderr.
- `inject:<text>` types the text as its own line, `(Chat fw-hook): <text>`, right after the message.

A missing or broken file, a bad regex or a failing script never affects the send. Hook lines aren't stored in the history.

## Files a chat server writes

| path | what |
|---|---|
| `data/chat.db` | the history and reactions |
| `data/.last-user-msg` | touched on every inbound message |
| `data/corrections.jsonl` | corrections spotted in operator messages |
| `data/chat-hooks.log` | output of shell hooks |
| `chat/inbound/<id>.<ext>` | inbound images. Nothing cleans this up; it's the cousin's inbox |

## Remote cousins

A hive node runs a smaller chat server of its own with only `/health`, `/api/send` and `/api/history`, and asks for its hive token from anything that isn't loopback. See [the hive API](hive-api.md#the-nodes-own-endpoints).
