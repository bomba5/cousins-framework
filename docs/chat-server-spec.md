# Chat server specification

One chat server per cousin: a small stdlib-only HTTP daemon that owns the
cousin's message history and delivers inbound messages into the cousin's
terminal session. The web UI, peer cousins, and CLIs are all just HTTP
clients of this one surface.

This document is the contract the implementation is written against and
tested from.

## Process model

- One server per cousin, started with `--home <cousin home>` and reading
  identity from `<home>/cousin.toml` (`[cousin] slug`, `[cousin] name`,
  `[chat] port`, optional `[chat] tmux_session` defaulting to the slug).
- Thread-per-request (`ThreadingMixIn`), and therefore one SQLite
  connection per request, opened lazily and **explicitly closed when the
  request finishes**. Relying on garbage collection here leaks native
  allocator arenas under large payloads; the close is load-bearing, not
  hygiene.
- Startup fails loud: missing configuration, unbindable port, or - when
  terminal delivery is enabled - an absent tmux binary are startup errors,
  not per-message log lines.
- No graceful-shutdown ceremony; the database is WAL-mode and every write
  commits per request.

## Storage

SQLite at `<home>/data/chat.db`, WAL mode with a small autocheckpoint
(200 pages) so the WAL cannot grow unbounded across restarts.

```sql
CREATE TABLE messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_user     TEXT NOT NULL,   -- normalized recipient/thread key
    user          TEXT NOT NULL,   -- display name of the author
    message       TEXT NOT NULL,
    timestamp     TEXT NOT NULL,   -- UTC ISO-8601
    type          TEXT NOT NULL,   -- 'user' inbound; the slug for own replies
    archived      INTEGER NOT NULL DEFAULT 0,
    reply_to      TEXT,            -- opaque client JSON, stored verbatim
    reply_to_user TEXT
);
CREATE INDEX idx_messages_chat_user ON messages(chat_user);
CREATE INDEX idx_messages_archived  ON messages(archived);

CREATE TABLE reactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    user       TEXT NOT NULL,
    emoji      TEXT NOT NULL,
    tap_count  INTEGER NOT NULL DEFAULT 1,
    created    TEXT NOT NULL,
    UNIQUE(message_id, user, emoji)
);
CREATE INDEX idx_reactions_message ON reactions(message_id);
```

The full schema is declared at creation; there is no migration dance and
no dual id space. `tap_count` exists because repeated taps of the same
reaction are an urgency signal, and the unique constraint would otherwise
dedupe them; a retap increments, it does not toggle off.

Attachment columns (`image`, `audio`, `video`) are reserved for the media
subsystem and absent until it ships.

## Endpoints

Every response is JSON (`Cache-Control: no-store`,
`Access-Control-Allow-Origin: *`) unless stated. Malformed request JSON is
a `400`, never silently an empty object. Every handler runs the network
guard first.

### `GET /health`
`200 {"status":"ok","slug":...,"port":...}` - the liveness probe.

### `POST /api/send` - inbound message
Request: `{"user": <display name>, "message": <text>, "reply_to": {...}?}`.
`user` and a non-empty `message` are required (`400` otherwise).

Effects, in order:
1. Insert into `messages` (`type='user'`).
2. Deliver into the cousin's terminal (see Injection).
3. Touch `<home>/data/.last-user-msg` - its mtime feeds the time prefix of
   the NEXT delivery, so each delivery reports the gap since the previous
   message. The touch happens after the delivery text is composed.

Response: `200 {"ok":true,"id":N,"timestamp":...}`. The response does not
wait for terminal delivery (see Stated limits).

### `POST /api/<slug>_reply` - the cousin's own outbound
The path embeds this server's slug; a reply misrouted to another cousin's
server fails with `404` instead of silently landing in the wrong history.

Request: `{"message": <text>, "reply_to_user": <name>, "reply_to": {...}?}`.
`reply_to_user` is required: there is no default recipient (`400` when
absent - an install with no configured operator has nobody to default to).

Insert with `chat_user = normalized(reply_to_user)`, `user` = the
configured display name, `type` = the slug. No terminal delivery: a
cousin's own words do not echo back into its pane.

### `GET /api/history?user=<name>&since=N&before=N&limit=N&archived=0|1|all`
`user` is required (`400` when absent). Modes, mutually exclusive:
`before` pages backward (`id < before`, newest-first, returned oldest-first),
`since` polls forward (`id > since`), neither returns the newest `limit`
(default 200). `archived` defaults to live rows only.

Response: `{"messages":[...], "total":N, "has_more":bool}` where
`has_more` is computed from an actual count, and each message carries its
reactions.

### `GET /api/search?q=<text>&user=<name>?&archived=0|1|all`
Substring search over message text and quoted text; without `user` it
searches all threads. The archive filter applies here exactly as in
history. Newest-first, capped at 50.

### `POST /api/reactions`
Request: `{"message_id":N, "user":..., "emoji":..., "action":"tap"|"remove"}`,
all validated (`400` on garbage, including non-integer ids). Add, bump
(`tap_count+1`), or remove; add and bump notify the cousin's terminal with
a single structured line. Response includes the message's full reaction
state.

### `POST /api/archive`
Request: `{"user":..., "keep":N}` - archive all but the newest `keep`
rows of that thread (`keep` validated, default 0). Response reports rows
actually archived.

### Static files
`GET /<file>` serves `<home>/www/` for a small extension allowlist, with
a path-traversal guard that requires the resolved path to sit strictly
inside the root. Optional: a cousin without a `www/` directory simply has
no pages.

## Terminal delivery (injection)

Inbound messages reach the cousin as a line typed into its tmux session:

```
[now: 2026-01-01 12:00 UTC | dt-since-msg: 12m] (Chat <Name>): <text>
```

- The `(Chat <Name>): ` prefix is the chat surface convention; the time
  prefix reports the gap since the previous inbound message (from the
  presence marker's mtime) and degrades to `[now: ...]` alone when no
  marker exists.
- Reaction notifications are single structured lines:
  `[fw-reaction] msg-id=N emoji=E user=U tap_count=T op=added|bumped`.
- Delivery is asynchronous on a background thread; the HTTP response never
  waits on tmux.
- All injection is serialized behind one process-wide lock: concurrent
  senders otherwise interleave keystrokes and merge messages.
- The wire sequence is: paste the text literally (`send-keys -l`), settle
  for a duration scaled to text length (a long paste needs the terminal to
  drain before Enter, or the submission strands), send Enter, then verify
  via `capture-pane` that the text left the input box and retry Enter
  exactly once. Injected text is single-line by construction - an embedded
  newline would split the paste into a premature submit.
- The tmux binary and socket are resolved from configuration/PATH, never
  hardcoded paths.

## Inbound files

An inbound `data:` image is decoded and written to
`<home>/chat/inbound/<message-id>.<ext>` (extension normalized against a
small allowlist, anything else `.bin`), and the delivery line carries
`[image attached -> Read <absolute path>]` so the cousin can open it. The
terminal line cannot carry megabytes of base64; the file is the handoff.
Nothing else reads the directory and there is no retention policy - it is
the cousin's inbox to manage.

## Network guard

Every request is checked against an address allowlist before anything else
runs. Defaults: loopback plus the RFC1918 ranges. An install extends the
list via `<framework root>/config/net-allowlist.json` (`{"allow": [cidr,
...]}`); extensions ADD to the defaults and can never remove loopback,
because local CLIs must always work. Unparsable client addresses are
denied (fail closed); unparsable configured CIDRs are skipped. Denials are
`403` with a CORS header, so a browser shows the status rather than an
opaque error.

## Stated limits

Like the contamination gate, this server is honest about what it does not
do:

- **Identity is client-asserted.** The `user` on a send and the
  `reply_to_user` on a reply are strings the caller chose; the address
  allowlist is the only boundary. Do not expose a chat server beyond
  networks where every client is trusted.
- **Delivery is best-effort by design.** A send is acknowledged when it is
  stored, not when the cousin has seen it; a failed injection is logged
  loudly server-side but is invisible to the sender. The message is never
  lost - it is in the history - but "delivered to the terminal" is not
  part of the acknowledgement.

## Consciously excluded

Engagement tracking, media routes and attachments, reaction-driven
queues, response-cadence lints, operator-correction capture, and
proactive memory recall belong to subsystems that ship separately. The
recall hook has a reserved seam: delivery composition accepts an optional
suffix provider; the memory subsystem plugs in there without touching the
send path.
