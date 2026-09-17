# Phase 3: The Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the source framework's web console as-is (browser-compiled React, no build step) minus the media and backlog surfaces, on a stdlib backend that serves the same API the views call, so the operator keeps every view in daily use: overview, cousins (cards, inspector, editors, spawn, dismiss with archive, flip), chat with the live terminal pane, jobs, memory, loops with drift, tokens, tracker, settings and account, restart panels.

**Architecture:** One backend module `cousin_lib/console/` (a package: `app.py` server + routes split by area, `auth.py`, `sse.py`, `pane.py`, `proxy.py`) on `http.server` with threads, behind the existing network guard, owning NO state of its own except sessions and `users.json` (the ui-spec rule: a console that dies loses nothing). Static files under `cousin_lib/console_static/` (React + Babel from a CDN as the source does; note it in the spec as the one runtime network fetch a browser makes). Everything install-specific comes from `config/` or `cousin.toml`. Excluded by the operator: media generation and display of generated media, the backlog view, the embedded game; there are no arc views (the arc machinery has no console surface). The scheduler role of the source backend is NOT ported: the loops daemon owns recurring work here.

**Tech Stack:** Python 3.11+ stdlib; JSX compiled in the browser by Babel standalone; `python3 -m unittest discover -s tests`; gate as in phase 0.

**Spec:** `docs/port-plan.md` (phase 3) and, once Task 0 lands, `docs/console-spec.md` (the API contract every later task codes against).

## Global Constraints

Identical to the phase 0 plan's Global Constraints, plus: no private literal survives in the static files (names, hosts, addresses, paths: the gate self-test scans them too); every route the retained views call is in `docs/console-spec.md` before it is implemented; a route the views do not call is not ported.

---

### Task 0: The console API contract (docs only)

**Files:** Create `docs/console-spec.md`; modify `docs/ui-spec.md` (point at it), README (link).

**Source:** `SOURCE_UI` (the source console directory, handed to you by the integrator) (`backend.py` 5995 lines: every `"/api/..."` route, its method, query and body parameters, response JSON shape, SSE event names and payloads; `static/app.jsx`, `views.jsx`, `cousins.jsx`, `chat.jsx`, `ui.jsx`, `data.jsx`: every `fetch(`/`EventSource(` call). Read-only; no private literal in the output.)

**Deliverable:** for every route a retained view calls: method, path, parameters, response shape (keys and types), errors; SSE streams (`/api/events`, `/api/pane/stream`) with event names and payloads; the auth model (users file, session cookie, `/api/auth/me`, change-password); the list of views retained and dropped (dropped: backlog, media display of generated files, the GPU box controls on the host view, games, presence/engagement, agents legacy tracker; retained: everything else); a section "private literals to genericize" listing each occurrence in the static files by file and what replaces it (a config value, a cousin field, or removal); a section "backend behaviours that move": scheduler role (loops daemon), MCP provisioning (spawn), dismissal archive (already in delete path spec). No code.

- [ ] Write the spec; run the docs coherence tests; commit `"docs: the console API contract"`.

---

### Task 1: Backend A (fleet, jobs, loops, memory, tokens, shared review, auth, admin, tracker)

**Files:** Create `cousin_lib/console/__init__.py`, `app.py` (server, router, guard, static), `auth.py`, `routes_fleet.py`, `routes_jobs.py`, `routes_loops.py`, `routes_memory.py`, `routes_shared.py`, `routes_admin.py`, `routes_tracker.py`; script `cousin-console = "cousin_lib.console.app:console_main"` (retire `cousin-ui`: keep the name as an alias printing a pointer for one release); tests `tests/console/test_*.py` per route file.

**Contract:** `docs/console-spec.md`. Fleet data comes from `FrameworkConfig.list_cousins()` and each cousin's chat `/health`; start/stop/flip/spawn/dismiss call `spawn.start_cousin`, `spawn.stop_cousin` (add if missing), `flip.flip`, `spawn.create_cousin`, and a dismiss that archives the whole home to `<root>/data/dismissed/<slug>-<ts>.tar.gz` before removal and refuses when the archive fails. Jobs from `jobs.py`; loops from `loops.py` (status, requests, per-cousin `[[loops]]`, drift = last fire vs schedule); memory = `cousin-memory search` per cousin; tokens from each cousin's transcript usage through the harness seam (absent: empty with a stated reason); shared review = `shared_tier` list/read/diff/promote/reject/audit; auth = `config/console-users.json` (bcrypt is not stdlib: PBKDF2-HMAC-SHA256 with per-user salt), session cookie, `/api/auth/me`, change-password; admin restart of a cousin = stop+start. Tracker routes call the phase 4 tracker store (`cousin_lib/tracker.py`; if it has not merged yet, code against its documented functions and stub the import guard).

- [ ] Tests red first per route file with temp roots and fake cousins; implement; commit `"console: backend A (fleet, jobs, loops, memory, shared, auth, admin, tracker)"`.

---

### Task 2: Backend B (chat proxy, pane, events, search, static)

**Files:** Create `cousin_lib/console/proxy.py` (messages, send, reactions, archive, inbound image files, search, peer-messages: all proxied to the cousin's chat server by its port, never a second store), `pane.py` (`/api/pane` capture, `/api/pane/stream` SSE of `tmux capture-pane` deltas, `/api/pane/input` through the injection module, `/api/pane/resize`), `sse.py` (`/api/events`: cousin state changes, job updates, loop fires, tracker changes; a poller that diffs, never a second source of truth), static serving of `console_static/` with a suffix allowlist and traversal check; tests `tests/console/test_proxy.py`, `test_pane.py`, `test_events.py`, `test_static.py`.

- [ ] Tests red first (proxy against a loopback fake chat server; pane against a fake tmux binary; SSE framed correctly and closing cleanly; static refuses traversal); implement; commit `"console: backend B (chat proxy, pane, events, static)"`.

---

### Task 3: Frontend A (`app.jsx`, `views.jsx`, `cousins.jsx`, `ui.jsx`, `data.jsx`)

**Files:** Create `cousin_lib/console_static/{index.html,app.jsx,views.jsx,cousins.jsx,ui.jsx,data.jsx}`; tests `tests/console/test_static_files.py` (every file present, no private literal, no reference to a dropped view or route, every `fetch(` path exists in the spec).

**Source:** `SOURCE_UI/static/*.jsx`. Port as-is minus: `BacklogView` and its nav entry, the GPU box cards in `HostView`, the agents legacy view, anything calling a route the spec dropped. Replace every private literal per the spec's genericize list. Keep the keyboard shortcuts, SSE application, sidebar groups, spawn/flip/dismiss modals, role and CLAUDE.md editors, loops editor, token panel.

- [ ] Write the static-file tests first (they fail on missing files), port, pass; commit `"console: frontend A (shell, cousins, jobs, memory, loops, tokens, settings)"`.

---

### Task 4: Frontend B (`chat.jsx`, `styles.css`, PWA assets)

**Files:** Create `cousin_lib/console_static/{chat.jsx,styles.css,manifest.webmanifest,favicon.svg}` (icons regenerated as simple SVG/PNG without brand marks); extend `tests/console/test_static_files.py`.

**Source:** `SOURCE_UI/static/chat.jsx`, `styles.css`, `index.html`. Port the chat view, bubbles with reactions and markdown rendering, the inbound-image display for attachments (a data URI or the server's inbound file route), the live pane view with input; drop generated-media lightbox/video/audio players, engagement emitters, presence pings, favourite reactions wiring. Keep the reveal palettes only if they carry no private cue text.

- [ ] Tests first; port; commit `"console: frontend B (chat, pane, styles, PWA)"`.

---

### Task 5: Integration, docs, unit template

**Files:** `tests/auto_console/test_console_e2e.py` (spawn a cousin in a temp root, start its chat server, start the console on a free port, log in, list cousins, send a chat message through the proxy, read history, open the pane stream for one frame, hit jobs/loops/memory/tracker), `docs/ui-spec.md` rewrite for the ported console, `docs/operations.md` (unit and first login), `systemd/cousin-console.service`, guide section, template row, provenance rows; results line by the integrator.

- [ ] Tests first; wire; commit `"console: end-to-end test, docs, unit"`.

## Self-review

- Every retained view has a backend task; every route is contract-first (Task 0); dropped surfaces are named; scheduler and MCP provisioning are explicitly not ported (already covered).
- Overlap: Tasks 1 and 2 share `console/app.py` (router registration): Task 1 creates it with a registration hook; Task 2 adds its routes through the hook. Tasks 3 and 4 share `index.html` (Task 3 owns it).
