# Hive specification: cross-machine cousins

**Unconfigured, there is no hive: no queen runs, no token is minted,
no socket listens, and every cousin is single-machine.** The hive is
the only feature that crosses a machine boundary, so its off state is
absolute - a fresh install has zero hive surface, and enabling it is a
deliberate act that starts a server and mints credentials. Nothing
about the hive is implicit.

## What crosses the boundary, and how it is authed

The hive is a message bus with one authenticated route and no others:

- **The queen** is a small HTTP server an operator runs on one
  machine. It holds the durable state: a per-cousin inbox and a shared
  memory corpus.
- **Nodes** are cousins on other machines. A node reaches the queen
  **outbound only** - it needs no inbound reachability, no public URL,
  no open port of its own. This is deliberate: a node behind NAT
  works, and a node exposes nothing.
- **Every queen route requires a bearer token** that maps to a slug;
  the sender's identity is taken from the token, never from the
  request body, so a node cannot claim to be another. The one
  unauthenticated route is `/health`.

**Two shortcuts the source framework used on its trusted LAN do NOT
ship**, because a public install has no trusted LAN: the direct
node-to-peer delivery that posted to another cousin's chat endpoint
unauthenticated, and the hardcoded gateway post to a named host. In
the public hive, every cross-machine message goes through the authed
queen bus. There is one route and it is authenticated; a second,
unauthenticated path would be the hole this rewrite exists to close.

## Trust model, stated plainly

- **The bearer token is the identity.** Whoever holds a node's token
  is that node. Tokens are minted on the queen, stored there with a
  restrictive mode, and delivered to the operator to place on the
  node. There is no automatic enrollment: a node exists because the
  operator put a token on it.
- **Scope is on the token.** A token carries what its node may read -
  its own inbox always, the shared corpus only if its scope includes
  it. Scope gates reads at the queen; it is not advisory.
- **`own` means this node's own.** A memory written in `own` scope is
  recalled only by the token of the slug that wrote it (and only when
  that token carries `own`); no other node's token reaches it, whatever
  its scope. Writes are gated the same way: a token may only write
  into a scope it carries, so an own-only node cannot plant text in
  the shared corpus.
- **Transport is plain HTTP unless the operator fronts it with TLS.**
  This is stated, not hidden: the queen speaks HTTP, and an operator
  who needs confidentiality across an untrusted network puts a TLS
  terminator in front of it. The framework does not pretend to
  encrypt what it does not.
- **Tokens do not expire or rotate automatically.** Rotation is a
  manual operator act (mint a new token, replace it on the node,
  retire the old). Stated as a limit rather than faked with a scheme
  that would need a revocation store the v1 hive does not have.

## The routes

- `POST /hive/msg` - a node sends a message to another slug's inbox.
  Sender from the token; recipient in the body; idempotent on a
  message id so a retry does not duplicate.
- `GET /hive/inbox?since=` - a node long-polls its own inbox (its slug
  from the token). Outbound-only reachability is enough.
- `POST /hive/memory` and `GET /hive/recall` - append to and query the
  shared corpus, scope-gated by the token.
- `GET /hive/health` - the only unauthenticated route.

## Node spawning

Enrolling a node is an operator act, documented as manual: mint a token
on the queen (`cousin-hive mint <slug>`), place it plus the queen URL
on the node, run the node runtime. The framework provides the queen,
the token minting, and the node runtime; the operator provides the
machine and moves the token. There is no remote-push spawn in v1 - a
framework that could install itself on another machine on the
operator's behalf is a larger trust surface than the hive needs, and
it ships as a documented manual step or not at all.
`cousin-spawn-node` is that manual step made repeatable: it mints the
token in the queen's store, renders the node's identity, and writes
one archive the operator moves and installs by hand
(`docs/deploying-a-node.md`). The node runtime it ships is stdlib
only, speaks the routes above with its bearer token, and keeps its own
chat surface in this framework's shapes.

## Unconfigured / failure behavior

- **No queen configured:** hive client calls fail toward local
  fallback - a cousin with no reachable queen behaves as a
  single-machine cousin, recall and remember becoming local no-ops.
  The absence is inert, never an error that stops the cousin.
- **Missing or wrong token:** the queen returns 401; the client turns
  that into a local fallback, not a crash.
- **Queen unreachable:** the node keeps serving its own chat locally
  and retries the bus; nothing cross-machine is lost because the inbox
  is durable and long-polled from a cursor.

## Stated limits

- **The hive trusts its tokens absolutely.** Confidentiality of the
  bus is the token's secrecy plus whatever TLS the operator adds;
  there is no per-message signing or end-to-end encryption.
- **The shared corpus is readable by every scoped node.** A memory
  appended to the shared tier reaches every node whose token includes
  shared scope; treat it as fleet-visible, the same posture as the
  local shared-memory tier.

## Consciously excluded

Remote-push spawn, TLS termination, token rotation/revocation
machinery, peer-to-peer direct routing, and any cross-operator
federation are out. The v1 hive is one operator's authed message bus
across their own machines, and no further.
