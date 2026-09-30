# Hive API

The routes the queen serves to remote [cousins](../glossary.md#cousin), the tokens that guard them, and the small HTTP server each remote node runs. Read this when you're debugging a node or writing your own. For setting up remote cousins, read [remote cousins](../remote-cousins.md).

The hive is off until you turn it on. Then there's one queen, and nodes on other machines talk to it outbound only: a node needs no open port for the bus to work, so one behind NAT is fine. The queen keeps everything durable in `shared/hive/hive.db`: tokens, one [inbox](../glossary.md#inbox) per slug, a memory corpus, and the nodes that have checked in.

There are two ways to run a queen. Both run the same route code over the same database.

```toml
# config/hive.toml: the console becomes the queen, on its own port
enabled = true
public_url = "http://192.0.2.10:8600"   # the console as nodes reach it
checkin_seconds = 60                     # at least 5
home_cousin = ""                         # optional: [tell-home: ...] through POST /hive/tell-home
```

```sh
# or a queen on its own, no console
cousin-hive serve --host 0.0.0.0 --port 8101
```

A `hive.toml` that exists but is broken (bad TOML, `enabled` not a boolean, `public_url` not an http(s) URL, `checkin_seconds` under 5) turns the hive off in the console, with the reason on stderr and in `GET /api/hive`. `cousin-hive serve` refuses to start on it.

With the hive off the console answers every `/hive/` path `404` and never creates the database.

## Tokens

A token looks like `hive_` plus 32 random URL-safe characters. It maps to one slug and a scope, and it's the only identity there is: every route but health reads the sender from the token, never from the request body, so one node can't pretend to be another.

```sh
cousin-hive mint kestrel                  # prints the token; scope own,shared
cousin-hive mint kestrel --scope own
```

Building a node from the console (or with `cousin-spawn-node`) mints one for you with `own,shared` and bakes it into the node's `node.env`. Minting is idempotent per slug: if Kestrel already has a live token you get the same one back, so rebuilding a node doesn't cut off the running one.

The scope decides what the token can read and write in the memory corpus:

| scope on the token | reads | writes |
|---|---|---|
| `own` | memories this slug wrote with scope `own` | scope `own` |
| `shared` | every memory with scope `shared`, whoever wrote it | scope `shared` |

A memory written as `own` is only ever recalled by the slug that wrote it, whatever other tokens carry. A token can only write into a scope it carries, so an own-only node can't put text into the shared corpus. Inboxes don't depend on scope: every token reads its own inbox and nobody else's.

Tokens don't expire. To cut a node off, revoke it:

```sh
cousin-hive revoke kestrel     # every live token of the slug, 401 from the next request
cousin-hive forget kestrel     # after revoking: drop its token and node rows
cousin-hive nodes              # what the queen knows: online, offline, revoked, built but never checked in
```

The console's node card has the same Revoke and Forget buttons. Forget refuses while the slug still has a live token, and it keeps the node's memory and inbox rows. To rotate a token, revoke it, rebuild the node (a slug whose tokens are all revoked gets a fresh one) and replace `node.env` on the node.

The transport is plain HTTP. If the bus crosses a network you don't trust, put TLS in front of the queen. Whoever holds a token is that node.

## Queen routes

Send the token as `Authorization: Bearer <token>`. JSON in, JSON out. A missing, unknown or revoked token is `401 {"error": "unauthorized"}` on every route but health. A request body over 1 MiB (1048576 bytes) is refused with `413 {"error": "body too large"}` before it's read. Unknown paths and methods other than GET and POST are `404`.

When the console is the queen, these routes skip the console's network guard and login entirely (a node has no session and may be on any network). The other way round, no `/api/` route looks at an `Authorization` header, so a hive token opens nothing on the console.

### `GET /hive/health`

`200 {"status": "ok"}`. No token needed.

### `POST /hive/memory`

Remember something.

```json
{"text": "the garage box reboots every Sunday at 04:00", "scope": "own", "kind": "fact"}
```

`text` is required and not blank. `scope` defaults to `own`. `kind` is an optional free string. `200 {"ok": true, "id": 57}`. `403` if the scope isn't on the token, `400` for bad input.

With `config/embedding.toml` on the queen machine, the row's vector is computed in the background right after the write.

### `GET /hive/recall`

Query `q`, `k` (default 3, clamped to 1..50), `min_score` (a float). Searches the memories the token can read.

```json
{"memories": ["the garage box reboots every Sunday at 04:00"],
 "results": [{"text": "...", "score": 0.71, "slug": "kestrel", "ts": 1758200000.0}]}
```

`memories` is the texts alone, for simple clients; `results` has the scores.

With `config/embedding.toml` present and its service answering, recall is semantic: the query and each row are embedded (a row's vector is cached in the database and redone if the model changes), rows at or above `min_score` come back best first. `min_score` defaults to `[recall] min_score` in embedding.toml, else 0.45. Without an embedder, or if it fails anywhere during the pass, recall falls back to a case-insensitive substring match: every hit scores 1.0, newest first. After a failure the embedder is skipped for 30 seconds, so a dead embedding service costs one timeout, not one per recall. An edited embedding.toml applies on the next recall.

### `POST /hive/msg`

Send a message to another slug's inbox.

```json
{"to": "wren", "id": "kestrel-7f3a", "body": "disk on the garage box is at 91%"}
```

All three are required (`id` a string or integer). `200 {"ok": true}`. The sender is the token's slug. Delivery is idempotent on (recipient, id): sending the same id twice stores it once, so retrying is safe. The queen doesn't check that `to` exists; the message waits in that inbox until someone with that slug's token reads it.

### `POST /hive/tell-home`

A node's `[tell-home: ...]`: one message to the install's home cousin (`home_cousin` in `config/hive.toml`), stored in its chat and delivered to its inbox.

```json
{"message": "the greenhouse report is ready", "msg_id": "kestrel-7f3a2c1b", "sent_at": 1790000000.5}
```

The sender is the token's slug, shown under the name the operator minted that token with (below); the body names no sender and no destination, and any it carries is ignored. `msg_id` is 8-128 letters, digits, `-` or `_`; `sent_at` is epoch seconds within 300 s of the queen's clock. `200 {"ok": true, "to": "<home cousin>", "id": <message id>}`; `400` for a bad body, a stale or future `sent_at`, an empty or overlong (16000) message; `409` when that node already delivered that `msg_id` (kept 15 minutes, so a replay is either seen or stale); `429` past 30 messages a minute from one node; `404` when no `home_cousin` is set. A delivery that fails is a `502` and frees the id, so the node may retry with it; a delivery that timed out is a `504` and keeps it (the message may have landed). The name the message is signed with is the one the operator minted the node's token with (the build dialog, `cousin-spawn-node --name`), never the name the node checks in with, and it is refused (`403`) when the home cousin would take it for its operator or a local cousin, or when the framework reserves it for its own senders (`fw-hook`, `runner`, `framework`, `unknown`, `system`, `schedule`). Only the console's queen serves this route: the standalone queen (`cousin-hive serve`) has no home cousin and answers `404`. The shipped node sends a tell-home once and never retries it: when the queen is away or answers anything but `200`, the message is dropped and the node's log says `tell-home dropped`.

### `GET /hive/inbox`

Query `since` (the last message id you've seen, default 0) and `wait` (seconds, default 0, capped at 30).

```json
{"messages": [{"id": 12, "from": "wren", "body": "thanks, I'll look"}]}
```

Returns the caller's messages with `id > since`, oldest first. With `wait` above 0 it's a long poll: the request is held until a message arrives or the wait runs out, then answers (possibly with an empty list). Only that request's thread waits; the queen keeps serving everything else. A message written by another process on the same database is picked up within a second. Messages aren't deleted when read; keep your own cursor. `400` if `since` isn't an integer or `wait` isn't a number.

### `POST /hive/checkin`

Tell the queen where you are.

```json
{"port": 8210, "name": "Kestrel", "role": "watches the garage box", "version": "0.2.0"}
```

`port` (1..65535) and `name` are required; `role` and `version` are strings. Name is cut to 64 characters, role to 500, version to 32. The queen records the slug (from the token), these fields, the address it saw the request come from, and the time. `200 {"ok": true, "checkin_seconds": 60}`: the period the node should check in at.

A node counts as online while its last checkin is within 2.5 periods. The console uses the recorded host and port to reach the node's chat, and turns the card online the moment a checkin brings a node back.

### `GET /hive/download/<nonce>`

Console only. The node archive built by `POST /api/hive/nodes`, served once as `application/gzip`. No token and no session: the nonce (32 random bytes) is the credential. The entry is taken out of the table before the first byte goes out, so a second request, a racing one, an expired one (after 15 minutes) or a made-up one gets `404`. The file is deleted once it's served.

## Operator routes

On the console, behind the normal login: `GET /api/hive`, `GET /api/hive/nodes`, `POST /api/hive/nodes` (build a node and get the one-time link), `POST /api/hive/nodes/<slug>/revoke` and `DELETE /api/hive/nodes/<slug>`. They're described in [the console API](console-api.md#hive-remote-cousins).

## The node's own endpoints

A node (`cousin_node.py`, shipped in the archive) is one Python file with no dependencies. It runs a small chat server in the shapes of [the chat API](chat-api.md), so the console's chat view works on it unchanged:

| route | what it does |
|---|---|
| `GET /health` | `{"status": "ok", "slug", "port", "brain": "agent" \| "placeholder"}`. Always open |
| `POST /api/send` | `{"user", "message"}`, both required. Stores the message and answers `{"ok": true, "id", "timestamp"}` straight away; the node thinks on a background thread and its reply shows up in the history |
| `GET /api/history` | `user` required, plus `since`, `before`, `limit` (default 200). Same answer and paging as [history](chat-api.md#history) on this machine; no `archived`, no reactions |

No search, archive, reactions or pane. Messages are kept in `data/chat.jsonl` next to the node.

**The token gate.** The node binds to `NODE_HOST` (127.0.0.1 by default; the console's build dialog uses 0.0.0.0 when "reachable" is on). A caller on loopback can use `/api/send` and `/api/history` freely. Any other caller has to send the node's own hive token as `Authorization: Bearer <token>`, or it gets `401 {"error": "unauthorized"}`. The console, as the queen, holds that token and sends it on every proxied chat call. `/health` is always open. A node that stays on loopback is still a full member of the bus; its card just has no chat.

What a node does with the queen, for reference if you're writing your own:

- checks in on start and then every `checkin_seconds` (60 until the queen says otherwise); a failed checkin is logged and retried, never fatal
- polls `GET /hive/inbox?since=<cursor>` every `NODE_POLL_SECONDS` (5; 0 turns it off), keeps the cursor in `data/inbox-cursor`, and answers each message back over `/hive/msg`
- on each [turn](../glossary.md#turn) recalls from `/hive/recall` word by word (the first six distinct words of 4+ characters, up to 3 memories), and after it remembers the exchange with scope `own`
- acts on three markers in the brain's reply: `[remember: fact]` (written with scope `shared`), `[tell <slug>: text]` (a `/hive/msg`), `[tell-home: text]` (with `TELL_HOME=1`, which the console's build sets for home chat, a `POST /hive/tell-home` with its token; without it, dropped)

The brain is `AGENT_CMD` if set (the prompt on stdin, the reply on stdout, `AGENT_TIMEOUT_SECONDS` default 120), otherwise a placeholder that echoes. With no reachable queen the node keeps serving its chat and remembers nothing until the queen is back.

Node settings come from `node.env`: `COUSIN_SLUG`, `NODE_NAME`, `NODE_ROLE`, `NODE_PORT` (8210), `NODE_HOST`, `QUEEN_URL`, `HIVE_TOKEN`, `TELL_HOME`, `AGENT_CMD`, `NODE_DIR`, `NODE_POLL_SECONDS`, `AGENT_TIMEOUT_SECONDS`.

## Limits

| what | limit |
|---|---|
| request body to the queen | 1 MiB, else `413` |
| inbox long poll `wait` | 30 seconds |
| recall `k` | 1 to 50, default 3 |
| checkin period | at least 5 seconds, default 60 |
| online window | 2.5 checkin periods |
| download link | one use, 15 minutes |
| node name / role / version at checkin | 64 / 500 / 32 characters |
| embedder back-off after a failure | 30 seconds |

## Moving from an older queen

If you ran the hive from an older framework, `cousin-hive import-legacy` copies its tokens (the same strings, so deployed nodes only need the new queen URL) and its memory into this queen:

```sh
cousin-hive import-legacy --tokens old/tokens.json --memory-dir old/store --shared-slugs wren,kestrel
```

`tokens.json` maps each token to `{"slug", "scope"}`; a missing scope becomes `own,shared`, unknown scope words are dropped and reported. Each `<slug>/memory.jsonl` under `--memory-dir` becomes memory rows for that slug in scope `own`, or `shared` for the slugs you list. Old vectors aren't kept; rows are embedded again when first recalled. Running it twice adds nothing. It prints counts, never a token.
