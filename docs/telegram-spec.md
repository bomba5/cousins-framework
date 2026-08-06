# Telegram bridge specification

**This is the framework's first feature that sends to a third party,
and unconfigured it opens no connection and sends nothing.** Every
other surface stays on the operator's own machines; the telegram
bridge relays a cousin's chat to and from Telegram's servers. So the
spec states the perimeter first:

- **What leaves the box:** outbound HTTPS to `api.telegram.org` only -
  polling for the operator's messages, and sending the cousin's
  replies (text and generated media) back. Nothing is sent anywhere
  else, and nothing inbound listens on a port.
- **To whom:** a single configured operator (or a small allowlist of
  Telegram user ids). The bridge refuses to run with no operator
  configured, because a bot with no allowlist serves anyone who finds
  its username.
- **Under whose credentials:** a per-cousin bot token, read from a
  gitignored config path. The token authorizes the cousin's own bot,
  nothing wider.
- **Unconfigured:** the bridge does not start. A missing token,
  `enabled` unset, or an empty operator list is a REFUSAL with a
  message naming what to fix - never a silent no-send that looks like
  a working bot dropping messages.

## Transport: long-poll, no inbound listener

The bridge long-polls `getUpdates`; it opens no inbound port and needs
no public URL. This is deliberate: an inbound webhook would be a new
listening surface on the operator's machine and a public endpoint to
secure, and the stranger default must add neither. Webhook transport
may be documented as an option for installs that already have a public
endpoint, but the default and the tested path is pull-only.

## Configuration (per cousin)

The bridge is per-cousin: one process per cousin, one bot, one
operator binding. In the cousin's `cousin.toml`:

```toml
[telegram]
enabled = true
token_file = "config/telegram-<slug>.token"   # gitignored
operators = [{ user_id = 123456, name = "Sam" }]
```

- `token_file` points at a gitignored file holding the bot token; the
  token never lives in `cousin.toml` (which is not gitignored) or in
  code.
- `operators` is the allowlist. A message from any Telegram user id not
  in it is dropped, silently to the sender and logged locally - an
  unauthorized DM is not acknowledged.

## The relay

The bridge is a pure relay between Telegram and the cousin's own chat
server; it never touches the terminal directly.

- **Inbound:** an authorized message becomes a `POST /api/send` to the
  cousin's chat server, which stores and delivers it exactly as any
  other inbound chat. Media in a Telegram message is downloaded (with
  a size cap) and handed to the chat server as an inbound attachment,
  the same path a browser upload takes.
- **Outbound:** the bridge polls the cousin's own chat history for the
  cousin's replies (rows whose type is the cousin's slug) and relays
  them to the operator's Telegram chat - text as a message, an
  attachment as the corresponding Telegram media send from the local
  file.

Because it goes through the chat server, the outbound content filter
and every other chat-path rule already apply; the bridge adds no
second copy of any of them.

## Failure behavior

- Missing/disabled/no-operators at startup: refuse to start (exit
  non-zero, message naming the cause). A supervisor restarting it will
  keep failing loudly until configured - which is correct; a
  half-configured bot should not appear to work.
- A transient Telegram API or network error mid-run: log and retry
  after a backoff; never crash and never drop the cursor. Un-relayable
  media is replaced by a text note, never silently omitted.
- The delivery cursor (last handled update, per operator) is persisted
  under the cousin's home so a restart does not replay or skip.

## Stated limits

- **Everything in the operator's Telegram chat transits Telegram's
  servers**, under Telegram's terms, which is inherent to the feature
  and stated so it is a choice. The framework sends only to
  `api.telegram.org` and only the configured operator's thread.
- **The bot token is a bearer credential.** Anyone holding the token
  file controls the bot; it is gitignored and should be readable only
  by the cousin's account.

## Consciously excluded

Group chats, inline queries, multi-bot fan-out, webhook transport as a
default, and any relay of one operator's messages to another are out.
The bridge connects one cousin to one operator over Telegram, and no
further.
