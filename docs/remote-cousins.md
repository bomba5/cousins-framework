# Remote cousins

A remote [cousin](glossary.md#cousin) runs on another machine: a Pi on a desk, a VM, a box
behind NAT. It keeps its memory on the console machine, talks to the
other cousins through it, and shows up in the console as a card you can
chat with. Read this when one machine is not enough.

The remote side is small on purpose. The node is one Python file with no
dependencies, it only ever makes outbound connections, and the console
never pushes anything onto it. You build an archive on the console
machine, carry it over, and run `./install.sh`.

## How it fits together

The console machine runs the **queen**: a small HTTP API under `/hive/`
that holds an [inbox](glossary.md#inbox) per cousin and a memory store. Each remote cousin is
a **node**. A node calls the queen; the queen never calls the node. That
is why a node behind NAT, or on a network the console can't see, works
fine.

Every node has a bearer token minted on the queen. The token is the
node's identity: whoever holds it is that node, and the queen takes the
sender from the token, never from the request body.

```
  node (Pi, VM, ...)                      console machine
  +---------------------+   outbound     +---------------------------+
  | cousin_node.py      | -------------> | cousin-console            |
  |  chat on :8210      |   HTTP(S)      |  /hive/*   the queen      |
  |  brain, poller,     |   + token      |  /api/*    your login     |
  |  checkin            |                |  shared/hive/hive.db      |
  +---------------------+                +---------------------------+
            ^                                        |
            +------ chat proxy, only if you allow it +
```

The one connection that goes the other way is optional: when you tick
"chat from this console", the console dials the node's chat port to
proxy your messages, and presents the node's own token when it does.

## Turn it on

The hive is off until you turn it on. With `config/hive.toml` missing or
`enabled = false` there is no `/hive/` route (every path is a 404), no
remote card, no token, and nothing is created under `shared/hive/`.

```sh
cd ~/cousins-framework
cp config/hive.toml.example config/hive.toml
```

```toml
# config/hive.toml
enabled = true

# The console as the NODES reach it: this machine's address on their
# network, and the console's port.
public_url = "http://192.0.2.10:8600"

# How often nodes check in, in seconds (default 60, at least 5).
#checkin_seconds = 60

# Optional: the local cousin a node's [tell-home: ...] marker reaches,
# through the queen, with the node's own token.
#home_cousin = "wren"
```

Nodes have to reach the console, so it can't stay on its default
`127.0.0.1`. Give the [supervisor](glossary.md#supervisor) the `--console-host 0.0.0.0` drop-in from
[install](install.md#reaching-the-console-from-the-lan), then restart it
(every running cousin restarts with it and resumes its session):

```sh
systemctl --user daemon-reload && systemctl --user restart cousin-supervisor
curl http://192.0.2.10:8600/hive/health
# {"status": "ok"}
```

If `hive.toml` is there but broken (bad TOML, `public_url` not an http(s)
URL, `checkin_seconds` below 5), the hive stays off and the console logs
one line saying why: `cousin-console: hive off: ...`. The spawn dialog
won't offer "Remote" either.

The `/hive/` routes sit outside the console login and the network guard,
because a node has no session and can be on any network. They accept the
hive token and nothing else. It works the other way too: no `/api/` route
reads an `Authorization` header, so a hive token gets you nothing in the
console.

## Build one from the console

This is how I do it.

1. In Cousins, open **spawn cousin** and pick **Remote (another
   machine)**. The option only appears when the hive is on.
2. Fill in:
   - **name** and **slug**. The slug is what the queen knows the node as
     (`^[a-z][a-z0-9_-]{1,31}$`). It can't be the slug of a local cousin.
   - **role**: one line. It goes into the node's `CLAUDE.md` and on its
     card.
   - **node port**: the node's own chat port on its machine, default
     8210.
   - **brain**: `placeholder`, or `agent command` with the command line
     as it runs on the node (see [The brain](#the-brain)).
   - **home chat**: only available when `home_cousin` is set. The
     node's `[tell-home: ...]` then reaches that cousin through the queen.
   - **chat from this console**: on by default. The node then listens on
     `0.0.0.0` instead of loopback, so the console can proxy chat to it.
3. Click **build node kestrel**. You get two commands with copy buttons:

   ```sh
   # download and install
   curl -fsSL -o kestrel-node.tar.gz 'http://192.0.2.10:8600/hive/download/<nonce>' && tar xzf kestrel-node.tar.gz && cd kestrel-node && ./install.sh

   # install an archive you already copied over
   tar xzf kestrel-node.tar.gz && cd kestrel-node && ./install.sh
   ```

4. Run one of them on the other machine. All it needs there is `python3`
   (3.11 or newer) and outbound access to `public_url`.

The card appears straight away as "built, not checked in" and turns
online at the node's first checkin.

The download link works once and expires after 15 minutes. The archive
is deleted from the console machine after the download, at expiry, or
when the console stops, whichever comes first. If you miss the window,
build again with the same slug: you get a new link, and the node keeps
the same token.

**The archive carries the node's token. Treat the link and the file like
a password.** Anyone who downloads it can act as that node on the queen.
Delete the tarball once the node is installed.

## Build one from the command line

`cousin-spawn-node` does the same thing without the console. Run it on
the queen machine, in the checkout:

```sh
cousin-spawn-node kestrel --root . \
    --queen-url http://192.0.2.10:8600 \
    --name Kestrel --role "watches the greenhouse" \
    --listen-all \
    --out /tmp/build
# built /tmp/build/kestrel-node.tar.gz
# it contains the node's bearer token: move it privately.
# next, on the node machine:
#   tar xzf kestrel-node.tar.gz && cd kestrel-node && ./install.sh
```

Then move the file yourself (scp, a USB stick, whatever) and run the
install line on the node. The token is never printed; it's only in the
archive. Keep `--out` outside any git tree.

Without `--token-file`, it mints the token in this root's queen store
(`shared/hive/hive.db`). Minting is idempotent per slug: building the
same slug again reuses its live token, so a rebuilt archive doesn't lock
out a node that's already deployed. A slug whose tokens are all revoked
gets a new one.

A token minted on another queen goes in by file or on standard input,
never on the command line, where every local user can read it (`ps`):

```sh
cousin-spawn-node kestrel --token-file /tmp/build/kestrel.token \
    --queen-url http://192.0.2.10:8600 --name Kestrel --role "watches the greenhouse" \
    --out /tmp/build
```

| flag | what it does |
|---|---|
| `slug` | the node's identity on the queen |
| `--queen-url URL` | required. The queen as the node will reach it, not as you reach it from here |
| `--name N`, `--role R` | required. Rendered into the node's `CLAUDE.md` |
| `--token-file PATH` | use a token already minted on another queen instead of minting one here, read from `PATH` (`-` reads standard input) |
| `--token T` | deprecated: the same, with the token on the command line, where every local user can read it. Still works, with a one-line warning on stderr |
| `--tell-home` | sets `TELL_HOME=1` in `node.env`: `[tell-home: ...]` reaches the queen's `home_cousin` (see [node.env](#nodeenv)); off when absent |
| `--agent-cmd CMD` | the brain command to put in `node.env`; empty means the placeholder |
| `--port N` | the node's chat port, default 8210 |
| `--listen-all` | bind the node's chat on `0.0.0.0` so the console can proxy chat to it. Default is loopback only |
| `--out DIR` | where `<slug>-node.tar.gz` goes, default the current directory |
| `--root ROOT` | the framework root holding `templates/`; falls back to `FRAMEWORK_ROOT` |

It exits 0 when it built the archive and 2 on a bad slug, missing
config, or unreadable template, and writes nothing in that case.

## What's in the archive

`kestrel-node.tar.gz` unpacks to `kestrel-node/`:

| file | what it is |
|---|---|
| `cousin_node.py` | the runtime: a small chat server (`/health`, `/api/send`, `/api/history`), the brain loop, the inbox poller, the checkin. Stdlib only |
| `install.sh` | the installer |
| `CLAUDE.md` | the node's identity: name, role, voice, the reply markers, the rules. Edit it on the node to shape the cousin |
| `node.env` | all settings, including `HIVE_TOKEN`. Mode 0600 |
| `README` | the install steps, for whoever finds the directory later |

`templates/hive-node/node.env.example` documents every `node.env` key;
they're also listed at the end of this page.

## What install.sh does

Run it from inside the unpacked directory. It refuses to start without
`node.env` next to it, and checks that `COUSIN_SLUG`, `NODE_PORT`,
`QUEEN_URL` and `HIVE_TOKEN` are set and that `python3` exists.

```sh
./install.sh               # install and start a systemd unit
./install.sh --print-unit  # print the unit, touch nothing
./install.sh --foreground  # run the node in this shell, no systemd
```

With no option it installs `cousin-node-<slug>.service`:

- If `sudo -n true` works (passwordless sudo), it writes a system unit to
  `/etc/systemd/system/`, set to run as you.
- Otherwise it writes a user unit to `~/.config/systemd/user/` and runs
  `loginctl enable-linger` so the node survives logout.

Either way the unit runs `python3 cousin_node.py` from the directory,
reads `node.env` as its environment, and restarts on failure after 5
seconds. The script then waits up to 20 seconds for `/health` to answer
and prints where the logs are. If the machine has no `systemctl`, it
tells you to use `--foreground` under your own [supervisor](glossary.md#supervisor).

Check it on the node:

```sh
curl http://127.0.0.1:8210/health
# {"status": "ok", "slug": "kestrel", "port": 8210, "brain": "placeholder"}
```

## The remote card

Every node the queen knows is a card in Cousins, marked `remote`, with
one of four states:

- **built, not checked in**: a token exists, the node hasn't called yet.
- **online**: the last checkin was within 2.5 checkin periods (150
  seconds with the default 60).
- **offline**: it has checked in before, but not recently.
- **revoked**: its token has been turned off.

A node checks in when it starts and then every `checkin_seconds`. The
checkin carries its port, name, role and runtime version. The card shows
host:port (the address the checkin came from, plus the port the node
reported), when it was last seen, and the runtime version. A node coming
online updates the card at once; going offline shows up on the next
fleet refresh.

A remote card has no start, stop, restart or pane. The node's own
service manager runs it, not the console. Remote cousins also stay out
of the loop and memory pickers, since there's nothing local to schedule
or browse.

**Chat.** When the card is online you get **open chat**. The console
proxies the conversation to the node's chat server at the host and port
from its last checkin, sending the node's own token. This only works if
the node was built with "chat from this console" (or `--listen-all`). A
node on loopback is still a full member of the hive, but you can't chat
with it from the console.

**Revoke** asks for confirmation, then turns off every live token of the
slug. From the next request, every queen call from that node gets 401.
**Forget** appears once it's revoked and removes the card and the token
rows. The node's memory and inbox stay on the queen; they're the fleet's
record, not the node's.

`cousin-hive nodes` lists the same thing on the queen machine:

```sh
cousin-hive nodes
# kestrel          online                 192.0.2.44:8210          last seen 12s ago
```

## The brain

The node needs something to think with. There are two options, set by
`AGENT_CMD` in `node.env`.

**Placeholder** (`AGENT_CMD` empty). It greets you, echoes what you
said, and mentions a memory if it recalled one. It's there so a fresh
node answers from minute one and you can test the plumbing before
wiring in a real model. `/health` says `"brain": "placeholder"`.

**Agent command.** One command line, run on the node for every [turn](glossary.md#turn). The
runtime splits it like a shell would (no actual shell), runs it in the
node directory with `COUSIN_SLUG`, `NODE_NAME` and `NODE_DIR` in its
environment, writes the prompt to stdin and takes stdout as the reply.
The prompt is `CLAUDE.md`, the marker rules, what was recalled, and the
message, so a backend that doesn't read `CLAUDE.md` on its own still
gets it.

```sh
# node.env
AGENT_CMD=/usr/local/bin/my-agent --plain
AGENT_TIMEOUT_SECONDS=120
```

Any CLI with a "prompt in, answer out" mode fits. If yours needs other
flags or reads its input differently, wrap it in a two-line script. A
non-zero exit or a timeout becomes a "(my brain is offline right now:
...)" reply, never a dead node. After changing `node.env`, restart the
unit.

## Memory and messages

Each message becomes one turn, whether it arrived on the node's own
`/api/send` or in its inbox on the queen:

1. **Recall.** The node asks the queen's `/hive/recall` for each keyword
   of the message and keeps the top 3 hits. Recall is by meaning when
   `config/embedding.toml` is set up on the console machine and the
   embedder answers, and by substring otherwise.
2. **Think.** The brain answers.
3. **Markers.** Three markers at the end of a reply are acted on, then
   removed before the reply is stored:
   - `[remember: fact]` writes the fact to the shared memory on the
     queen, readable by every node whose token has shared scope.
   - `[tell <slug>: text]` sends a message to another cousin through the
     queen.
   - `[tell-home: text]` reaches the home cousin through the queen
     (`POST /hive/tell-home`, with the node's token), if the node was
     built with home chat (`TELL_HOME=1`). Otherwise it's dropped.
4. **Remember.** The whole exchange is stored on the queen under the
   node's own scope, every turn. Only that node can recall its own
   memories. If the node's disk dies, its memory doesn't.
5. **Reply.** The reply is stored in the sender's chat [thread](glossary.md#thread). If the
   turn came over the hive, the answer goes back over the hive too.

The node polls its inbox every `NODE_POLL_SECONDS` (default 5; 0 turns
polling off) and remembers where it got to in `data/`, so a restart
doesn't replay old messages.

Tokens minted by the console or `cousin-spawn-node` carry both scopes,
`own` and `shared`. `cousin-hive mint <slug> --scope own` gives a token
that can only read and write its own memory.

A rough edge: local cousins are not on the hive by default. A local
cousin can talk to it with a token (`cousin-hive mint wren`, then
`cousin-hive send` and `cousin-hive recall`), but nothing reads a local
cousin's hive inbox yet. If you want a node's words to land in a local
cousin's chat, set `home_cousin` to that cousin and build the node with
home chat: `[tell-home: ...]` then reaches it through the queen, which
checks the node's token and refuses a replayed message. It works the
same in the container.

```sh
# from anywhere holding a token: in a file (`-` reads stdin) ...
cousin-hive send --queen http://192.0.2.10:8600 --token-file ~/.hive-token --to kestrel --id m1 "are you there"
# ... or in HIVE_TOKEN, as a node's node.env sets it
cousin-hive recall --queen http://192.0.2.10:8600 "greenhouse"
```

`--token T` still works but is deprecated: the token is on the command
line, where every local user can read it, and it prints a one-line
warning on stderr.

## When things go wrong

- **Queen unreachable.** The node keeps serving its own chat. Recall
  returns nothing and remember does nothing until the queen is back. It
  polls at its normal pace, no retry storm.
- **Wrong or revoked token.** The queen answers 401, which the node
  treats as no queen. Its checkin logs `HTTP 401` once.
- **Checkin failing.** Logged once per distinct reason and retried every
  period. The card stays offline until one gets through.
- **Card stuck on "built, not checked in".** On the node:
  `journalctl -u cousin-node-kestrel -f` (add `--user` for a user
  unit). Check that `QUEEN_URL` in `node.env` is reachable from the
  node: `curl <QUEEN_URL>/hive/health`.
- **Online but chat fails.** The node was built on loopback, or a
  firewall on the node blocks its port from the console machine.

## Update, rotate, remove

- **Update the runtime.** Copy a newer `cousin_node.py` over the old one
  and restart the unit. `node.env` and `CLAUDE.md` stay as they are.
- **Rotate the token.** Revoke it (the card, or `cousin-hive revoke
  kestrel`), build the slug again (it gets a fresh token), copy
  `HIVE_TOKEN` from the new archive's `node.env` into the node's, and
  restart.
- **Remove.** On the node, `systemctl [--user] disable --now
  cousin-node-kestrel` and delete the directory. On the queen, revoke
  and forget it (the card, or `cousin-hive revoke kestrel && cousin-hive
  forget kestrel`). Forget refuses while the slug still has a live
  token.

## Bringing nodes over from an older queen

If you ran nodes against a previous framework's queen, import its tokens
and memory into this one:

```sh
cousin-hive import-legacy --tokens /path/to/old/tokens.json \
    --memory-dir /path/to/old/store \
    --shared-slugs kestrel
```

- `tokens.json` maps each token string to `{"slug", "scope"}`. Every
  token keeps the same string, so a deployed node only needs its
  `QUEEN_URL` changed in `node.env` and a restart. Scopes `own` and
  `shared` carry over; a missing scope becomes both; any other word is
  dropped and reported; a token left with nothing gets `own`. A token
  string that already belongs to another slug here is reported and
  skipped.
- `--memory-dir` holds `<slug>/memory.jsonl`. Each record becomes a
  memory for that slug with its original time and kind, in `own` scope,
  or `shared` for the slugs in `--shared-slugs`. Old embeddings are not
  carried over; rows are embedded again when recall needs them.

It prints counts, never a token. Running it twice adds nothing the
second time.

## Security, in plain words

- **Outbound only.** A node opens no port for the queen. The only
  inbound connection is the optional chat proxy from the console.
- **Tokens are the whole story.** Every queen route except
  `/hive/health` needs `Authorization: Bearer <token>`. The sender is
  whoever the token belongs to. A token can only write to scopes it
  carries and only read its own memory plus, with `shared`, the shared
  pool. Tokens don't expire or rotate on their own; revoking is up to
  you.
- **The node's chat is locked off loopback.** When a node listens on
  its network, `/api/send` and `/api/history` answer a non-loopback
  caller only if it presents the node's own token (the console does).
  Callers on the node itself need nothing. `/health` is open.
- **No TLS built in.** The queen speaks plain HTTP. On your own LAN
  that's usually fine. Across anything you don't trust, put a TLS
  terminator (a reverse proxy) in front of the console and give nodes
  the `https://` URL as `public_url`.
- **Anything in shared memory is fleet-visible.** Every node with
  shared scope can recall it.
- **Request bodies over 1 MiB** are refused with 413 before they're
  read.

The full route list is in [the hive API reference](reference/hive-api.md).

## A standalone queen

You don't need the console to run a queen. `cousin-hive serve` runs the
same routes over the same store, on its own port:

```sh
cousin-hive serve --host 0.0.0.0 --port 8101
```

It reads `checkin_seconds` from `config/hive.toml` if the file is there
(`--checkin-seconds` overrides it). Nodes work the same way against it,
but there are no cards and no chat proxy, since those live in the
console. Mint tokens with `cousin-hive mint <slug>` (it prints the
token) and build archives with `cousin-spawn-node --queen-url
http://<this machine>:8101`.

## Reference

### config/hive.toml

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | turns the hive on |
| `public_url` | none, required when enabled | the queen as nodes reach it; baked into every archive the console builds and used for download links |
| `checkin_seconds` | `60` | how often nodes check in; at least 5. Online means a checkin within 2.5 periods |
| `home_cousin` | empty | the local cousin `[tell-home: ...]` reaches through the queen (`POST /hive/tell-home`) |

### node.env

| key | default | meaning |
|---|---|---|
| `COUSIN_SLUG` | | the slug the token was minted for |
| `NODE_NAME` | | display name |
| `NODE_ROLE` | empty | one line, sent with each checkin and shown on the card |
| `NODE_PORT` | `8210` | the node's chat port |
| `NODE_HOST` | `127.0.0.1` | where the chat binds; `0.0.0.0` lets the console proxy chat |
| `QUEEN_URL` | | the queen, reached outbound |
| `HIVE_TOKEN` | | the node's bearer token. Secret |
| `TELL_HOME` | empty | `1`: `[tell-home: ...]` goes to the queen's `POST /hive/tell-home` with `HIVE_TOKEN` |
| `HOME_CHAT_URL` | empty | ignored since 2.0.0 (no home chat server); a node with it set and `TELL_HOME` not `1` says so at start |
| `AGENT_CMD` | empty | the brain command; empty means the placeholder |
| `NODE_POLL_SECONDS` | `5` | inbox poll interval; 0 turns it off |
| `AGENT_TIMEOUT_SECONDS` | `120` | how long one brain call may take |

### cousin-hive

| command | what it does |
|---|---|
| `cousin-hive mint <slug> [--scope own,shared]` | mint a token (or return the live one) and print it |
| `cousin-hive serve [--host 0.0.0.0] [--port 8101] [--checkin-seconds N]` | run a standalone queen |
| `cousin-hive nodes` | list known nodes, their state and last checkin |
| `cousin-hive revoke <slug>` | turn off every live token of the slug |
| `cousin-hive forget <slug>` | remove a revoked node's token and node rows |
| `cousin-hive import-legacy --tokens F [--memory-dir D] [--shared-slugs a,b]` | import an older queen |
| `cousin-hive send --queen URL [--token-file PATH] --to SLUG --id ID BODY` | send a message over the hive; the token from `PATH` (`-`: stdin), else `HIVE_TOKEN` |
| `cousin-hive recall --queen URL [--token-file PATH] QUERY` | recall memories; the token as for `send` |

`send` and `recall` still take `--token T`, deprecated (the token is on
the command line, readable by every local user; a one-line warning says
so).

The queen-side commands (`mint`, `serve`, `nodes`, `revoke`, `forget`,
`import-legacy`) find the store through `FRAMEWORK_ROOT` (or the root your `COUSIN_HOME` sits under), at
`<root>/shared/hive/hive.db`.
