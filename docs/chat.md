# Chat

Each [cousin](glossary.md#cousin) keeps its own chat history, and no cousin runs a server of its
own. This page covers where a message goes, how it reaches the cousin, how
the cousin answers, how cousins talk to each other, what the console does
with the history, and where the Telegram bridge fits ([telegram](telegram.md)
has the details). The chat routes the console answers are in
[reference/console-api.md](reference/console-api.md); the same API over HTTP,
which a remote hive node's own chat endpoint answers, is in
[reference/chat-api.md](reference/chat-api.md).

## Where a message goes

A message for a cousin is stored in that cousin's `data/chat.db` and handed
to its [runner](glossary.md#runner)'s [inbox](glossary.md#inbox) (`data/inbox.db`); the runner is woken through its
wake socket. Nothing listens on a port per cousin. Who does the storing
depends on where the message comes from:

- **The console.** It answers the chat routes itself, in its own process,
  over the cousin's `data/chat.db`, and delivers to the inbox
  ([in the console](#in-the-console)).
- **Another cousin.** `cousin-chat send` writes the peer's store and inbox
  from the sender's own process ([cousin to cousin](#cousin-to-cousin)).
- **The cousin itself.** `cousin-reply` writes the cousin's own store, in
  its own process ([replying](#replying)).
- **Telegram.** The bridge runs as a child of the
  [supervisor](glossary.md#supervisor) (`telegram:<slug>`); it stores the row and delivers it to the
  inbox itself ([telegram](telegram.md)).
- **A remote hive node.** The hive carries it. A node reaches its home
  cousin through the queen's `POST /hive/tell-home`, and a message for the
  node goes to the node's own chat endpoint, which the console forwards to
  ([remote cousins](remote-cousins.md)).

Nothing listens for chat but the console, so who can send a message is who
can reach the console: its address allowlist and its login
([console](console.md)).

A cousin with no `[agent] runner` gets nothing: it is on no
[lane](glossary.md#lane), so it is refused by name, with one line:

```
cousin-chat send kestrel "the deploy is green"
#   -> cousin-chat: kestrel has no [agent] runner: 2.0.0 has no legacy tmux lane. Move it on the last 1.x release with cousin-migrate apply kestrel, or convert it by hand (docs/migrating.md, "A cousin with no runner")
```

`cousin-chat send` exits 1 and sends nothing, the Telegram bridge refuses
to start (and refuses each message the same way), and start, stop, [flip](glossary.md#flip) and
reincarnate refuse it; the console's start and stop answer 409. A
[worker](glossary.md#worker) (`[cousin] type = "worker"`) has no session to deliver to either.
The runner kinds are `sdk`, `tmux`, `opencode` and `fake`
([runners](reference/runners.md)).

What a cousin keeps, all under its home:

- `data/chat.db`: SQLite with every message and reaction. One [thread](glossary.md#thread)
  per person, keyed by the name lowercased with spaces turned into
  underscores, so "Ana" and "ana" are the same thread.
- `chat/inbound/<message id>.<ext>`: images people sent from the console
  (and, before 0.11, images the cousin attached with `cousin-reply --image`).
- `chat/images/`, `chat/audio/`, `chat/video/`: generated media
  ([media](media.md)), files the cousin attached to a reply with
  `cousin-reply --image` or `--video`, and photos relayed from Telegram.

There's no retention policy on any of it. Old threads can be archived
(hidden, not deleted) from the console.

## How a message reaches the cousin

A send takes `{"user": "ana", "message": "..."}`. The console,
`cousin-chat` and the Telegram bridge all take the same steps: store the
row, deliver it, capture a correction when the sender is the operator, then fire
the chat hooks. The delivery is one `chat` item in the runner's inbox, on
the sender's thread (`operator:ana` for you, `peer:<slug>` for a cousin,
`person:<name>` for anyone else), and the runner answers it in a
[turn](glossary.md#turn) ([runners](reference/runners.md#what-folds-into-a-running-turn) says
when it joins a turn already running). What the model reads starts with a
header line:

```
[operator:ana] chat from ana at 2026-09-18 14:02 UTC

can you check last night's backup?
```

The message itself follows verbatim, line breaks included.

A send's answer means "stored", not "the cousin has seen it". A message
that arrives while the runner is down waits in the inbox and is answered
once the runner is back.

Extras that can ride on the item:

- An image sent with the message (`"image": "data:image/png;base64,..."`)
  is saved as `chat/inbound/<id>.<ext>` and handed to the runner as that
  file's path; the runner shows it to the model. If it can't be decoded,
  the message still goes through, with `[image attached, decode failed]`
  in place of the path.
- For a chat message, the runner's prompt hook may add an `[fw-recall]`
  line naming memory files that look relevant
  ([memory](memory.md#proactive-recall-in-chat)).
- A reaction in the console reaches the cousin as a `reaction` item on the
  `system` thread:
  `[fw-reaction] msg-id=42 emoji=... user=ana tap_count=2 op=bumped`.
  Tapping the same reaction again bumps the count instead of removing
  it, so a cousin can tell you're insisting.

### Chat hooks

A cousin can react to a kind of message without having to notice it
each time. Put a list in `chat-hooks.json` in the cousin home:

```json
[
  {
    "pattern": "(?i)\\bprice\\s+check\\b",
    "user": "ana",
    "handler": "shell:scripts/quote-prices.sh",
    "desc": "quote the tariff when ana asks"
  },
  {
    "pattern": "(?i)\\bdebug\\s+health\\b",
    "user": "*",
    "handler": "inject:[fw-hook] consider running the health check",
    "desc": "nudge on a debug request"
  }
]
```

`pattern` is a Python regex searched in the message. `user` is matched
case-insensitively; `*` or leaving it out matches anyone. Every matching
hook fires, in file order, after the message has been stored and
delivered. Hooks fire on every send the console, `cousin-chat` and the
Telegram bridge make.

- `shell:<path>` runs a script in the background, from the cousin home,
  with `COUSIN_HOOK_USER`, `COUSIN_HOOK_MESSAGE`, `COUSIN_HOOK_PATTERN`,
  `COUSIN_SLUG` and `COUSIN_HOME` set, and without the server's auth
  variables or any credential-shaped variable (`*SECRET*`, `*_KEY`,
  `*_TOKEN`, `*_PASSWORD`). Output goes to
  `data/chat-hooks.log`. A relative path is taken from the home, and the
  script must live inside the home or the framework root; anything else
  is refused.
- `inject:<text>` delivers the text to the runner as its own item, a
  `hook` item from `fw-hook` on the `system` thread, right after the
  message.

A broken hook file, a bad regex or a missing script never breaks a
send. A bad regex is reported once per process, on the stderr of the
process that made the send (the console, the bridge or `cousin-chat`).

## Replying

A cousin answers with `cousin-reply`. The text comes from stdin, so
newlines and quotes survive:

```
cousin-reply --user ana <<'REPLY'
Backup ran at 03:10, 41 GB, no errors.
The one from Tuesday is still missing though.
REPLY
#   -> reply posted (id=57)

echo "done" | cousin-reply --user ana
cousin-reply -m "done" --reply-to 55
cousin-reply --user ana --image chart.png -m "last week's disk use"
cousin-reply --user ana --video run.mp4 -m "the bench run"
```

- `--user` is who the reply is for; without it the reply goes to
  `[operator] name` from `cousin.toml`, and with neither it refuses.
- `--reply-to <id>` quotes an earlier message.
- `--image` attaches a PNG, JPEG, GIF or WebP; `--video` an MP4, WebM,
  MOV or M4V (one or the other). The file is copied to `chat/images/`
  or `chat/video/` under a fresh name, and the reply row records it
  (`attachment_kind`, `attachment_path`) the way `cousin-image` does, so
  the console shows it and the Telegram bridge relays it. With no text,
  the caption is `(image: <file name>)` or `(video: <file name>)`.
- Exit codes: 0 posted, 1 the reply couldn't be stored (a local write
  failure), 2 missing config or bad arguments, 3 blocked by the outbound
  filter.

`cousin-reply` needs `COUSIN_HOME` and writes the reply into the
cousin's own chat store, in its own process. The reply goes into the
history for that person's thread. It's never delivered back to the
cousin itself.

If the install has an outbound filter (`config/outbound-filter.json`,
see [configuration](configuration.md)), replies and cousin-to-cousin
messages pass through it first, and a blocked message isn't sent at
all.

## Cousin to cousin

Another cousin doesn't read your chat tab. A message for it goes into its
own history and inbox:

```
cousin-chat list
#   -> wren         kind=sdk      (self)
#      kestrel      kind=tmux
cousin-chat send kestrel "the descaling schedule moved to Fridays"
#   -> {"id": 12, "ok": true, "to": "kestrel"}
```

`list` prints each cousin's runner kind: `none` for a cousin with no
runner, `worker` for a worker. `send` writes the message into Kestrel's
chat history and inbox directly, from the sender's own process, as a
`chat` item on the `peer:wren` thread, and Kestrel answers with
`cousin-chat send wren ...`. A cousin with no runner is refused, exit 1
([above](#where-a-message-goes)). The sender name is the sending cousin's
`name`. `--from` may only respell it (the cousin's own `name` or slug, case
and spaces aside): any other name is refused, exit 2, and nothing is sent. To an
external peer `--from` may still be a free-form display name ("Wren of
testbed"): the receiving install checks it.
So is a sender name that is the target's operator or one the framework
writes itself (`fw-hook`, `runner`): a cousin's message never reaches the
operator-only paths, such as correction capture.

The two paths don't mix, and picking the wrong one fails quietly:

- A message from you (the operator) is answered with `cousin-reply --user ana`.
- A message from a cousin is answered with `cousin-chat send <its slug>`.

`cousin-reply --user Kestrel` posts into Wren's own history under a
thread called kestrel, where nobody reads it. The CLAUDE.md template
spells this out for every cousin, and the MCP `send` tool picks the
right path for you by name ([mcp](mcp.md)).

A cousin with `peer_visible = false` under `[cousin]` doesn't appear in
anyone's `cousin-chat list`, and it doesn't see any peers either.

### Cousins on another install

A cousin that runs somewhere else (another checkout on the same box, or
another machine on the LAN) can be reached by slug once
`config/external-peers.toml` names its address. Say Kestrel still lives
on an older install:

```toml
[peers.kestrel]
url = "http://192.0.2.10:8085"
send_path = "/api/send"     # the default
```

```
cousin-chat list
#   -> wren         kind=sdk      (self)
#      kestrel      url=http://192.0.2.10:8085 (external)
cousin-chat send kestrel "the deploy is green"
```

The message is POSTed as `{"user": <sender name>, "message": <text>}`.
A peer on a 2.x install takes messages on its console's `POST /peer/send`
instead: give its entry a `token_file` (the secret the two installs share,
a 0600 file under the root) and a `sender` (the name that peer knows this
install by). `send_path` then defaults to `/peer/send`, the body is
`{"to", "message", "msg_id", "sent_at"}`, and the request carries an
`Authorization: HMAC <sender>:<signature>` header signed with the secret;
the secret itself is never sent. The keys, and the peer's side
(`inbound_token_file`, `reach`), are in
[external-peers.toml](configuration.md#external-peerstoml).

A signed send that can't be confirmed isn't lost. If the peer answers a
5xx or 429, or the connection fails, breaks off or times out, the
message goes to the outbox (`data/outbox.db`) and the send answers
`{"ok": true, "to", "id": null, "queued": true, "msg_id", "note"}`
(`cousin-chat send` prints that; the runner's `send` tool returns it).
The loops daemon sends it again on its ticks, at least 15 s, 30 s, 1, 2,
4 and then 5 minutes apart (seven tries in all, at about 0, 15 s, 45 s,
2, 4, 8 and 13 minutes), always under the same `msg_id` with a fresh
`sent_at`. The peer's gate delivers an id only once, so a retry of a
message that had landed answers 409 and counts as delivered, never shown
twice. Nothing is sent more than 14 minutes after the first attempt
started (inside the 15 the gate remembers an id for). The outbox gives
up at once on any other 4xx, a redirect or an error that won't pass on
its own, when the peer is gone from `config/external-peers.toml` or lost
its `token_file`, and on a row whose 14 minutes ran out while the
daemon was down. A 5xx the peer gives for good (its 503 for an entry
with no `reach`) is still retried until the 14 minutes run out. Either
way the sending cousin gets a `system` item from `framework` saying how
it ended (`[fw-outbox] Your message to kestrel ... was delivered on
attempt 3`, or `was NOT delivered` with the last error). One pass sends
for at most 20 seconds and stops trying a peer after its first failure,
so a dead peer never holds the daemon's tick; and a retried message can
arrive after one sent later straight away, so the outbox keeps no order
between them. A legacy peer (no `token_file`) has no id to dedup by and
is sent once, never retried. `cousin-chat outbox` and the console's
System page (install config, under external peers) list what the
outbox holds.

The address must pass the same allowlist the console uses (loopback,
private ranges, plus `config/net-allowlist.json`). The request goes
direct, no proxy, and a redirect is refused. A local cousin with the
same slug always wins over the file. For cousins on other machines that
are part of this install, see [remote cousins](remote-cousins.md).

## In the console

The console answers its chat view itself, over each cousin's
`data/chat.db`, and keeps no copy of the history. For a remote hive node
it forwards to the node's own chat endpoint ([console](console.md)).

- Messages render as Markdown (GitHub flavour, single line breaks kept).
  A fenced code block tagged `mermaid` is drawn as a diagram. Both
  libraries (marked and mermaid) load from cdn.jsdelivr.net, so a browser
  with no internet gets plain text.
- Paste an image into the composer, or drop one on it, to send it with
  the message.
- Images and generated media show inline. Click one to open the viewer:
  full size, left and right arrows walk through every image and video in
  the thread, Escape closes it.
- The media button in the chat header hides or shows all attachments.
  It's remembered in your browser only.
- Search, reply-to, reactions, archive and the [reasoning pane](console.md#the-reasoning-pane-a-runner-cousin) (the
  runner's live [stream](glossary.md#stream) of the cousin's turns, with the interrupt) are all
  in the same view.

## Telegram bridge

The bridge relays one cousin's chat to your Telegram and back. It is off
until it is fully configured, and runs as a child of `cousin-supervisor`
beside the cousin's runner. What it relays, how to provision a bot, when it
runs and the state file are all in [telegram](telegram.md).
