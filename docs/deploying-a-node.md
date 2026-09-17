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

1. **Build** an archive on the queen machine (`cousin-spawn-node`). It
   mints the node's bearer token in the queen's store and renders its
   identity.
2. **Move** the archive to the other machine yourself and run
   `./install.sh` there.
3. The node comes alive with only outbound reach to the queen. Nothing
   on the framework host needs to reach it.

The archive carries the token, so the archive is the secret: move it
the way you would move a credential, and delete it once installed.
There is no remote-push spawn by design (the spec says why).

## Prerequisites

On the queen machine: a running queen (`cousin-hive serve`) and a
checkout with `templates/hive-node/` (any framework root).

On the node machine: `python3` (3.11+, stdlib only; nothing to pip
install) and outbound network to the queen URL. Confirm before
installing:

```
python3 -c 'import urllib.request; print(urllib.request.urlopen("http://queen.example.invalid:8101/hive/health").read())'
```

Transport is plain HTTP; across an untrusted network put a TLS
terminator in front of the queen and give the node the https URL.

## Quick start

```
# on the queen machine: mint the token, render the identity, bundle
cousin-spawn-node kestrel --root . \
    --queen-url http://queen.example.invalid:8101 \
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
                  [--port N] [--out DIR] [--root ROOT]
```

| option | meaning |
|---|---|
| `--queen-url` | the queen as the NODE will reach it (not as the builder does) |
| `--name`, `--role` | the identity rendered into the node's `CLAUDE.md` |
| `--token` | a token already minted on a remote queen; without it one is minted in this root's queen store (`shared/hive/hive.db`), idempotent per slug |
| `--home-chat` | a chat server (this framework's `/api/send`) the node's `[tell-home: ...]` marker posts to; absent, the marker is a no-op |
| `--agent-cmd` | the backend command to bake into `node.env`; empty means the placeholder brain |
| `--port` | the node's own chat port, default 8210 |
| `--out` | where `<slug>-node.tar.gz` lands, default the current directory; never write it into a tree you publish (it is a binary and a secret) |
| `--root` | the framework root holding `templates/`; falls back to `FRAMEWORK_ROOT` |

The token is never printed; it is in the archive. Rebuilding the same
slug reuses its token, so a rebuilt archive does not orphan a node
already deployed with the first one.

## What is in the archive

`<slug>-node.tar.gz` unpacks to `<slug>-node/`:

| file | what it is |
|---|---|
| `cousin_node.py` | the runtime: a stdlib chat server (`/health`, `/api/send`, `/api/history` in this framework's shapes), the brain loop, the inbox poller |
| `install.sh` | the local installer; needs python3 and the queen, refuses without `node.env` |
| `CLAUDE.md` | the node's rendered identity: name, role, voice, the marker protocol, the hard rules. Edit it on the node to shape the cousin. |
| `node.env` | configuration including the bearer token, mode 0600. `templates/hive-node/node.env.example` documents every key. |
| `README` | the two steps, for whoever finds the directory later |

## How a turn works

Every message, whether posted to the node's `/api/send` or delivered to
its inbox on the queen by another cousin, becomes one turn:

1. **Recall.** The node asks the queen's `/hive/recall` per keyword of
   the message and gathers the top hits (the queen matches
   substrings; a whole sentence is the wrong query).
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
curl -X POST http://127.0.0.1:8210/api/send \
    -H 'Content-Type: application/json' \
    -d '{"user": "Sam", "message": "hello"}'
curl 'http://127.0.0.1:8210/api/history?user=Sam'

# from any cousin holding a token:
cousin-hive send --queen http://queen.example.invalid:8101 \
    --token hive_<token> --to kestrel --id m1 "are you there"
cousin-hive recall --queen http://queen.example.invalid:8101 \
    --token hive_<token> "greenhouse"
```

## Unconfigured and failure behaviour

- **No reachable queen:** the node keeps serving its own chat; recall
  returns nothing and remember is a no-op until the queen answers.
  Nothing crashes and nothing is retried in a storm (one poll per
  `NODE_POLL_SECONDS`).
- **Wrong or retired token:** the queen answers 401; to the node that
  is the same as no queen.
- **No `HOME_CHAT_URL`:** `[tell-home: ...]` is dropped silently.
- **No `AGENT_CMD`:** the placeholder brain; `/health` says so
  (`"brain": "placeholder"`).

## Updating, rotating, removing

- **Update the runtime:** copy a newer `cousin_node.py` over the old one
  and restart the unit; `node.env` and `CLAUDE.md` are untouched.
- **Rotate the token:** mint a new one on the queen (`cousin-hive mint`
  writes the same store the builder does; retire the old row by hand
  in `shared/hive/hive.db`), replace `HIVE_TOKEN` in `node.env`,
  restart.
- **Remove:** `systemctl [--user] disable --now cousin-node-<slug>`,
  delete the directory, delete the token row on the queen.

## What this deliberately does not do

The node is not registered in the framework host's `cousins/`
directory and does not appear in the console: the console enumerates
homes on its own filesystem, and a node has none there. The node is
reached the way the spec allows, over the authed bus, and by whoever
can reach its own chat port. There is no unauthenticated node-to-peer
post and no hardcoded gateway; the one gateway is the `--home-chat`
you configured, and it is off when you did not.
