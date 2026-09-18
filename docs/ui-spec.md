# Web console specification

**A console process that dies loses nothing but its pixels.**

That sentence is the whole constraint, and it is testable: any
proposed feature answers "what would this lose on `kill -9`", and the
answer decides the argument without appealing to taste. If a feature
would lose state, that state belongs in a store the framework already
owns, and the feature renders or requests it - it does not hold it.

This page states the constraints the console lives under. The wire it
serves, route by route, is `docs/console-spec.md`; running it under
systemd and putting a login in front of it is `docs/operations.md`
section 8; the end-to-end walk of one operator session, in one
process on loopback, is `tests/console/test_console_e2e.py`.

## What the console serves

`cousin-console` is the operator's daily surface over the fleet, the
source framework's console ported view for view: the overview, the
cousin cards with their inspector and editors (role, `CLAUDE.md`,
loops, hidden), spawn and dismiss with the archive, flip now and flip
at a time, chat with the live terminal pane typed into from the
browser, jobs with live log tails, the memory tree with the
shared-tier review, loops with drift, tokens from the harness
transcripts, the tracker, settings, account, and the restart panel.
Dropped with the operator's decision: media generation (the display
of generated media and inbound images came back on 2026-09-18: inline
players, a media on/off toggle and a viewer), the backlog board, the embedded game, presence and
engagement, the GPU box controls and the legacy agents table. The
scheduler the source console carried is not ported: the loops daemon
owns recurring work here (`docs/loops-spec.md`).

## What the console owns

Exactly two things, both stated in `docs/console-spec.md`:

- **Browser sessions**, in memory. A restart logs everyone out; that
  is the cost of the sentence at the top, and it is the whole cost.
- **The users file**, `config/console-users.json`, written by
  `cousin-console adduser <name>` (password from a prompt, never argv),
  PBKDF2-HMAC-SHA256 with a per-user salt, mode 0600, atomic.

Everything else it shows is a projection of a store some other
component owns, read on every request, and every command it accepts
writes through the library a CLI would use:

- **No registry.** The filesystem is the registry (`FrameworkConfig`).
  The console enumerates cousins by reading it, never by caching a
  list that becomes the truth; a cousin spawned by hand appears on the
  next request with no restart.
- **No scheduler state, no tick.** The loops daemon owns firing and
  fire timestamps; the console reads `loops-state`, the fire log and
  the request store, computes drift from them, and submits request
  rows (fire, timed flip). It never fires a loop.
- **No job store.** The jobs module owns `jobs.db`; the console reads
  the same file and runs the same maintenance any reader may run.
- **No tracker store.** `cousin_lib/tracker.py` owns `tracker.db`; the
  console calls the same functions `cousin-tracker` does.
- **No identity generation.** The template is the single identity
  source; spawn from the console is `spawn.create_cousin` then
  `spawn.start_cousin`, never a `CLAUDE.md` of its own.
- **No second implementation of a single-sited mechanism.** tmux
  session creation (`spawn.start_cousin`), terminal injection
  (`server.injection`, whose process-wide lock the pane's input route
  takes), flip (`flip.flip`) and dismissal (`spawn.dismiss_cousin`,
  archive first, refuse on a failed archive) each have exactly one
  site; the console calls it there.
- **No chat store.** The per-cousin chat servers are the chat surface;
  every chat route is a proxy to the cousin's server by its configured
  host and port, the inbound-image route serves the cousin's own inbox
  directory, and the console never stores a message.
- **No preference store.** Sidebar groups and the unread watermark
  live in the browser's local storage; the source kept them
  server-side, which made the console an owner.

The testable form of all of it: **every piece of state the console
displays must be readable, and every command it issues must be
issuable, without the console running.** A CLI or another client can
already do everything the console does; the console is a convenience
over that surface, not a gateway to it. (The source inverted this -
its CLIs depended on the console being up: job registration, the
tracker, presence, chat discovery. That inversion does not recur.)

## Reverse-dependency rule

Nothing outside the console may depend on the console being up. Where
the source had CLIs calling the web server, the public tools reach the
store directly, which the earlier milestones already arranged: jobs
owns its database, the tracker owns its database, chat discovers
cousins from the filesystem, the loops daemon owns scheduling. No CLI,
no daemon, no cousin process calls the console to get its work done,
and the console's own MCP provisioning of a new cousin happens inside
`spawn.create_cousin`, not in a console handler.

## Architecture

One stdlib package, `cousin_lib/console/`: a threaded `http.server`
(`app.py`) with a method-and-path router that the route modules
register on at import (`ROUTE_MODULES`), so the server is the sum of
its modules and a route lives next to the store it projects (`auth`,
`routes_fleet`, `routes_jobs`, `routes_loops`, `routes_memory`,
`routes_shared`, `routes_admin`, `routes_tracker`, `proxy`, `pane`,
`sse`, `static`, `tokens`). A handler returns `(status, json)`, a raw
byte body, or a stream; the server owns the wire conventions
(`application/json`, `Cache-Control: no-store`, malformed JSON is a
400, an exception is a 500 with its message) so no module re-derives
them.

The root is `--root` or `FRAMEWORK_ROOT`, resolved by the shared rule
and exported once (`docs/configuration.md`); the console's own settings
are flags (`--port`, `--host`, `--tmux-bin`, `--tmux-socket`,
`--secure-cookie`), never a file it owns.

### The network guard, then the users file

The guard is the boundary, exactly as for the chat server:
`NetGuard` from `config/net-allowlist.json`, loopback plus configured
CIDRs, fail-closed, stated honestly as an address-trust model rather
than a user-identity one. It runs before anything else on every
request, including DELETE and both SSE streams; a denial is 403.

Above it, the users file adds a per-person boundary for installs whose
console more than the operator can reach. When it holds at least one
user, every `/api/*` route except `POST /api/auth/login` and
`GET /api/auth/me` needs a session cookie (`console_session`,
HttpOnly, SameSite=Strict, Secure behind TLS, idle expiry 30 days);
static files need none, because the login form is part of the page.
There is no silent-bypass ladder: no loopback exemption, no "trusted
LAN disables auth" tier, because a bypass that wide is
indistinguishable from no auth and is named as such rather than
disguised as a convenience. When the file is absent the console is
open to everyone the guard admits and says so: `me` reports
`configured: false` and the account panel shows the `adduser` line.

Authorization is not faked. No per-user scopes are enforced, so none
are stored: the users file has no `scope` field, because a
stored-but-unread permission reads as a boundary that is not there.

A GET never mutates and never shells out to another host; state
changes are POST and DELETE only.

### The two live streams

Both are projections; a dropped connection costs a reconnect and a
fresh first frame, never data. The client backs off from 1 s to 15 s.

- **`GET /api/events`**, one per dashboard tab. `data:` frames of
  `{kind, data}`: a `snapshot` on connect (the same rows the GET views
  return), then `cousins-refresh`, `loops-refresh`, `cousin-status`,
  `job-add`/`job-update`/`job-delete`, `cousin-flip`, `loop-fire`,
  `tracker-change`. A poller inside the console diffs the stores other
  components own (jobs and the request store every 2 s, the fleet and
  loops every 15 s) and the command handlers emit at the moment they
  act. The poller is a differ, never a second source of truth: it
  holds the previous reading only to know what changed.
- **`GET /api/pane/stream?cousin=<slug>`**, one per open pane. Named
  events: a full `pane` frame on connect (ending with the cursor
  position so the browser caret lands where tmux's is), `pane` again
  on change, `geom` when the geometry changes, `heartbeat` after 3 s
  of silence, `: tick` comments between polls. Bytes come from
  `capture-pane` polling on the console's tmux binary and socket, the
  same resolution the injection module uses, so there is no tap file
  to start and remove per connection; a remote cousin (`[chat] host`)
  runs the same commands over `ssh <host>`. Input goes the other way
  through `POST /api/pane/input` under the injection lock, so a chat
  delivery and a keystroke never interleave.

Every stream closes cleanly on disconnect: the generator's `finally`
releases its queue, and the connection is not kept alive for a second
request.

### The static bundle and its one runtime fetch

The frontend is committed under `cousin_lib/console_static/`: an
`index.html`, six JSX files compiled in the browser by Babel
standalone, a stylesheet, a manifest and an SVG mark without brand
marks. The server serves them from a suffix allowlist (`.html .jsx
.js .css .json .webmanifest .svg .png .ico`), with the resolved path
required to sit strictly inside that directory (403 on traversal, 404
otherwise), `Cache-Control: no-store`, and a `?v=<mtime>` stamp on
each local `src`/`href` in the page so a redeploy is never masked by a
browser cache. No private literal survives in these files: the gate
scans them, and a test checks that every `/api/...` literal they
contain is a route on the contract and none is a route it dropped.

One exception to the no-external-fetch posture is made knowingly and
stated here as well as in the contract: the script tags in
`index.html` load React 18, Babel standalone, marked, mermaid and
xterm from a CDN, pinned (with SRI hashes where the CDN serves stable
builds), so the views port as-is without a maintainer build step.
Those tags are **the one runtime network fetch a browser makes** for
this console; the backend itself fetches nothing from outside the
install. An adopter who cannot allow that fetch vendors the same files
under the static directory and edits the tags; nothing else changes.

### The retired alias

`cousin-ui` was the first, view-only console written against the
earlier version of this page. It is retired: for one release the name
stays in `pyproject.toml` as an alias that prints a pointer to
`cousin-console` on stderr and then runs the console with the same
flags (`--root`, `--host`, `--port` are shared), so an install script
that still names it keeps working and says what to change. The old
module and its static directory stay importable for their tests until
the alias goes; nothing new is written against them.

## Stated limits

- **Identity is address-asserted below the users file.** With auth
  not configured, the guard is the only boundary, as for the chat
  server; do not expose the console past a network where every client
  is trusted.
- **Best-effort live.** A view can lag its store by its poll
  interval; when the console and a store disagree, the store wins and
  the console is stale, never the reverse.
- **The pane is a terminal over HTTP, not a PTY.** Keystrokes are
  batched client-side and mapped to key names server-side; an
  application relying on exotic escape sequences may not receive them,
  by design (a dropped sequence never becomes a stray Escape).
- **Sessions do not survive a console restart.** The cost of owning
  nothing durable but the users file.
- **The pane and the fleet's liveness column are only as true as
  tmux.** They read the session through the console's binary and
  socket; a cousin running on another socket reads as stopped until
  `--tmux-socket` names it.
