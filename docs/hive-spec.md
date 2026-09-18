# Hive specification: cross-machine cousins

**Unconfigured, there is no hive: no queen runs, no token is minted,
no socket listens, and every cousin is single-machine.** The hive is
the only feature that crosses a machine boundary, so its off state is
absolute - a fresh install has zero hive surface, and enabling it is a
deliberate act that starts a server and mints credentials. Nothing
about the hive is implicit.

Enabling it is one of two acts: write `config/hive.toml` with
`enabled = true` and a `public_url` (the console becomes the queen, on
its own port, see "The console is the queen"), or run `cousin-hive
serve` (a standalone queen). Both run the same route code
(`cousin_lib.hive.handle_request`) over the same store
(`<root>/shared/hive/hive.db`). With `config/hive.toml` absent or
`enabled = false`, the console answers every `/hive/` path 404, lists
no remote cousin, and never creates the store.

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
- **Tokens do not expire or rotate automatically.** Revocation is an
  operator act (`cousin-hive revoke <slug>`, or Revoke on the node's
  card): every live token of the slug is marked revoked in the store
  and answers 401 on every route from the next request. Rotation is
  revoke plus a rebuild (a slug whose tokens are all revoked gets a
  new one), then replacing `node.env` on the node.

## The routes

Every route but health takes `Authorization: Bearer <token>`; an
unknown or revoked token is 401 on every route. JSON in, JSON out; a
body over 1 MiB is refused 413 before it is read.

- `GET /hive/health` -> `200 {"status": "ok"}`. The only
  unauthenticated route.
- `POST /hive/memory` `{"text": str, "scope": "own"|"shared" (default
  "own"), "kind": str?}` -> `{"ok": true, "id": int}`. A token may
  write only into a scope it carries (403 otherwise); empty text is
  400.
- `GET /hive/recall?q=<query>&k=<int, default 3, at most
  50>&min_score=<float>` -> `{"memories": [text, ...], "results":
  [{"text", "score", "slug", "ts"}, ...]}`. `memories` is the
  first-release shape, kept for clients that read only it. Readable
  rows follow the scope rule above (own = the caller's own rows;
  shared = every token carrying shared). **Semantic** when
  `config/embedding.toml` is present and its service answers: the
  query and each row are embedded (a row's vector is cached in the
  store, computed on append in the background or at the first recall
  that needs it, and recomputed when the model changes), rows scoring
  at least `min_score` (default: embedding.toml `[recall] min_score`,
  else 0.45) are returned best first. **Substring** otherwise, or when
  the service fails anywhere in the pass: every hit scores 1.0,
  newest first. A failed service is skipped for 30 s so a dead
  embedder costs one timeout, not one per recall.
- `POST /hive/msg` `{"to": slug, "id": msg_id, "body": str}` ->
  `{"ok": true}`. Sender from the token; idempotent on (recipient,
  id) so a retry does not duplicate.
- `GET /hive/inbox?since=<id>&wait=<seconds>` -> `{"messages":
  [{"id", "from", "body"}]}` for the caller's own inbox past `since`.
  With `wait` > 0 (capped at 30) the request is held until a message
  arrives or the wait ends; only that request's thread waits, the
  server keeps answering everything else. Outbound-only reachability
  is enough.
- `POST /hive/checkin` `{"port": int, "name": str, "role": str,
  "version": str?}` -> `{"ok": true, "checkin_seconds": int}`.
  Records the node in the queen's `nodes` table: slug (from the
  token), name, role, host (the peer address the queen saw), port,
  version, last seen. A node calls it on start and then every
  `checkin_seconds` (the queen's answer; 60 by default). A node is
  online while its last checkin is within 2.5 periods.

## The console is the queen

With `config/hive.toml` enabled, the console's own server and port
answer the routes above under `/hive/`. They are the node side and
never touch the operator side: `/hive/` bypasses the console's
network guard and login (a node has no session and may be on any
network) and authenticates with the hive token alone, while no
`/api/` route reads an `Authorization` header, so a hive token opens
nothing an operator can do. The console adds, behind its normal login
(`docs/console-spec.md`, "Hive: the console as queen"):

- **Remote cousins as cards.** Every checked-in node, and every slug
  with a minted token that has not checked in ("built, not checked
  in"), is a card marked remote: host:port, last seen, online or
  offline, revoked. No start, stop, restart or pane: the console does
  not run it. The card turns online the moment a checkin brings it
  back; offline follows from time on the next fleet refresh.
- **Chat.** The chat view proxies to the node's chat server at the
  host and port of its last checkin, presenting the node's own token.
  This is the one path that dials a node, and it is opt-in per node:
  a node answers on the network only when built with `NODE_HOST`
  other than loopback (the console's build dialog does so by default,
  `cousin-spawn-node --listen-all` on the CLI), and off loopback its
  chat routes answer only a caller holding its token. A node that
  stays on loopback is still a full member of the bus; its card just
  has no reachable chat.
- **Build ("egg-drop").** The spawn dialog's "Remote (another
  machine)" builds the node archive with `queen_url` = `public_url`
  into an owner-only directory under `shared/hive/downloads/` and
  answers a one-time download link
  (valid 15 minutes or for one download, then deleted) plus the
  install command. The operator runs the command on the other
  machine; nothing is pushed.
- **Revoke and forget.** Revoke marks the tokens revoked (401 from
  now); Forget removes a revoked node's rows (its memory and inbox
  stay, they are the fleet's record).

## Importing the previous framework's queen

`cousin-hive import-legacy --tokens <tokens.json> [--memory-dir
<dir>] [--shared-slugs a,b]` reads the previous queen's store:

- `tokens.json` maps each token string to `{"slug", "scope"}`. Each
  token is inserted with the SAME string, so a deployed node only
  changes its queen URL. Scope mapping: the old vocabulary was the
  same two words; `own` -> `own`, `shared` -> `shared`, a missing
  scope -> `own` + `shared` (the old queen's default), any other word
  is dropped and reported, and a token left with nothing gets `own`.
  A token string that already maps to another slug here is reported
  and not imported.
- `<dir>/<slug>/memory.jsonl` holds the old append-only records
  (`text`, `kind`, `ts`, `seq`, an embedding). Each becomes a memory
  row for that slug with its original `ts` and `kind`, in `own` scope
  (or `shared` for a slug named in `--shared-slugs`, the old queen's
  shared-corpus list). The old vectors are not carried over (another
  model and task-prefix convention); rows are embedded lazily. Every
  row carries an origin key (`legacy:<slug>:seq:<n>`), so re-running
  the import adds nothing.

The command prints counts, never a token.

## Node spawning

Enrolling a node is an operator act, documented as manual: mint a token
on the queen (`cousin-hive mint <slug>`), place it plus the queen URL
on the node, run the node runtime. The framework provides the queen,
the token minting, and the node runtime; the operator provides the
machine and moves the token. There is no remote-push spawn in v1 - a
framework that could install itself on another machine on the
operator's behalf is a larger trust surface than the hive needs, and
it ships as a documented manual step or not at all. The console's
build dialog keeps that line: it builds the archive and hands out a
one-time link, and the operator runs the install on the node.
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
- **Missing, wrong or revoked token:** the queen returns 401; the
  client turns that into a local fallback, not a crash.
- **Failed checkin:** logged by the node and retried every period;
  never fatal. The node's card shows it offline until one succeeds.
- **No embedder, or a failing one:** recall is substring, stated in
  every result's score of 1.0.
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

Remote-push spawn, TLS termination, automatic token rotation or
expiry, peer-to-peer direct routing, and any cross-operator
federation are out. The v1 hive is one operator's authed message bus
across their own machines, and no further.
