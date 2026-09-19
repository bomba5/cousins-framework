# Chat

Every cousin runs its own small chat server. This page covers how a
message gets into a cousin's session, how the cousin answers, how
cousins talk to each other, what the console does with the history, and
the Telegram bridge. The HTTP routes themselves are in
[reference/chat-api.md](reference/chat-api.md).

## One server per cousin

Starting a cousin (`cousin-spawn <slug> --start`, the console's start
button, a flip) starts its chat server too. By hand:

```
cousin-chat-server --home cousins/wren
#   -> cousin-chat-server: wren on port 8091
```

It reads everything it needs from the cousin's `cousin.toml`:

```toml
[cousin]
slug = "wren"
name = "Wren"

[chat]
port = 8091
# host = "127.0.0.1"        # the default: listen on loopback only
# tmux_session = "wren"     # the default: the slug

[operator]
name = "ana"
```

It won't start without a port, and it won't start without `tmux` on
PATH unless you pass `--no-terminal-delivery` (then messages are stored
but never typed into a session). When spawn or the watchdog starts it,
its output goes to `data/chat-server.log` in the cousin home. It runs
with no supervisor of its own; the `cousin-chat-watchdog` timer restarts
a dead one, and `systemd/` also has a `cousin-chat-server@.service`
template if you'd rather have systemd own it ([operations](operations.md)).

What it keeps, all under the cousin home:

- `data/chat.db`: SQLite with every message and reaction. One thread
  per person, keyed by the name lowercased with spaces turned into
  underscores, so "Ana" and "ana" are the same thread.
- `chat/inbound/<message id>.<ext>`: images people sent, and images the
  cousin attached to a reply.
- `chat/images/`, `chat/audio/`, `chat/video/`: generated media
  ([media](media.md)).
- `www/`: optional. Files here (HTML, CSS, JS, images) are served as
  static pages from the chat port.

There's no retention policy on any of it. Old threads can be archived
(hidden, not deleted) from the console.

### Who can talk to it

Every request is checked against an address allowlist first: loopback
and the private ranges (10/8, 172.16/12, 192.168/16) by default. To add
more, write `config/net-allowlist.json` under the framework root:

```json
{"allow": ["100.64.0.0/10"]}
```

This can only add; loopback always stays in. Past that there's no login.
The sender name on a message is whatever the client says it is, so
don't open a chat server to a network where you don't trust every
machine.

## How a message reaches the cousin

A message comes in as `POST /api/send` with `{"user": "ana",
"message": "..."}`. The console, `cousin-chat`, the Telegram bridge and
curl all do exactly that. The server stores it, answers straight away,
and then types it into the cousin's tmux pane as one line:

```
[now: 2026-09-18 14:02 UTC | dt-since-msg: 12m] (Chat ana): can you check last night's backup?
```

`dt-since-msg` is the time since the previous message, so the cousin
can tell a quick follow-up from a message after a long gap. Newlines in
the message become spaces, because a newline in the paste would submit
half a message. The full text with its line breaks is still in the
history.

The typing itself: paste the text literally, wait a moment for the
terminal to take it (longer for long messages, at most 0.6 s), press
Enter, look at the pane, and press Enter once more if the text is still
sitting in the input box. All deliveries go through one lock so two
messages arriving together don't get their keystrokes mixed.

The HTTP answer means "stored", not "the cousin has seen it". If the
tmux session is gone, the failure is logged in the server's output and
the message stays in the history.

Extras that can ride on the line:

- An image sent with the message (`"image": "data:image/png;base64,..."`)
  is saved to `chat/inbound/<id>.png` and the line ends with
  `[image attached -> Read /abs/path/to/chat/inbound/42.png]` so the
  cousin can open it. If it can't be decoded, the message still goes
  through with `[image attached, decode failed]`.
- For messages from you, a recall line may be appended with
  memory files that look relevant ([memory](memory.md#proactive-recall-in-chat)).
- A reaction in the console reaches the cousin as
  `[fw-reaction] msg-id=42 emoji=... user=ana tap_count=2 op=bumped`.
  Tapping the same reaction again bumps the count instead of removing
  it, so a cousin can tell you're insisting.

### When the pane isn't ready

Three settings in `config/harness.toml` decide what the chat server does
when the pane shows something other than an agent waiting for input. The Claude Code
preset (`config/harness.toml.claude-code.example`) fills them in.

`attention_patterns` is text that means the agent is waiting on a
person: the login menu, the "trust this folder" prompt, the API key
question. Before typing, the server reads the pane; if one of these is
showing, it types nothing and logs `tmux delivery SKIPPED`, since typed
text would pick menu options. The message stays in the history, and the
console marks the cousin as needing attention. Open its pane, answer the
prompt, and send again.

```toml
attention_patterns = [
    "Select login method",
    "Yes, I trust this folder",
    "Do you want to use this API key",
]
```

`[input_mode]` handles Claude Code's vim mode. In NORMAL mode, typed text
is read as vim commands, so when the pane shows `normal_marker` the
server first sends `insert_keys`:

```toml
[input_mode]
normal_marker = "-- NORMAL --"
insert_keys = "i"
```

`busy_patterns` (regular expressions, such as Claude Code's
"esc to interrupt" spinner line) mean the agent is in the middle of a
turn. Chat delivery doesn't wait for these: a message typed while
Claude Code is working gets queued by Claude Code and handled after the
turn. Busy patterns are what stop `cousin-auth` and the console from
restarting a cousin mid-turn ([cousins](cousins.md)).

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
delivered.

- `shell:<path>` runs a script in the background, from the cousin home,
  with `COUSIN_HOOK_USER`, `COUSIN_HOOK_MESSAGE`, `COUSIN_HOOK_PATTERN`,
  `COUSIN_SLUG` and `COUSIN_HOME` set. Output goes to
  `data/chat-hooks.log`. A relative path is taken from the home, and the
  script must live inside the home or the framework root; anything else
  is refused.
- `inject:<text>` types the text into the pane as its own line, from
  `fw-hook`, right after the message.

A broken hook file, a bad regex or a missing script never breaks a
send. A bad regex is reported once on the server's stderr.

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
```

- `--user` is who the reply is for; without it the reply goes to
  `[operator] name` from `cousin.toml`, and with neither it refuses.
- `--reply-to <id>` quotes an earlier message.
- `--image` attaches a PNG, JPEG, GIF or WebP. It's copied to
  `chat/inbound/<reply id>.<ext>` and the console shows it on the reply.
  With `--image` and no text, the caption is `(image: <file name>)`.
- Exit codes: 0 posted, 1 the server said no or couldn't be reached, 2
  missing config or bad arguments, 3 blocked by the outbound filter.

`cousin-reply` needs `COUSIN_HOME` and posts to its own server's
`/api/<slug>_reply` route. The reply goes into the history for that
person's thread. It's never typed back into the cousin's own pane.

If the install has an outbound filter (`config/outbound-filter.json`,
see [configuration](configuration.md)), replies and cousin-to-cousin
messages pass through it first, and a blocked message isn't sent at
all.

## Cousin to cousin

Another cousin doesn't read your chat tab. It has its own server, so a
message for it goes there:

```
cousin-chat list
#   -> wren         port=8091   (self)
#      kestrel      port=8092
cousin-chat send kestrel "the descaling schedule moved to Fridays"
#   -> {"id": 12, "ok": true, "to": "kestrel"}
```

That lands in Kestrel's pane as `(Chat Wren): ...`, the same way your
messages do, and Kestrel answers with `cousin-chat send wren ...`. The
sender name is the sending cousin's `name`; `--from` overrides it.

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
`config/external-peers.toml` names its chat server. Say Kestrel still
lives on an older install:

```toml
[peers.kestrel]
url = "http://192.0.2.10:8085"
send_path = "/api/send"     # the default
```

```
cousin-chat list
#   -> wren         port=8091   (self)
#      kestrel      url=http://192.0.2.10:8085 (external)
cousin-chat send kestrel "the deploy is green"
```

The message is POSTed as `{"user": <sender name>, "message": <text>}`.
The address must pass the same allowlist the chat server uses (loopback,
private ranges, plus `config/net-allowlist.json`). The request goes
direct, no proxy, and a redirect is refused. A local cousin with the
same slug always wins over the file. For cousins on other machines that
are part of this install, see [remote cousins](remote-cousins.md).

## In the console

The console's chat view talks to each cousin's chat server; it keeps no
copy of the history ([console](console.md)).

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
- Search, reply-to, reactions, archive and the live terminal pane are
  all in the same view.

## Telegram bridge

The bridge relays one cousin's chat to your Telegram and back. It's the
one feature in the framework that sends your content to a third party,
so it stays off until it's fully configured, and refuses to start
otherwise.

What leaves the machine: HTTPS to `api.telegram.org` only. The bridge
long-polls for your messages and sends the cousin's replies back. It
opens no port and needs no public URL. Everything in that Telegram chat
passes through Telegram's servers.

Set it up:

1. Create a bot with @BotFather and copy its token.
2. Find your numeric Telegram user id (not your @name).
3. Put the token in a file under the framework root, readable only by
   you. `config/` is gitignored:

   ```
   install -m 600 /dev/null config/telegram-wren.token
   $EDITOR config/telegram-wren.token
   ```

4. Add a `[telegram]` table to the cousin's `cousin.toml`:

   ```toml
   [telegram]
   enabled = true
   token_file = "config/telegram-wren.token"    # relative to the framework root
   operators = [{ user_id = 123456789, name = "ana" }]
   ```

5. Run it, as a service if you like:

   ```
   FRAMEWORK_ROOT=$PWD cousin-telegram --home cousins/wren
   # or: COUSIN_HOME=$PWD/cousins/wren cousin-telegram
   ```

It finds the framework root through `FRAMEWORK_ROOT`, or from the home
itself when that lives at `<root>/cousins/<slug>`, so `--home` alone is
enough for a standard install. No framework root, no `[telegram]` table, `enabled` not true, no token file or an
empty `operators` list are all refusals with exit 2 and a message
naming the problem.

How it relays:

- In: a text message from an id in `operators` becomes a normal
  `/api/send` to the cousin's chat server, under the name you gave in
  `operators` (`operator` if you gave none). The cousin sees it like any
  other chat line. A photo goes in as an image attachment with its
  caption (or `[photo]`), the largest size Telegram offers, up to
  10 MB; the cousin gets the file path to read.
- Out: every few seconds the bridge reads each operator's thread and
  sends each new reply from the cousin there to that operator's
  Telegram id (every id that shares the name, if several do).
- A reply with an attachment is uploaded as the file: an image as a
  photo, a video as a video, a voice reply (mp3) as audio. Telegram's
  own limits apply (10 MB for a photo, 50 MB otherwise); a file over
  them is rejected, logged and skipped.
- A message from anyone else gets no answer at all, so the bot never
  confirms it exists. The bridge logs the rejected id, so if you got
  your own id wrong you'll see it in the log instead of wondering
  whether the bridge is down.
- Where it is gets saved in the cousin's `data/telegram-bridge.json`:
  the Telegram update offset and one reply position per operator
  thread. A restart picks up where the bridge stopped. On the very first
  start it begins at the end of each thread, so it doesn't re-send your
  chat history.
- A position moves only past what was delivered. Network errors, a
  chat server that's down, Telegram 5xx and rate limits (429) are
  logged and retried on the next pass, so nothing is lost; the bridge
  doesn't exit on them. A retry can send a reply twice to an operator
  who already got it when another operator's send failed.
- A permanent rejection (any other 4xx, such as a bot the operator
  blocked) is logged with the message id and skipped, so one bad
  message can't hold up everything behind it.

Rough edges right now:

- Only text and photos go in. A voice message, video, sticker or
  document from Telegram isn't relayed; the bridge logs that it skipped
  it. The chat server takes image attachments only.

Keep the token file private. Whoever has it controls the bot.
