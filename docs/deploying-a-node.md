# Deploying a node: a cousin on another machine

How to run a cousin on a machine that is not the framework host - a
small board on a desk, a VM, a box in another country - and have it be
a member of the fleet through the hive (`docs/hive-spec.md`): its
memory on the queen, reachable by the other cousins over the bus, with
its own chat surface in this framework's shapes.

## The mental model

A node only ever talks **outbound** to the queen; the queen never dials
the node. A box behind NAT, or one with no address the framework host
can see, is a first-class node. Deployment is therefore **copy-over**,
never push:

1. **Build** an archive on the queen machine (the console's spawn
   dialog, or `cousin-spawn-node`). It mints the node's bearer token
   in the queen's store and renders its identity.
2. **Move** the archive to the other machine yourself and run
   `./install.sh` there.
3. The node comes alive with only outbound reach to the queen. Nothing
   on the framework host needs to reach it.

The archive carries the token, so the archive is the secret: move it
the way you would move a credential, and delete it once installed.
There is no remote-push spawn by design (the spec says why).

## Prerequisites

On the queen machine: a running queen and a checkout with
`templates/hive-node/` (any framework root). The queen is either the
console with `config/hive.toml` enabled (the usual case: the console's
own URL is the queen URL) or a standalone `cousin-hive serve`.

On the node machine: `python3` (3.11+, stdlib only; nothing to pip
install) and outbound network to the queen URL. Confirm before
installing:

```
python3 -c 'import urllib.request; print(urllib.request.urlopen("http://queen.example.invalid:8600/hive/health").read())'
```

Transport is plain HTTP; across an untrusted network put a TLS
terminator in front of the queen and give the node the https URL.

## From the console (the usual way)

1. Turn the hive on: copy `config/hive.toml.example` to
   `config/hive.toml`, set `enabled = true` and `public_url` to the
   console as the node will reach it (`http://<lan-ip>:<console
   port>`; the console must listen on the LAN, `docs/install.md`), and
   restart the console. `GET /hive/health` on that URL now answers.
2. In the Cousins view, **spawn cousin -> Remote (another machine)**:
   slug, name, role, the node's port (8210), the brain (the
   placeholder, or an agent command line as it runs ON THE NODE:
   prompt on stdin, reply on stdout), home chat on or off (needs
   `home_chat_url` in hive.toml), and whether the console may chat
   with it (on by default: the node then listens on its network and
   answers chat only to its own token, which the console holds).
3. **Build node** answers two commands with copy buttons:

   ```
   curl -fsSL -o kestrel-node.tar.gz 'http://<queen>/hive/download/<nonce>' && tar xzf kestrel-node.tar.gz && cd kestrel-node && ./install.sh
   tar xzf kestrel-node.tar.gz && cd kestrel-node && ./install.sh
   ```

   The link works ONCE and expires after 15 minutes; the archive is
   deleted from the console machine then (or when the console stops).
   It carries the node's token, so treat the link like one. Building
   the same slug again gives a new link to an archive with the same
   token.
4. Run the first command on the node machine. The card, "built, not
   checked in" until now, turns online at the node's first checkin
   (on start, then every `checkin_seconds`). Open chat from the card.

The card shows host:port (the address the console saw the checkin
come from, and the port the node reported), when it was last seen,
and its runtime version. It has no start, stop or pane: the node's
own service manager runs it. **Revoke** (with a confirm) turns its
token off: every queen call answers 401 and the card says revoked;
**Forget** then removes the card.

## From the command line

```
# on the queen machine: mint the token, render the identity, bundle
cousin-spawn-node kestrel --root . \
    --queen-url http://queen.example.invalid:8600 \
    --name Kestrel --role "watches the greenhouse" \
    --home-chat http://home.example.invalid:8090 \
    --out /tmp/build
#   -> built /tmp/build/kestrel-node.tar.gz

# move it (your way), then on the node machine:
tar xzf kestrel-node.tar.gz && cd kestrel-node && ./install.sh
```

`install.sh` writes a systemd unit (a system unit when passwordless
sudo is available, else a user unit with linger), starts it, and waits
for `/health`. `./install.sh --print-unit` shows the unit without
touching anything; `./install.sh --foreground` runs the node in the
shell for a box without systemd. It refuses to run without `node.env`
beside it.

## `cousin-spawn-node` reference

```
cousin-spawn-node <slug> --queen-url URL --name N --role R
                  [--token T] [--home-chat URL] [--agent-cmd CMD]
                  [--port N] [--listen-all] [--out DIR] [--root ROOT]
```

| option | meaning |
|---|---|
| `--queen-url` | the queen as the NODE will reach it (not as the builder does) |
| `--name`, `--role` | the identity rendered into the node's `CLAUDE.md` |
| `--token` | a token already minted on a remote queen; without it one is minted in this root's queen store (`shared/hive/hive.db`), idempotent per slug |
| `--home-chat` | a chat server (this framework's `/api/send`) the node's `[tell-home: ...]` marker posts to; absent, the marker is a no-op |
| `--agent-cmd` | the backend command to bake into `node.env`; empty means the placeholder brain |
| `--port` | the node's own chat port, default 8210 |
| `--listen-all` | bind the node's chat on 0.0.0.0 so the console can proxy chat to it; off loopback the node answers `/api/*` only to its own token. Default: loopback only |
| `--out` | where `<slug>-node.tar.gz` lands, default the current directory; never write it into a tree you publish (it is a binary and a secret) |
| `--root` | the framework root holding `templates/`; falls back to `FRAMEWORK_ROOT` |

The token is never printed; it is in the archive. Rebuilding the same
slug reuses its token, so a rebuilt archive does not orphan a node
already deployed with the first one.

## What is in the archive

`<slug>-node.tar.gz` unpacks to `<slug>-node/`:

| file | what it is |
|---|---|
| `cousin_node.py` | the runtime: a stdlib chat server (`/health`, `/api/send`, `/api/history` in this framework's shapes), the brain loop, the inbox poller, the checkin |
| `install.sh` | the local installer; needs python3 and the queen, refuses without `node.env` |
| `CLAUDE.md` | the node's rendered identity: name, role, voice, the marker protocol, the hard rules. Edit it on the node to shape the cousin. |
| `node.env` | configuration including the bearer token, mode 0600. `templates/hive-node/node.env.example` documents every key. |
| `README` | the two steps, for whoever finds the directory later |

## How a turn works

Every message, whether posted to the node's `/api/send` or delivered to
its inbox on the queen by another cousin, becomes one turn:

1. **Recall.** The node asks the queen's `/hive/recall` per keyword of
   the message and gathers the top hits (the queen recalls by meaning
   when it has an embedder, else by substring, and a whole sentence
   is the wrong substring query).
2. **Think.** With `AGENT_CMD` set in `node.env`, the node runs that
   command with the prompt on stdin (the identity file, the marker
   doctrine, what was recalled, the message) and takes stdout as the
   reply; a non-zero exit or a timeout becomes a stated "brain is
   offline" line, never a dead node. With `AGENT_CMD` empty the
   placeholder brain answers: it greets, echoes, and surfaces a
   recalled memory, so a fresh node is never dead on arrival.
3. **Act on markers.** `[remember: fact]` appends to the shared corpus
   on the queen; `[tell <slug>: text]` sends through the queen bus
   (sender from the token, never from the body); `[tell-home: text]`
   posts to the configured home chat server. A marker whose payload is
   a `<placeholder>` is the syntax being quoted and is left alone.
4. **Remember.** The exchange is appended to the queen under the node's
   own scope, every turn, so the fleet's memory of the node does not
   depend on the node's disk.
5. **Reply.** The cleaned reply is stored in the sender's thread; a turn
   that came over the bus is also answered over the bus.

The inbox cursor is persisted under `data/`, so a restart does not
replay messages already answered.

Beside the turns, the node checks in with the queen on start and then
every `checkin_seconds` the queen answers with (60 until it has
answered): its port, name (`NODE_NAME`), role (`NODE_ROLE`) and
runtime version. That is what puts its card online in the console. A
failed checkin is logged once per reason and retried; it never stops
the node.

## The brain command

`AGENT_CMD` is one command line. The runtime `shlex`-splits it (no
shell), runs it in the node directory with `COUSIN_SLUG`, `NODE_NAME`
and `NODE_DIR` in the environment, writes the prompt to stdin, and
reads the reply from stdout, within `AGENT_TIMEOUT_SECONDS`. Any
agent CLI with a non-interactive "prompt in, answer out" mode fits;
wrap one in a two-line script if it needs flags or a different input
channel. The identity file rides in the prompt, so a backend that does
not read `CLAUDE.md` from its working directory still gets it.

## Verifying

```
# on the node:
curl http://127.0.0.1:8210/health
#   -> {"status": "ok", "slug": "kestrel", "port": 8210, "brain": "placeholder"}
# (from the node itself: loopback needs no token; from elsewhere the
#  chat routes need -H "Authorization: Bearer <its token>")
curl -X POST http://127.0.0.1:8210/api/send \
    -H 'Content-Type: application/json' \
    -d '{"user": "Sam", "message": "hello"}'
curl 'http://127.0.0.1:8210/api/history?user=Sam'

# from any cousin holding a token:
cousin-hive send --queen http://queen.example.invalid:8600 \
    --token hive_<token> --to kestrel --id m1 "are you there"
cousin-hive recall --queen http://queen.example.invalid:8600 \
    --token hive_<token> "greenhouse"

# on the queen machine: who checked in, when
cousin-hive nodes
```

## Unconfigured and failure behaviour

- **No reachable queen:** the node keeps serving its own chat; recall
  returns nothing and remember is a no-op until the queen answers.
  Nothing crashes and nothing is retried in a storm (one poll per
  `NODE_POLL_SECONDS`).
- **Wrong or revoked token:** the queen answers 401; to the node that
  is the same as no queen. Its checkin logs `HTTP 401` once.
- **No `HOME_CHAT_URL`:** `[tell-home: ...]` is dropped silently.
- **No `AGENT_CMD`:** the placeholder brain; `/health` says so
  (`"brain": "placeholder"`).

## Updating, rotating, removing

- **Update the runtime:** copy a newer `cousin_node.py` over the old one
  and restart the unit; `node.env` and `CLAUDE.md` are untouched.
- **Rotate the token:** revoke it (`cousin-hive revoke <slug>` or the
  card's Revoke), build the slug again (a slug with only revoked
  tokens gets a new one), replace `HIVE_TOKEN` in `node.env` from the
  new archive, restart.
- **Remove:** `systemctl [--user] disable --now cousin-node-<slug>`,
  delete the directory, then revoke and forget it on the queen
  (`cousin-hive revoke <slug> && cousin-hive forget <slug>`, or the
  card's Revoke and Forget).
- **Move from the previous framework's queen:** `cousin-hive
  import-legacy --tokens <old tokens.json> --memory-dir <old store>`
  on the new queen keeps every token string, so a deployed node only
  changes `QUEEN_URL` in its `node.env` (see `docs/hive-spec.md`).

## What this deliberately does not do

The node is not registered in the framework host's `cousins/`
directory: it has no home there, no tmux session, no pane, and the
console cannot start, stop or flip it. It appears in the console as a
remote card because it checks in with the queen the console hosts
(with a standalone `cousin-hive serve` queen it does not appear in a
console at all). It is reached over the authed bus, and its chat
only by a caller on its own box or one holding its token. There is no
unauthenticated node-to-peer post and no hardcoded gateway; the one
gateway is the home chat you configured, and it is off when you did
not.
