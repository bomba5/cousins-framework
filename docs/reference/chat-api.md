# Chat API

The calls that read and write a [cousin](../glossary.md#cousin)'s chat history, for when you're writing a client, a bridge or a script. For how chat works day to day, read [the chat guide](../chat.md).

## Where it runs

No cousin on this machine runs a chat server or has a port. The console, `cousin-chat`, `cousin-reply` and the Telegram bridge make these calls in-process, through `cousin_lib/server/chat_api.py` (`history`, `search`, `send`, `reply`, `archive`, `react`), over the cousin's own `data/chat.db`. Each takes the body or query described below and gives back the JSON described below, or the `400` text. A script on this machine goes through `cousin-chat` or the console's chat routes ([the console API](console-api.md)), which answer with these calls.

A local cousin with no `[agent] runner` has nothing to deliver to: 2.0.0 has no legacy tmux [lane](../glossary.md#lane). It is refused by name, and `cousin-chat send` to it exits 1.

Over HTTP, this is the API a hive node's own chat server answers (`cousin_node.py`, in the node's archive), and the one the console proxies to for a remote node. A node answers only `GET /health`, `POST /api/send` and `GET /api/history`. Search, reactions, archive and replies have no route on a node; they are in-process calls here. The rest of the node is in [the hive API](hive-api.md#the-nodes-own-endpoints).

## Who can call it

A node's chat server binds to `NODE_HOST` (`127.0.0.1` by default). A caller on loopback can use `/api/send` and `/api/history` freely. Any other caller has to send the node's own hive token as `Authorization: Bearer <token>`, or it gets `401 {"error": "unauthorized"}`. The console, as the queen, holds that token and sends it on every proxied call. `/health` is open.

On this machine there is no port to call. The in-process calls trust their caller: `user` on a send is whatever the caller says it is. The console's chat routes sit behind its login and its network guard.

## Conventions

- A node answers JSON with `Cache-Control: no-store` and `Access-Control-Allow-Origin: *`.
- A body that isn't a JSON object is `400 {"error": "malformed JSON body"}` or `400 {"error": "JSON body must be an object"}`. Other client errors are `400 {"error": "..."}` too, on a node and in-process alike.
- On a node, any other path is `404 {"error": "not found"}`.

## The message row

On this machine messages live in `<home>/data/chat.db` (SQLite, WAL). Every call that returns messages returns rows like this:

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
| `chat_user` | the [thread](../glossary.md#thread) key: the other party's name, lowercased, spaces turned into `_`. `Ana Lima` and `ana lima` are one thread |
| `user` | who wrote it (display name) |
| `type` | `user` for inbound messages, the cousin's slug for its own replies |
| `timestamp` | UTC ISO 8601 |
| `archived` | 0 or 1 |
| `reply_to` | whatever JSON the client sent as `reply_to`, stored as a string, untouched |
| `reply_to_user` | on a reply, who it was addressed to |
| `attachment_kind`, `attachment_path` | on a reply with media: `image`, `voice` or `video`, and the absolute path of the file |
| `reactions` | added by history; search rows don't carry it |

A thread is everything between the cousin and one other party, in both directions: your messages are stored under your name, the cousin's replies to you under `reply_to_user`, which is also you.

A node keeps its messages in `data/chat.jsonl` beside it, in the same shape. There, `reply_to` and the attachment fields are always null, `reactions` is always empty and nothing is ever archived.

## `GET /health`

A node only. `200 {"status": "ok", "slug": "kestrel", "port": 8210, "brain": "placeholder"}`; `brain` is `agent` when the node has an `AGENT_CMD`. The node's `install.sh` waits on it.

## Send

An inbound message for the cousin: the in-process `send`, or `POST /api/send` on a node.

```sh
# on the node itself
curl -s 127.0.0.1:8210/api/send -H 'Content-Type: application/json' \
  -d '{"user": "ana", "message": "can you check the backups?"}'
```

Body: `user` and a non-empty `message` (both required), plus optional `reply_to` (any JSON) and `image` (a `data:image/<type>;base64,...` URI). A node reads only `user` and `message`. The answer is `200 {"ok": true, "id": 413, "timestamp": "..."}` once the row is stored. It doesn't wait for the cousin.

On this machine, before any of that, every send checks whether the message is a login
code a running `cousin-account login|token --via <this cousin>` is waiting
on (R18). When it is, the send answers `200 {"ok": true, "id", "timestamp",
"diverted": true}` and delivers nothing: no chat hook ever sees it. The row
stored in `chat.db` is a redaction line
(`[login code received for account <name>]`, or, for a second or late code,
`[a late login code for account <name> was discarded: ...]`), never the code
itself.

What happens on an ordinary (non-diverted) send on this machine, in order:

1. The row is stored (`type: "user"`, thread = `user`).
2. If there's an `image`, it's decoded to `<home>/chat/inbound/<id>.<ext>` (png, jpg, jpeg, gif or webp; any other type is saved as `.bin`). A bad image doesn't fail the send; the cousin is handed `[image attached, decode failed]` in place of the path.
3. The message is delivered: a `chat` item on the sender's thread goes into the cousin's [inbox](../glossary.md#inbox), with the image's path as an attachment. The send doesn't wait for it. Nothing is appended to the message; recall is the [runner](../glossary.md#runner)'s own prompt hook.
4. If `user` is you (`[operator] name`), the message is checked for corrections ("stop", "don't", "instead", ...) and any hit goes to `data/corrections.jsonl`. Failures here never fail the send.
5. Chat hooks from `<home>/chat-hooks.json` run (see below).

On a node the row is stored and the answer goes back at once. The node's brain runs the [turn](../glossary.md#turn) on a background thread, and its reply shows up in the history. A node has no login codes, no corrections and no chat hooks.

## Reply

The cousin's own outbound message. `cousin-reply`, the media `chat` commands and the runner's `reply` tool store it in-process (`chat_api.reply`). No route takes it: a node stores its brain's replies itself.

```sh
echo "backups are fine" | cousin-reply --user ana
```

Body:

```json
{"message": "here's the chart", "reply_to_user": "ana", "reply_to": {...},
 "attachment": {"kind": "image", "path": "/srv/cousins/wren/chat/images/chart.png"}}
```

`reply_to_user` is required; there is no default recipient. You need a non-empty `message` or an `attachment` with both `kind` and `path` (a caption-less picture is fine). The row is stored under the recipient's thread with `user` = the cousin's display name and `type` = the slug. It is never delivered back to the cousin. The answer is `{"ok": true, "id", "timestamp"}`.

The attachment path is stored as given; nothing checks the file. The console only serves attachments that sit directly in `chat/images/`, `chat/audio/` or `chat/video/` of the home. Generated media uses this field. `cousin-reply --image` works differently: it stores the reply, then copies the picture to `chat/inbound/<reply id>.<ext>`, which the console also shows on that message. `cousin-reply` defaults `reply_to_user` to `[operator] name` and sends `--reply-to N` as `{"id": N}`.

## History

One thread, oldest first: the in-process `history`, or `GET /api/history` on a node.

| param | meaning |
|---|---|
| `user` | required, the other party's name |
| `limit` | default 200 |
| `before` | page backward: the newest `limit` rows with `id < before` |
| `since` | poll forward: the oldest `limit` rows with `id > since` |
| `archived` | `0` (default, live rows), `1` (archived only), `all`. Not on a node |

With neither `before` nor `since` you get the newest `limit` rows. If you pass both, `since` wins. Non-integer numbers are `400`.

Answer: `{"messages": [...], "total": n, "has_more": bool}`. `total` counts the whole thread (with the archive filter). `has_more` is counted, not guessed: whether there are more rows in the direction you're paging. To scroll back, pass the smallest `id` you have as `before`; to poll for new ones, pass the largest as `since`.

## Search

In-process only. `q` (required) is matched as a substring against the message text and the `reply_to` JSON. `user` narrows to one thread; without it all threads are searched. `archived` works as in history. Answer: `{"messages": [...]}`, newest first, at most 50.

## Reactions

In-process only (`react`).

```json
{"message_id": 412, "user": "ana", "emoji": "+1", "action": "tap"}
```

All four are required. `message_id` must be an integer, `action` `tap` or `remove`. A tap on a reaction you've already made bumps its `tap_count` rather than taking it away (tapping three times means "really"); only `remove` deletes. Answer:

```json
{"message_id": 412, "op": "added" | "bumped" | "removed",
 "reactions": [{"user": "ana", "emoji": "+1", "tap_count": 1}]}
```

An add or a bump also tells the cousin: a `reaction` item with this line goes into its inbox, on its `system` thread:

```
[fw-reaction] msg-id=412 emoji=+1 user=ana tap_count=2 op=bumped
```

## Archive

In-process only. `{"user": "ana", "keep": 20}`. Archives every live row of that thread except the newest `keep` (default 0, must be a non-negative integer). Answer `{"ok": true, "archived": n}` with the number of rows that actually changed. Archived rows are still there; ask for them with `archived=1` or `all`.

## What the cousin sees

A delivered message is a row in the cousin's inbox (`data/inbox.db`). The runner claims it and writes it into a turn under a one-line header with the thread, the kind of item and the sender, `[operator:ana] chat from ana at 2026-09-18 07:02 UTC`. Messages from another cousin arrive the same way, on a `peer:<slug>` thread. That's how a cousin tells you from a peer: your name is `[operator] name`. An inbound image is handed on as its path; what each runner kind does with it is in [the runners reference](runners.md).

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

`pattern` is a Python regex searched in the message. `user` is compared case-insensitively; `*` or leaving it out matches anyone. `desc` is ignored. Every matching entry fires, in file order, after the message is stored and delivered.

- `shell:<path>` runs the script detached, working directory the home, with `COUSIN_HOOK_USER`, `COUSIN_HOOK_MESSAGE`, `COUSIN_HOOK_PATTERN`, `COUSIN_SLUG` and `COUSIN_HOME` set. Output goes to `<home>/data/chat-hooks.log`. A relative path is taken from the home. The script has to be inside the home or the framework root, otherwise it's refused with a line on stderr.
- `inject:<text>` delivers the text to the cousin as its own item, from `fw-hook` on its `system` thread, right after the message.

A missing or broken file, a bad regex or a failing script never affects the send. Hook lines aren't stored in the history.

## Files a send writes

| path | what |
|---|---|
| `data/chat.db` | the history and reactions |
| `data/inbox.db` | the delivered item, for the runner |
| `data/corrections.jsonl` | corrections spotted in operator messages |
| `data/chat-hooks.log` | output of shell hooks |
| `chat/inbound/<id>.<ext>` | inbound images. Nothing cleans this up |
