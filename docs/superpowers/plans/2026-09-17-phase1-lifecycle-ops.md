# Phase 1: Lifecycle and Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the lifecycle and operations surface to parity: ready-file triggers and a transcript-size guard inside the loops daemon, session bookend macros and harness hooks, reincarnate and transplant, the prompt-cache audit, the fleet compaction sweep, the tool-surface manifest read at boot, and systemd unit templates with an operations guide.

**Architecture:** Same rules as phase 0: one module per behaviour, config seams in `config/` and `cousin.toml`, no install specifics in code, tests without services, gate-clean. The loops daemon stays the single owner of recurring work (the loops spec's "one owner" rule), so the ready-file seam and the size guard become tick steps, not new daemons. The source framework is readable at the paths the integrator hands you as `SOURCE` and `SOURCE_TESTS`; port behaviour and tests, never private names, addresses or paths.

**Tech Stack:** Python 3.11+ stdlib only. `python3 -m unittest discover -s tests`. Gate: `python3 -m cousin_lib.gate.cli --root . --denylist ~/.config/cousin-framework/denylist.txt --mode gate` (must print nothing).

**Spec:** `docs/port-plan.md` (phase 1 section).

## Global Constraints

Identical to the phase 0 plan (`docs/superpowers/plans/2026-09-17-phase0-memory-parity.md`, Global Constraints): zero third-party runtime dependencies; every new `config/` file documented in `docs/configuration.md` with an example; every new console script in `pyproject.toml`, `docs/guide.md`, the cousin template (re-render `examples/wren/CLAUDE.md`); a provenance row per new module; no private literals; no em dashes; commits authored `Jhonata Poma-Hansen <jhonata.poma@gmail.com>` with no attribution lines; one commit per task on branch `phase1/t<N>` in your worktree; TDD red first.

---

### Task 1: Ready-file seam and transcript-size guard inside the loops daemon

**Files:** Modify `cousin_lib/loops.py` (`tick`), `cousin_lib/config.py` (`harness_config` gains `flip_when_transcript_mb`), `docs/loops-spec.md` (replace "The trigger-file seam (not implemented)" with the shipped contract), `docs/configuration.md`; tests `tests/test_loops_ready.py`, `tests/test_loops_size_guard.py`.

**Source:** `SOURCE/ready_watcher.py` (the three trigger kinds: a named loop's prompt, the canonical heartbeat re-read, `<slug>-message` literal delivery), `SOURCE`'s `bin/cousin-bytes-guard` (threshold, at most one flip per run, skip stopped or non-managed cousins), `SOURCE_TESTS/test_worker_and_watcher.py`, `test_bytes_guard.py`.

**Contract:**
- Ready files: on every tick, for each cousin home, every `<home>/<name>.ready` file is a trigger. `name` matching a `[[loops]]` entry fires that loop's prompt through the daemon's normal delivery; `name == "context-heartbeat"` delivers the daemon's heartbeat composition; `name` ending in `-message` delivers the file's contents as a literal line. The file is removed only after delivery succeeded (dedup state committed after delivery, like everything else); a failed delivery leaves the file and reports once. Unknown names are removed with a report line, never delivered.
- Size guard: when `config/harness.toml` sets `flip_when_transcript_mb` (absent: off) and the cousin's `[runtime] session_id` transcript at `<transcripts_dir>/<session_id>.jsonl` exceeds it, the tick submits ONE flip request (`submit_request("flip", cousin=slug, payload={"fire_at": now + 300, "reason": "transcript over N MB"})`) for at most one cousin per tick, never a second while one is pending for that cousin.

- [ ] Tests red first: ready file fires the named loop and is removed; failed delivery keeps the file; heartbeat name; message name; unknown name removed with report; size guard submits one request; no duplicate while pending; absent seam does nothing; at most one cousin per tick.
- [ ] Implement inside `tick()` as two helpers `_fire_ready_files(...)` and `_guard_transcript_size(...)`; docs; commit `"loops: ready-file triggers and a transcript-size guard in the daemon"`.

---

### Task 2: Session bookends and harness hooks

**Files:** Create `cousin_lib/session.py` (script `cousin-session = "cousin_lib.session:session_main"`), `hooks/pre_compact.sh`, `hooks/session_checkpoint.sh`, `hooks/session_init.sh`, `docs/session-hooks.md`; modify `templates/cousin-CLAUDE.template.md` (tool row + a short "Session bookends" paragraph), `docs/guide.md`, `docs/provenance.md`, `pyproject.toml`; tests `tests/test_session.py`, `tests/test_hooks_shell.py`.

**Source:** `SOURCE/session.py` (hooks from `cousin.toml [session.start_hooks]` / `[session.end_hooks]`, bare strings or `{name, cmd}` entries, last-run record at `data/session.json`, `--skip`, `status`), `SOURCE/hooks/*.sh` (PreCompact: dump critical state to `data/pre-compact-checkpoint.md`; Stop: write `data/session-checkpoint.md`; SessionStart: print a who/where/what banner from `cousin.toml` and STATUS.md), `SOURCE_TESTS/test_session.py`.

**Contract:** `cousin-session start|end|status [--skip NAME ...]`; each hook runs with `COUSIN_HOME`, `COUSIN_SLUG`, `SESSION_PHASE` in its env, a non-zero hook is reported and does not stop the others; `status` prints the last run. Drop the source's media-specific hooks (`--no-fav-drain`, `--cosplay`, the voice check). The three shell hooks take `COUSIN_HOME` from the environment, write only under `<home>/data/`, and are executable; `tests/test_hooks_shell.py` runs each with a temp home and asserts the files.

- [ ] Tests red first; implement; commit `"lifecycle: session bookends and harness hooks"`.

---

### Task 3: Reincarnate and transplant

**Files:** Create `cousin_lib/lifecycle.py` (scripts `cousin-reincarnate = "cousin_lib.lifecycle:reincarnate_main"`, `cousin-transplant = "cousin_lib.lifecycle:transplant_main"`), `docs/lifecycle-surgery.md` (referenced from `docs/lifecycle-spec.md` and README); modify `docs/guide.md`, `docs/provenance.md`, `pyproject.toml`, template; tests `tests/test_lifecycle.py`.

**Source:** `SOURCE/lifecycle.py` (`_snapshot`, `_rewrite_role`, `cmd_reincarnate`, `_braid_memory`, `cmd_transplant`), `SOURCE_TESTS/test_lifecycle.py`; `SOURCE`'s `docs/lifecycle.md` for the framing (rewrite the prose; do not copy names).

**Contract:**
- `cousin-reincarnate <slug> --new-role "<text>" [--root R]`: snapshot the home to `<root>/data/lifecycle/<slug>/<timestamp>/` (memory, MEMORY.md, STATUS.md, CLAUDE.md, cousin.toml), ask the running cousin for a bequest by delivering a prompt and waiting up to N seconds for `data/handoff.md` to change (the same handoff window mechanism `flip.py` uses; reuse its helpers), rewrite the role line in CLAUDE.md and `cousin.toml [cousin] role`, then flip through `flip.flip()`. Every step appends to `<root>/data/lifecycle/audit.jsonl`.
- `cousin-transplant --donor A --recipient B --mode soul-donation|body-swap|merge [--root R]`: snapshot both; soul-donation copies A's memory and MEMORY.md over B's (B's memory snapshotted first); body-swap swaps the two homes' identity files (CLAUDE.md, cousin.toml name/role, self-portrait) leaving memory in place; merge braids A's MEMORY.md into B's under a dated heading and unions `memory/raw`. Both cousins flipped afterwards. Refuses when either slug is unknown or the mode is not one of the three.
- No HTTP to a console: everything goes through the filesystem registry (`FrameworkConfig.list_cousins`), `flip.flip`, and the chat server's `/api/send` only for the bequest prompt (reuse `reply`/`chat` helpers).

- [ ] Tests red first with a fake tmux and a fake flip (inject `do_flip`); implement; commit `"lifecycle: reincarnate and transplant"`.

---

### Task 4: Prompt-cache audit

**Files:** Create `cousin_lib/cache_audit.py` (script `cousin-cache-audit = "cousin_lib.cache_audit:cache_audit_main"`); modify docs/guide, provenance, pyproject, template; tests `tests/test_cache_audit.py`.

**Source:** `SOURCE/_cache_audit.py`, `SOURCE_TESTS/test_cache_audit_and_friends.py`.

**Contract:** reads the cousin's session transcripts from the harness seam (`config/harness.toml transcripts_dir`; absent: exit 2 naming the seam), takes the per-turn usage fields (`cache_read_input_tokens`, `input_tokens`, `cache_creation_input_tokens`) from assistant messages, and prints per-cousin hit rate, per-turn distribution (min, median, max), and the top suspect invalidators: files under the home (and the harness auto-memory dir when configured) whose mtime falls between two successive turns where the hit rate dropped. `--days N`, `--json`, `--diagnose` (lists the suspects with the turn pair).

- [ ] Tests red first with synthetic transcripts; implement; commit `"memory: prompt-cache audit"`.

---

### Task 5: Compaction sweep, tool-surface manifest, systemd unit templates, operations guide

**Files:** Create `cousin_lib/sweep.py` (script `cousin-sweep = "cousin_lib.sweep:sweep_main"`), `cousin_lib/tool_surface.py` (script `cousin-tool-surface = "cousin_lib.tool_surface:tool_surface_main"`), `systemd/` (unit templates with `{{ROOT}}`, `{{USER_BIN}}` placeholders and a README), `docs/operations.md`; modify `cousin_lib/boot.py` (a bounded "Tool Surface" section reading `<root>/data/tool-surface.md` when present), docs/guide, provenance, pyproject, template, README (link operations.md); tests `tests/test_sweep.py`, `tests/test_tool_surface.py`, `tests/test_systemd_templates.py`, `tests/test_boot_tool_surface.py`.

**Source:** `SOURCE/compact_sweep.py`, `SOURCE`'s `bin/build-tool-manifest`, `SOURCE`'s `systemd-units/*` (shapes only; no paths), `SOURCE`'s `bin/cousin-daily-flip` (already covered by the daemon's `flip_at`; the operations guide says so).

**Contract:**
- `cousin-sweep compact [--target index|raw|both] [--root R]`: runs `cousin-memory compact` for every cousin home under the root, one at a time, reports per-cousin results, never aborts on one failure, exits non-zero if any failed.
- `cousin-tool-surface [--root R] [--bin DIR]`: writes `<root>/data/tool-surface.md` with one line per console script (name and the first line of `--help`), deriving the script list from the installed entry points (`importlib.metadata` for this package) so nothing is hardcoded; the boot packet includes it as section "## Tool Surface" bounded to 1500 chars, degraded-marked when absent.
- `systemd/`: `cousin-loops.service` (the daemon), `cousin-sweep.service` + `.timer` (weekly), `cousin-tool-surface.service` + `.timer` (daily), `cousin-chat-server@.service` (templated per slug) and `README.md` explaining placeholder substitution with `sed` and the environment each unit needs (`FRAMEWORK_ROOT`, `PATH`). `tests/test_systemd_templates.py` asserts every unit parses as an INI file, references only placeholders (no absolute paths), and every `ExecStart` names a console script that exists in `pyproject.toml`.
- `docs/operations.md`: install from a cold clone, the units, the daily flip via `flip_at`, backups, the sweep, the tool surface, what to check when something is silent.

- [ ] Tests red first; implement; commit `"ops: compaction sweep, tool-surface manifest, systemd templates, operations guide"`.

---

### Task 6 (integrator): results line, README links, full suite, gate, push.

## Self-review

- Spec coverage: daily flip (daemon `flip_at`, documented in T5), size guard (T1), cache audit (T4), session bookends (T2), reincarnate/transplant (T3), hooks (T2), ready watcher (T1), sweep (T5), tool manifest (T5), unit templates (T5). All phase 1 bullets covered.
- File overlap: T1 loops.py/config.py; T2 session.py/hooks; T3 lifecycle.py; T4 cache_audit.py; T5 sweep/tool_surface/boot.py/systemd. Shared docs files (guide, provenance, pyproject, template, wren) will conflict at merge; the integrator resolves by keeping both sides.
