# Telegram bridge

The bridge relays one cousin's chat to Telegram and back, so you can talk
to the cousin from your phone. It is the only feature in the framework
that sends your content to a third party. It stays off until it is fully
configured, and refuses to start otherwise.

Code: `cousin_lib/telegram.py` (entry point `telegram_main`, CLI
`cousin-telegram`). Tests: `tests/test_telegram.py`. Provisioning and lifecycle:
`cousin_lib/telegram_admin.py`.

## What it does

- **What leaves the machine:** HTTPS to `api.telegram.org`, and nothing
  else. The bridge long-polls Telegram (`getUpdates`) for your messages
  and posts the cousin's replies back. It opens no port and needs no
  public URL or webhook. Everything in the Telegram chat passes through
  Telegram's servers.
- **What it touches locally:** for a tmux cousin, the cousin's own
  chat server on `127.0.0.1:<[chat] port>`, through `POST /api/send`
  ([reference/chat-api.md](reference/chat-api.md)), and its chat store.
  It never types into the cousin's terminal and keeps no chat history of
  its own. The chat server owns all of that: recall, typing the line into
  the terminal, the presence marker, correction capture and chat hooks.
  For a runner cousin (`[agent] runner` in cousin.toml) there is no
  terminal: the bridge stores an inbound message in the cousin's
  `data/chat.db` and delivers it to the runner's inbox itself, then
  touches the presence marker, captures a correction and fires chat
  hooks, the steps `/api/send` takes. Replies are read back from the
  cousin's `data/chat.db` on both lanes (what `GET /api/history` returns,
  read in the bridge's own process), so a runner cousin needs no chat
  server and no `[chat] port` for the bridge.
- **Its only state:** where it is, in `data/telegram-bridge.json`
  ([below](#the-state-file)).
- **What it depends on:** the bridge belongs to its cousin, like the
  chat server. It starts and stops with the cousin
  ([when it runs](#6-when-it-runs)). If a tmux cousin's chat server is
  down, the bridge retries until it is back.

One bridge serves one cousin. For two cousins on Telegram, create two
bots and run two bridges.

## Interface

A Telegram message from an allowed operator lands in the cousin's chat
exactly as if it had been typed in the console, in the thread named for
that operator. The cousin's replies in that thread go back to Telegram,
whether the message came from Telegram or from the console.

| direction | Telegram side | chat side | status |
|---|---|---|---|
| in | text message | a normal line from `<operator name>` (`relay_inbound`) | works |
| in | photo, with or without a caption | an image attachment. The caption is the text, or `[photo]` if there is none. Telegram's largest size is taken, up to 10 MB (`_MAX_INBOUND_BYTES`); a bigger photo is logged and skipped | works (0.9.0) |
| in | voice, video, sticker, document, anything else | not relayed. The log says `skipped a message from <id> that is neither text nor a photo` | not supported |
| in | a message from an id not in `operators` | dropped, and the stranger gets no answer. The log says `rejected message from unauthorized Telegram id <id>` | by design |
| out | a text reply | `sendMessage` to every Telegram id of that thread's operator | works |
| out | an image reply (`cousin-reply --image`, `cousin-image`) | `sendPhoto`, uploaded as a file (`_tg_upload`), with the reply text as the caption | works (0.11.0) |
| out | a video reply (`cousin-reply --video`, `cousin-video`) | `sendVideo`, with the caption | works (0.11.0) |
| out | a voice reply (`cousin-voice`, an mp3) | `sendAudio`, with the caption (`_upload_spec`). It arrives as an audio file, not as a Telegram voice note | code path present, not live-tested |

Outbound attachments ride the reply row: `cousin-reply --image` or
`--video`, and the media commands, record `attachment_kind` and
`attachment_path` on the row ([chat](chat.md)), and the bridge uploads
that file. It relays nothing else from the cousin's disk.

The bridge checks each attachment before uploading it (`_unsendable`).
A file that is missing, a photo over 10 MB, or any other file over
50 MB (Telegram's bot upload limits, `_UPLOAD_LIMITS`) is skipped
with a log line such as `reply 14650: attachment <path> is 12.3 MB, over
Telegram's 10 MB limit, skipped`. The **whole reply** is skipped,
caption included, so the operator gets nothing on Telegram for it. It
is still in the console. Without this check, a missing file would look
like a transient error and hold the thread's cursor on that reply for
good.

The Telegram chat and the console thread are the same conversation only
if the operator's `name` in `[telegram]` matches the thread you use in
the console. Thread names ignore case and treat spaces as underscores
(`normalize_chat_user` in `cousin_lib/server/storage.py`). Use the same
name as the cousin's `[operator] name`. Then Telegram messages count as
the operator's too, for the recall suffix and correction capture, which
key off that name (`_is_operator` in `cousin_lib/server/app.py`). An
entry with no `name` lands in a thread called `operator`
(`_thread_name`).

## Set it up

Steps 1-4 are done once per cousin. By hand they need a shell on the
host. Section 5 does steps 3 and 4 from the console instead. Either way,
the token never appears on a command line or in a chat.

### 1. Create a bot

In Telegram, open **@BotFather**, send `/newbot`, and pick a display name
and a username ending in `bot`. BotFather answers with the token, which
looks like `123456789:AA...`. Anyone who has the token controls the bot,
so treat it like a password. Don't paste it into a cousin's chat, a
tracker item or a shell command. If it leaks, send `/revoke` to
BotFather, then save the new token: in the console panel, which
restarts the bridge, or by hand (step 3), followed by a restart of the
cousin.

### 2. Find your numeric Telegram user id

The allowlist uses the numeric id, not your `@username`. The id is a
number such as `88487857`. The bridge can show it to you, without any
third-party bot:

1. Do steps 3 and 4 with a placeholder id:
   `operators = [{ user_id = 1, name = "ana" }]`. The bridge runs only
   when at least one operator is set (`ready` in `telegram_admin.py`).
2. Start the cousin, which starts the bridge ([section 6](#6-when-it-runs)),
   and send the bot any message.
3. `data/telegram.log` shows `rejected message from unauthorized
   Telegram id 88487857 (not in operators)`, and the console lists the
   sender under "waiting to be added". That number is your id. Replace
   the placeholder with it, in the console or in `cousin.toml` followed by
   a restart of the cousin.

A lookup bot such as @userinfobot also shows your id, but that
means messaging a third party.

### 3. Write the token file

The token lives in a file under the framework root, and `[telegram]
token_file` names it relative to the root. The convention is
`config/telegram/<slug>.token`. `config/` is gitignored. Create the file
private first, then put the token in with an editor, so the token never
passes through your shell history or a process list:

```
mkdir -p config/telegram
install -m 600 /dev/null config/telegram/wren.token
$EDITOR config/telegram/wren.token      # paste the token, save
```

The bridge reads the file and strips surrounding whitespace
(`load_bridge_config`). It does not check the file mode, so the `600`
is up to you.

### 4. Add the `[telegram]` table to `cousin.toml`

```toml
[telegram]
enabled = true
token_file = "config/telegram/wren.token"    # relative to the framework root
operators = [{ user_id = 88487857, name = "ana" }]
```

| key | meaning |
|---|---|
| `enabled` | must be `true` |
| `token_file` | the token file, relative to the framework root |
| `operators` | the allowlist: `{ user_id, name }` per person. Several entries are allowed. Entries that share a `name` share one thread, and each reply goes to every id with that name |

The bridge refuses to start (exit 2, the reason on stderr) if any of the
following is true (`load_bridge_config`):

| stderr says | fix |
|---|---|
| `no [telegram] section; the bridge is off for this cousin` | add the table |
| `[telegram] enabled is not true` | set `enabled = true` |
| `no framework root: ...` | run it on a home at `<root>/cousins/<slug>`, or set `FRAMEWORK_ROOT` |
| `no bot token at <path>; ...` | write the token file (step 3) |
| `no operators configured; ...` | add at least one `{ user_id, name }` |

### 5. Provisioning from the console

The cousin's inspector has a **Telegram** panel ([console](console.md)) that
does steps 3 and 4 without a shell:

1. Paste the token from @BotFather and save it. It is written to
   `config/telegram/<slug>.token` (0600) and checked with `getMe`; the
   panel shows the bot's @name and never the token again.
2. Open the bot in Telegram and press **Start**. You are not an operator
   yet, so nobody is served; the panel shows you under "waiting to be
   added" with your numeric id. On a fresh bot no bridge runs yet (it
   needs an operator), so the console itself reads the bot's pending
   messages (`getUpdates` without an offset, which confirms nothing and
   never runs beside a live bridge). Check the id and @name, then click
   **add**.
3. Switch the bridge **on**.

The same routes are in the console API reference (`/api/cousins/<slug>/telegram`).

### 6. When it runs

The bridge belongs to its cousin, like the chat server: it starts when the
cousin starts (a console start, `cousin-start@`, a flip) if `[telegram]` is
enabled and complete, and stops when the cousin stops. Its pid is in
`data/telegram.pid` and its output in `data/telegram.log`. A token or operator
change from the console restarts it. There is no separate service unit: two
bridges polling the same bot make Telegram answer 409 Conflict.

To run it by hand, `--home` alone is enough on a standard install. The
root comes from `FRAMEWORK_ROOT` or from the home's location:

```
cousin-telegram --home cousins/wren
# or: COUSIN_HOME=$PWD/cousins/wren cousin-telegram
```

### 7. Press Start in Telegram

Open the bot in Telegram and press **Start**. A bot cannot send the
first message to a user. Until you press Start, every reply fails with
HTTP 400. The bridge treats a 4xx as permanent: `data/telegram.log` shows
`reply <id> to <user id> rejected, skipped: HTTP 400: Bad Request: chat
not found`, and the reply is **not** retried (`relay_outbound`,
`_permanent`). Replies the cousin wrote before you pressed Start are not
delivered later.

Pressing Start sends `/start` to the bot, and the bridge relays it like
any other text, so the cousin sees a `/start` line in its chat.

Then send a message and check that the cousin answers on Telegram.

## The state file

`<home>/data/telegram-bridge.json` (`load_cursors`, `save_cursors`):

```json
{"tg_offset": 512345678, "threads": {"ana": 14650}}
```

- `tg_offset` is the next Telegram update to fetch. It moves past an
  update only once the update is relayed or permanently rejected
  (`pump_inbound`).
- `threads` has one cursor per operator thread: the last chat row id
  handled. It moves past a reply only once the reply is delivered or
  permanently rejected (`pump_outbound`).
- The file is written atomically after every step (a temp file, then a
  rename), so a restart resumes where the bridge stopped.
- On the first start, and for a thread with no cursor, the bridge begins
  at the thread's newest row. It does not send your chat history
  to Telegram. A missing or unreadable file counts as a first start.
- To re-send from a given point, stop the bridge, edit the thread's
  number, and start it again. Deleting the file only resets the bridge to
  "from now on".
- `login_notified_since` is the `since` of the last `data/login-required.json`
  the bridge relayed (below); absent until the first one.

## Login and billing notices

For a runner cousin, when its account needs a login or its billing stopped
it (`data/login-required.json`, see
[configuration](configuration.md#datalogin-requiredjson)), the bridge sends
one line to every id in `[telegram] operators` (not just the thread that
happened to be open): `<slug>: account <account> needs a login (on <host>).
<action>.`, or `... has a billing problem ...` for a billing stop. It sends
this once per `since`: a login that is fixed and fails again later (a new
`since`) is notified again, but the same stop is not repeated every poll.
This rides the bridge's own send path (`tg_send_text`), separately from chat
relay; a notice a cousin's own `relay_notice` (`cousin-account login|token
--via <slug>`, the sign-in URL) posts as a chat row from `cousin-account`
reaches Telegram through the ordinary outbound pump instead, like any other
row in the cousin's chat.

A login code pasted back through Telegram is diverted by the bridge like one
pasted in the chat page: never delivered to the cousin and never kept in its
chat history. It does stay in Telegram's own message history, which the
framework cannot reach; the code is single-use and tied to that login flow,
but you may delete the message.

## Errors and delivery

- **Transient** (network errors, a chat server that is down, Telegram
  5xx, rate limits (429)): logged (`inbound error, retrying` / `outbound
  error, retrying`) and retried on the next pass. The bridge does not
  exit and nothing is lost. Delivery is at least once: when one of
  several operators fails, the retry can send the reply again to the
  ones who already had it.
- **Permanent** (any other 4xx, such as a bot the operator blocked, or no
  Start pressed yet): logged with the message id and skipped, so one bad
  message cannot hold up the ones behind it.
- An HTTP error in the log carries the server's reason, taken from
  Telegram's `description` or the chat server's `error`
  (`_describe`, 0.11.0): `HTTP 403: Forbidden: bot was blocked by the
  user`, not just `HTTP Error 403`.
- A pass runs about every 5 seconds (`run_bridge`, `poll_interval`).

## Security notes

- The allowlist is the perimeter. With no operators the bridge will not
  run, because an open bot would serve whoever finds its username.
- Strangers get no answer, so the bot never confirms it exists. Their
  ids are in `data/telegram.log`. The five newest are also kept in
  `data/telegram-pending.json` (`note_refused`), and the console offers
  them as "waiting to be added". Anyone who messages the bot can land
  on that list, so check the id and @name before you click **add**.
- The token is read only from the file. It is never a flag, an
  environment variable or a config value, so it doesn't end up in `ps`,
  shell history or `cousin.toml`.
