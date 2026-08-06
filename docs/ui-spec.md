# Web UI specification

**A UI process that dies loses nothing but its pixels.**

That sentence is the whole constraint, and it is testable: any
proposed feature answers "what would this lose on `kill -9`", and the
answer decides the argument without appealing to taste. If a feature
would lose state, that state belongs in a store the framework already
owns, and the feature renders or requests it - it does not hold it.

## What the UI is not allowed to own

The source framework put its scheduler state, its cousin registry, its
job store, and its identity generator inside the web server process,
and that ownership was the root of the defects the earlier milestones
fixed. The public UI owns none of it:

- **No registry.** The filesystem is the registry (`FrameworkConfig`).
  The UI enumerates cousins by reading it, never by caching a list
  that becomes the truth.
- **No scheduler state, no tick.** The loops daemon owns firing and
  fire timestamps; the UI reads `loops-state` and the request store,
  and submits requests. It never fires a loop.
- **No job store.** The jobs module owns its SQLite database; the UI
  reads the same file.
- **No identity generation.** The template is the single identity
  source; the UI invokes `cousin-spawn`'s library, never emits a
  CLAUDE.md of its own.
- **No second implementation of a single-sited mechanism.** tmux
  creation (`spawn.start_cousin`), terminal injection
  (`server.injection`), and flip (`flip.flip`) each have exactly one
  site; the UI triggers them there or through their request rows.
- **No chat store.** The per-cousin chat servers are the chat surface;
  the UI proxies views of them and never stores a message.

The testable form of all six: **every piece of state the UI displays
must be readable, and every command it issues must be issuable,
without the UI running.** A CLI or another client can already do
everything the UI does; the UI is a convenience over that surface, not
a gateway to it. (The source inverted this - its CLIs depended on the
UI being up. That inversion does not recur.)

## Architecture

A thin HTTP daemon, stdlib only, serving two things:

1. **JSON read views** over the stores the framework already
   persists: the cousin registry (filesystem), jobs (jobs.db), loop
   state and the request store (the loops daemon's files), shared-tier
   listings, trace and memory. Each view is a projection; none is a
   cache that anything else trusts.
2. **Committed static assets.** The frontend ships as files in the
   repository - no runtime CDN, no in-browser transpile. A maintainer
   build step (documented, the maintainer's toolchain) produces the
   bundle; adopters clone and serve. This is the same reasoning as the
   gate's no-external-fetch rule applied to the browser: a console
   that cannot boot without four external hosts is not reproducible.

Commands are POST routes that write through the same libraries a CLI
uses - request rows to the loops daemon, `cousin-spawn` for creation,
the jobs module for job mutations - never in-process state.

Live updates are server-sent events OR polling; both are projections
of durable state, so a dropped connection or a restarted daemon costs
a refresh, never data. The event stream carries no state a reader
could not reconstruct from a plain GET.

## Reverse-dependency rule

Nothing outside the UI may depend on the UI being up. Where the source
had CLIs calling the web server (job registration, presence, chat
discovery), the public tools reach the store directly - which the
earlier milestones already arranged: jobs owns its database, chat
discovers cousins from the filesystem, the loops daemon owns
scheduling. The UI spec inherits that and forbids re-introducing the
dependency: no CLI, no daemon, no cousin process calls the UI to get
its work done.

## Authentication

The network guard is the boundary, exactly as for the chat server:
loopback plus configured CIDRs, fail-closed, stated honestly as an
address-trust model rather than a user-identity one. Beyond it:

- Every route is guarded, including DELETE. (The source left cousin
  destruction ungated; a rewrite that reproduced that would be
  shipping the bug in new code.)
- No silent-bypass ladder. There is one guard - the network
  allowlist - and it is the same one the chat server documents; there
  is no separate "trusted LAN disables auth entirely" tier, because a
  bypass that wide is indistinguishable from no auth and should be
  named as such, not disguised as a convenience.
- No side-effecting GET. A GET never mutates, never shells out to
  another host; state changes are POST/DELETE only.
- Authorization is not faked. If per-identity scopes are not enforced,
  the schema does not carry a `scope` field that reads as if they
  were - a stored-but-unread permission is worse than none, because it
  reads as a boundary that is not there.

## Stated limits

- **Identity is address-asserted**, like the chat server: the guard is
  the only boundary; do not expose the UI past a network where every
  client is trusted.
- **The UI is best-effort live.** A view can lag the store by its
  refresh interval; nothing the UI shows is authoritative over the
  store it read from. When they disagree, the store wins and the UI is
  stale, never the reverse.

## Consciously excluded from v1

The pane terminal (the source's `tmux pipe-pane` streaming with its
xterm byte-parser is the single most defect-scarred mechanism in the
monolith; it ships when it can be built against the spec with the same
care the chat server got, not before), presence tracking (no durable
home yet - it needs a daemon-owned store, deferred with that
decision), engagement, media generation views, and the household's
network-map and remote-GPU integrations. The v1 UI renders the fleet,
the loops, the jobs, and the chat, and submits the requests that drive
them. That is a console a stranger can run; the rest is the
household's, and ships behind it or not at all.
