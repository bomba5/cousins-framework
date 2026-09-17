# Phase 4: Hive Node Builder and the Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-contained hive node archive that runs a cousin on another machine against this framework's queen, and a framework-wide in-flight work tracker with a CLI and a store the console can serve.

**Architecture and constraints:** as the phase 0 plan's Global Constraints. The queen here is `cousin_lib/hive.py` (`cousin-hive mint/serve/send/recall`, bearer tokens, `HiveStore`); build on it, do not fork it. The node template is stdlib-only and ships under `templates/hive-node/`.

**Spec:** `docs/port-plan.md` (phase 4).

---

### Task 1: Hive node builder and template

**Files:** Create `cousin_lib/spawn_node.py` (script `cousin-spawn-node = "cousin_lib.spawn_node:spawn_node_main"`), `templates/hive-node/cousin_node.py`, `templates/hive-node/install.sh`, `templates/hive-node/CLAUDE.md`, `templates/hive-node/node.env.example`, `docs/deploying-a-node.md`; modify guide, provenance, pyproject, template row (re-render wren), README link; tests `tests/test_spawn_node.py`, `tests/test_hive_node.py`.

**Source:** `SOURCE/spawn_node.py`, `SOURCE_TEMPLATES=/home/user/framework/templates/hive-node/` (`cousin_node.py`, `install.sh`, `CLAUDE.md`), `SOURCE_DOC=/home/user/framework/docs/deploying-a-cousin-abroad.md` (framing; rewrite), `SOURCE_TESTS/test_spawn_node.py`, `test_hive_node_opencode_brain.py`, `test_hive_node_peer_routing.py`.

**Contract:** `cousin-spawn-node <slug> --queen-url URL --name N --role R [--out DIR]` mints a token through the queen's mint path (or `--token` when the queen is remote), renders the node identity from the template, and writes `<out>/<slug>-node.tar.gz` containing `cousin_node.py`, `install.sh`, `CLAUDE.md`, `node.env` (queen URL, token, slug, chat port) and a `README`. `cousin_node.py` is a stdlib node: its own small chat server (`/health`, `/api/send`, `/api/history` in this framework's shapes), a brain loop that recalls from and remembers to the queen each turn, a gateway that posts to a configured home chat server; the backend is the command in `node.env AGENT_CMD` when set, else a placeholder brain that echoes and records, so a fresh node is never dead on arrival. `install.sh` needs only python3 and outbound network to the queen; refuses without `node.env`. Tests run the node with a loopback queen (`build_queen`) and a fake agent command.

- [ ] Tests first; implement; commit `"hive: node builder and the node template"`.

---

### Task 2: The in-flight work tracker

**Files:** Create `cousin_lib/tracker.py` (script `cousin-tracker = "cousin_lib.tracker:tracker_main"`), `docs/tracker-spec.md`; modify guide, provenance, pyproject, template row (re-render wren), README link; tests `tests/test_tracker.py`.

**Source:** `SOURCE/tracker.py` (a thin HTTP client of the source console; here the STORE lives in the library and the console calls it), `SOURCE_TESTS`: grep `tracker` in `/home/user/framework/tests/unit` and `tests/auto/test_ui_tracker_e2e.py` for the shapes.

**Contract:** sqlite at `<root>/data/tracker.db`, items `{id, title, domain, state, tags, owner, notes, created_at, updated_at}`, states `open|active|blocked|done|dropped`; library functions `add`, `update`, `set_state`, `list_items(domain=None, state=None, tag=None)`, `show`, `delete`, all concurrency-safe (one connection per call, `BEGIN IMMEDIATE`); CLI `cousin-tracker add|update|state|list|show|delete` with `--json`; a documented JSON shape for the console (`docs/tracker-spec.md`). Ids never recycle.

- [ ] Tests first; implement; commit `"tracker: framework-wide in-flight work store and CLI"`.
