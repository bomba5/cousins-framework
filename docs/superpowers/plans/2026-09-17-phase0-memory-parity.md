# Phase 0: Memory Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring this repository's memory subsystem to full parity with the source framework: hybrid self-healing search, usage-weighted recall, proactive recall in chat, a regenerated durable layer, bounded raw, capsules, corrections, transcript mining, and the small memory CLIs.

**Architecture:** Every behaviour is a `cousin_lib` module with a CLI where the source had one, configured through `config/*.toml` seams (never code constants for hosts, paths, thresholds), tested at the boundary with loopback HTTP fakes and temp homes, and gate-clean. The source framework is available read-only at the path the integrator hands you as the environment variable `SOURCE` (its unit tests at `SOURCE_TESTS`); you read it to learn behaviour and port its tests, you never copy private names, addresses, or paths into this tree.

**Tech Stack:** Python 3.11+ stdlib only (sqlite3 with FTS5, urllib, tomllib, gzip, json). `python3 -m unittest discover -s tests`. The gate: `python3 -m cousin_lib.gate.cli --root . --denylist ~/.config/cousin-framework/denylist.txt --mode gate` (must print nothing).

**Spec:** `docs/port-plan.md` (phase 0 section) in this repository.

## Global Constraints

- Zero third-party runtime dependencies (`pyproject.toml` has no `[project.dependencies]`; keep it so).
- Every new `config/` file gets a row in `docs/configuration.md` (a test asserts the list stays complete) and a `config/<name>.example`.
- Every new console script goes in `pyproject.toml [project.scripts]`, appears in `docs/guide.md` and in `templates/cousin-CLAUDE.template.md` (tests derive the required rows from `pyproject`).
- Every new module gets a row in `docs/provenance.md` ("inspected rewrite of the source framework's X" or "written fresh").
- No RFC1918 literals, no `/home/<user>` paths, no real cousin, family or host names anywhere, including tests and docs. Use `example.invalid`, `203.0.113.0/24`, `cousins/testa`.
- No em dashes in any file you write; ASCII hyphen only.
- Commits are authored `Jhonata Poma-Hansen <jhonata.poma@gmail.com>` via `git -c user.name=... -c user.email=...`, no Co-authored-by, no AI attribution. One commit per task on your branch.
- Work in your own worktree/branch `phase0/t<N>`; do not push; the integrator merges.
- TDD: write the failing test, run it and see it fail for the right reason, implement, run green, commit. Port the source's own unit tests for the module (re-expressed, generic) before writing new ones.

---

## File structure (phase 0)

| file | responsibility |
|---|---|
| `cousin_lib/config.py` | add `harness_config(root)` reader for `config/harness.toml` |
| `cousin_lib/memory_search.py` | hybrid search: sources + collections, chunking, incremental per-file vector index (`memory/embeddings.json`), FTS5 leg, RRF fusion, degrade notice, `--json` |
| `cousin_lib/reinforce.py` | recall log + counts, score bonus with decay |
| `cousin_lib/distill.py` | Layer-3 producer from raw |
| `cousin_lib/raw_fold.py` | monthly gzip fold + per-topic digest |
| `cousin_lib/capsule.py` | reasoning capsules store + `cousin-reason` |
| `cousin_lib/corrections.py` | corrections capture + boot summary |
| `cousin_lib/transcript_mine.py` | mine the harness transcript at flip |
| `cousin_lib/callback.py` | callback moments CLI |
| `cousin_lib/backup.py` | VACUUM INTO snapshot of a cousin home |
| `cousin_lib/sync_state.py` | STATUS.md -> data/state.json |
| `cousin_lib/memory.py` | CLI additions: `distill`, `compact --target raw`, `decide --stdin`, `consolidate` promotes |
| `cousin_lib/boot.py` | run distill before reading the floor; capsules + corrections sections |
| `cousin_lib/flip.py` | call transcript mining after capture, before archive |
| `cousin_lib/server/app.py` | proactive recall line on `/api/send` |
| `config/harness.toml.example`, `config/embedding.toml.example` | seams |

---

### Task 1: Config seams (`config/harness.toml`, extended `config/embedding.toml`)

**Files:**
- Modify: `cousin_lib/config.py` (append at end)
- Create: `config/harness.toml.example`, `config/embedding.toml.example`
- Modify: `docs/configuration.md` (table rows + Formats)
- Test: `tests/test_config_seams.py`

**Interfaces:**
- Produces: `config.harness_config(root: Path) -> dict | None` returning `{"transcripts_dir": str|None, "auto_memory_dir": str|None}` with `{home}` and `{home_encoded}` placeholders expanded by `config.expand_harness_path(template: str, home: Path) -> Path` where `home_encoded` is the home path with `/` replaced by `-` (the harness's project-dir encoding: `/a/b` -> `-a-b`).
- Produces: `memory_search._embedding_config()` (Task 2) reads optional keys `chunk_chars` (default 2000), `chunk_overlap` (default 200), `[recall] min_chars` (24), `min_score` (0.45), `top` (3).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_seams.py
import os, tempfile, unittest
from pathlib import Path
from cousin_lib import config

class HarnessSeam(unittest.TestCase):
    def test_absent_file_means_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(config.harness_config(Path(root)))

    def test_reads_both_keys_and_expands_placeholders(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            (Path(root) / "config" / "harness.toml").write_text(
                'transcripts_dir = "/tmp/h/projects/{home_encoded}"\n'
                'auto_memory_dir = "/tmp/h/projects/{home_encoded}/memory"\n')
            cfg = config.harness_config(Path(root))
            home = Path("/srv/fw/cousins/testa/files")
            self.assertEqual(
                config.expand_harness_path(cfg["transcripts_dir"], home),
                Path("/tmp/h/projects/-srv-fw-cousins-testa-files"))

    def test_unparsable_file_is_loud(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            (Path(root) / "config" / "harness.toml").write_text("not = [toml")
            with self.assertRaises(config.MissingConfigError):
                config.harness_config(Path(root))
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_config_seams -v` and see `AttributeError: harness_config`.
- [ ] **Step 3: Implement** in `cousin_lib/config.py`:

```python
def harness_config(root):
    """config/harness.toml: where the agent harness keeps this install's
    session transcripts and its own auto-memory directory. Absent: None
    (transcript mining and the harness memory collection are off).
    Unparsable: loud, because it was promised."""
    path = Path(root) / "config" / "harness.toml"
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise MissingConfigError("config/harness.toml is unusable: %s" % err)
    return {"transcripts_dir": data.get("transcripts_dir"),
            "auto_memory_dir": data.get("auto_memory_dir")}


def expand_harness_path(template, home):
    home = Path(home)
    encoded = str(home).replace("/", "-")
    return Path(template.replace("{home_encoded}", encoded)
                        .replace("{home}", str(home)))
```

- [ ] **Step 4: Run green.** Then add the two example files (comments only + keys), the `docs/configuration.md` rows ("`config/harness.toml` | transcript mining at flip, the harness auto-memory search collection | both off, said once at flip") and Formats lines, and run the whole suite: `python3 -m unittest discover -s tests` (the configuration coherence test must pass).
- [ ] **Step 5: Gate + commit** `git add -A cousin_lib/config.py config/*.example docs/configuration.md tests/test_config_seams.py` then `git -c user.name="Jhonata Poma-Hansen" -c user.email="jhonata.poma@gmail.com" commit -m "config: harness seam and embedding search options"`.

---

### Task 2: Hybrid search parity (`memory_search.py`)

**Files:**
- Modify: `cousin_lib/memory_search.py` (rewrite internals, keep `search(query, *, top, home) -> (hits, notice)` and `print_results`)
- Modify: `cousin_lib/memory.py` `_cmd_search`: add `--json`, `--collection`
- Test: `tests/test_memory_search.py` (extend), `tests/test_semantic_search.py` (extend), new `tests/test_memory_search_incremental.py`

**Source to read:** `SOURCE/memory_search.py` (`_chunk_text`, `load_memory_files`, `load_notes_files`, `ensure_index`, `fts_search`, `search`) and its tests `test_memory_search_incremental.py`, `test_memory_search_fts.py`, `test_batches_memory_search.py` (port the behaviours, not the private paths).

**Interfaces:**
- Produces: `search(query, *, top=5, home=None, collection=None) -> (hits, notice)`; each hit `{"path": str, "collection": str, "score": float, "snippet": str, "chunk": int}`.
- Produces: `ensure_index(home, config, *, force=False) -> dict` report `{"embedded": n, "reused": n, "dropped": n, "failed": n, "stale_reason": str|None}`; vectors persisted in `home/memory/embeddings.json` as `{"<collection>:<relpath>#<chunk>": {"mtime": float, "vector": [...], "text_hash": str}}`.
- Produces: collections `memory`, `notes`, and `harness` (the harness auto-memory dir from `config/harness.toml`, when present and existing).

Behaviours (each is a test; port the source test names where they exist):
1. `_chunk_text` splits by `chunk_chars` with `chunk_overlap`, never returns empty chunks, one chunk for short text.
2. `ensure_index` embeds only new/changed files (mtime or text hash), reuses unchanged vectors, drops keys whose file is gone, counts failures without poisoning (on embed failure keep the prior vector if any, else skip), and treats an empty index over non-empty sources as stale.
3. Index writes are atomic (`.tmp` then `os.replace`).
4. `search` fuses keyword and semantic legs by RRF; `collection=` filters both legs; `top` bounds.
5. Degrade notices unchanged (absent config: keyword only and `None`; broken: notice; unreachable: notice).
6. `cousin-memory search --json` prints a JSON list of hits; `--collection notes` filters.
7. The harness collection is included only when `config/harness.toml` names an existing directory.

- [ ] **Step 1: Write failing tests** for behaviours 1-3 first, e.g.

```python
def test_incremental_reembeds_only_changed_files(self):
    with tempfile.TemporaryDirectory() as root, _fake_embedder() as url:
        home = _home_with(root, {"memory/a.md": "alpha", "memory/b.md": "beta"})
        cfg = {"url": url, "model": "m", "timeout_s": 5}
        rep = memory_search.ensure_index(home, cfg)
        self.assertEqual((rep["embedded"], rep["reused"]), (2, 0))
        (home / "memory" / "a.md").write_text("alpha changed")
        rep = memory_search.ensure_index(home, cfg)
        self.assertEqual((rep["embedded"], rep["reused"]), (1, 1))
        (home / "memory" / "b.md").unlink()
        rep = memory_search.ensure_index(home, cfg)
        self.assertEqual(rep["dropped"], 1)
```
`_fake_embedder()` is a context manager starting `http.server.HTTPServer(("127.0.0.1", 0), handler)` whose handler answers POST with `{"embedding": [len(prompt) % 7, 1.0, 0.5]}` (deterministic, text-dependent). Put it in `tests/_fakes.py` for reuse by Tasks 3 and 9.

- [ ] **Step 2: Run, see failures** (`ensure_index` missing).
- [ ] **Step 3: Implement** the index: `_sources(home, root)` returns `(collection, path)` incl. harness; `_chunk_text`; `ensure_index`; `_semantic_search` reads `embeddings.json`, embeds the query once, cosine over chunks, best chunk per path; `_keyword_search` stays FTS5 (rebuild when stale as today).
- [ ] **Step 4: Green; then behaviours 4-7 as further red/green cycles.**
- [ ] **Step 5: Gate + commit** `"memory: incremental hybrid search with chunking and collections"`.

---

### Task 3: Usage-weighted recall (`reinforce.py`)

**Files:** Create `cousin_lib/reinforce.py`; modify `cousin_lib/memory_search.py` (`search` applies bonus, records surfaced paths); test `tests/test_reinforce.py`.

**Source:** `SOURCE/_reinforce.py`, tests `test_reinforce.py`.

**Interfaces:** `reinforce.record(home, paths: list[str]) -> None` appends to `home/memory/.recall-log.jsonl` and bumps `home/memory/.recall-counts.json`; `reinforce.bonus(home, path, *, now=None) -> float` in `[0, MAX_BONUS]` with `MAX_BONUS = 0.15`, half-life 14 days on the count's recency; `search` multiplies each fused score by `1 + bonus`.

- [ ] Tests: `test_bonus_zero_for_never_recalled`, `test_bonus_grows_with_recalls_and_is_capped`, `test_bonus_decays_with_age`, `test_search_records_surfaced_paths` (uses Task 2's fake embedder), `test_corrupt_counts_file_is_reset_not_fatal`.
- [ ] Implement, green, gate, commit `"memory: usage-weighted recall bonus"`.

---

### Task 4: Durable layer producer, raw fold, decide --stdin, consolidate promotes

**Files:** Create `cousin_lib/distill.py`, `cousin_lib/raw_fold.py`; modify `cousin_lib/memory.py` (`distill` subcommand, `compact --target raw`, `decide --stdin`, `consolidate` calls distill), `cousin_lib/boot.py` (`assemble` runs `distill.distill(home)` in a try/except before `_memories`); tests `tests/test_distill.py`, `tests/test_raw_fold.py`, `tests/test_memory_cli_distill.py`, `tests/test_boot_distill_hook.py`, `tests/test_memory_decide_stdin.py`.

**Source:** `SOURCE/_distill.py`, `SOURCE/_raw_fold.py`, `SOURCE/memory.py` (`parse_decide_stdin`, `cmd_distill`, `_run_distill`, the raw branch of `cmd_compact`), tests of the same names. This repository's raw entry shape is the one `memory.py:_append_raw` writes; read it first and make `distill` consume that shape (keys `topic`, `content`, `truth_level`, `created_at`, `id`).

**Interfaces:** `distill.distill(home, *, max_lines=40, since_days=3650) -> {"files": {name: n}, "topics": int, "entries": int}`; `distill.classify(entry) -> filename`; `distill.AUTO_MARKER`; `raw_fold.fold_raw(home, *, keep_days=30) -> {"folded_days", "folded_entries", "months"}`; `raw_fold.topic_key(topic) -> 12 hex`; `memory.parse_decide_stdin(text) -> (topic, decision, reasoning)` splitting on lines that are exactly `---`, error on any other chunk count.

- [ ] Port the source tests one by one (red first): classification incl. whole-word keywords, newest-per-topic wins, regenerate-not-append, bounded and ranked, superseded marker, never writes outside distilled, curated text above the marker survives, legacy header migration, fold byte-identical gzip, digest readable by `list_raw`-equivalent, idempotent, deterministic ids, CLI distill/consolidate/compact raw, boot hook.
- [ ] Commit `"memory: durable layer distiller, lossless raw fold, decide --stdin"`.

---

### Task 5: Reasoning capsules (`capsule.py`, `cousin-reason`)

**Files:** Create `cousin_lib/capsule.py`; modify `cousin_lib/boot.py` (a `## Reasoning capsules` block inside `_memories`, newest 5); `pyproject.toml` script `cousin-reason = "cousin_lib.capsule:reason_main"`; test `tests/test_capsule.py`.

**Source:** `SOURCE/_capsule.py`, `bin/cousin-reason`, `tests/unit/test_capsule.py`.

**Interfaces:** `capsule.write_capsule(home, *, conclusion, evidence: list[str], rejected: list[str] | None = None, confidence: str = "medium", truth_level="L3_COUSIN_CONCLUSION", topic="") -> Path` appending one JSON line to `home/memory/capsules.jsonl` and one markdown block to `home/memory/distilled/reasoning-capsules.md` above the distill marker (curated region); `capsule.list_capsules(home, n=10) -> list[dict]`; CLI `cousin-reason capsule --conclusion ... --evidence ... [--rejected ...] [--confidence ...] [--topic ...]` and `cousin-reason list [--n]`.

- [ ] Tests: round-trip write/list, newest first, boot packet carries the newest capsule's conclusion, `distill` run leaves the capsules file's curated block intact (uses Task 4's marker).
- [ ] Commit `"memory: reasoning capsules and cousin-reason"`.

---

### Task 6: Corrections capture (`corrections.py`)

**Files:** Create `cousin_lib/corrections.py`; modify `cousin_lib/server/app.py` (on `/api/send` from an operator, call `corrections.detect_and_record(home, user, text)`), `cousin_lib/boot.py` (`_calibration` appends `corrections.summary_for_boot(home, n=15)`); test `tests/test_corrections.py`.

**Source:** `SOURCE/_corrections.py`, tests `test_corrections.py`.

**Interfaces:** `corrections.detect(text) -> str | None` returning a class in `{"negative_directive", "halt", "rejection", "soft_correction", "redirect", "acceptance", "preference_positive"}` from generic regexes (port the source patterns; they carry no private terms); `corrections.record(home, *, user, text, kind) -> None` appending to `home/data/corrections.jsonl`; `corrections.summary_for_boot(home, n=15) -> str` ("# Recent operator corrections (last N)" or a stated empty marker).

- [ ] Tests: each class detected from a sample line, non-matching text returns None, record/summary round-trip newest first and truncated to 140 chars, boot calibration includes the summary when present, the chat server records on `/api/send` (use the app test harness in `tests/server/test_app.py` as the pattern).
- [ ] Commit `"memory: corrections capture into the calibration layer"`.

---

### Task 7: Transcript mining at flip (`transcript_mine.py`)

**Files:** Create `cousin_lib/transcript_mine.py`; modify `cousin_lib/flip.py` (after the capture stage: `transcript_mine.mine(home, root, session_id)` best-effort, stage record `{"stage": "transcript_mine", "mined": n}` or `{"stage": "transcript_mine", "skipped": "config/harness.toml absent"}`); test `tests/test_transcript_mine.py`.

**Source:** `SOURCE/_transcript_mine.py`, tests `test_transcript_mine.py`.

**Interfaces:** `transcript_mine.mine(home, root, session_id, *, max_entries=24) -> int`: locates `<transcripts_dir>/<session_id>.jsonl` via Task 1's seam, reads assistant text turns, keeps the ones that look like conclusions (sentences with "because", "so ", "therefore", "the cause", "fixed", "decided") and dead ends ("does not", "failed", "wrong"), writes each as a raw candidate `{"topic": "episode:<session_id[:8]>", "content": ..., "truth_level": "L3_COUSIN_CONCLUSION", "source": "flip-transcript"}` through `memory._append_raw`, capped at `max_entries`, and returns the count. Missing file: 0, no error.

- [ ] Tests: a synthetic transcript jsonl (the harness's shape: `{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}` lines) yields the expected candidates; cap respected; absent config -> 0 and flip records the skip; a `flip --dry-run` test asserts the stage appears.
- [ ] Commit `"flip: mine the dying session's transcript into raw candidates"`.

---

### Task 8: Callbacks, backup, state sync (three small CLIs)

**Files:** Create `cousin_lib/callback.py`, `cousin_lib/backup.py`, `cousin_lib/sync_state.py`; `pyproject.toml` scripts `cousin-callback`, `cousin-backup`, `cousin-sync-state`; tests `tests/test_callback.py`, `tests/test_backup.py`, `tests/test_sync_state.py`.

**Source:** `SOURCE/callback.py`, `SOURCE/backup_memory.py`, `SOURCE/sync_state.py` and their tests (`test_small_helpers.py`, `test_sync_state_newest_section.py`).

**Interfaces:**
- `callback`: `tag(home, text, *, cycle=None, category=None)`, `list_all(home)`, `search(home, query)`; file `home/memory/callbacks.md`, one bullet per moment with date, cycle, category. CLI `cousin-callback tag|list|search`.
- `backup`: `snapshot(home, dest_root) -> Path` creating `<dest_root>/<slug>/<YYYY-MM-DD>/` with `VACUUM INTO` copies of every `*.db` under `home/data` and plain copies of `memory/`, `MEMORY.md`, `STATUS.md`, `CLAUDE.md`; CLI `cousin-backup --dest <dir>`; `--dest` is required (no default path in code).
- `sync_state`: `parse_status(text) -> {"open_loops": [...], "parked": [...], "recently_closed": [...]}` where the FIRST section of each kind wins and any other `## ` heading ends a section, bullets of any style count; `write_state(home)` writes `home/data/state.json` with `generated_at`; CLI `cousin-sync-state`.

- [ ] Tests per module (red first): callback round-trip and search; backup produces a readable sqlite copy while the source stays untouched and refuses a missing `--dest`; sync_state newest-section-wins (port the source's regression test), plain bullets counted, empty STATUS gives empty lists.
- [ ] Commit `"memory: callbacks, backup snapshots, state sync"`.

---

### Task 9: Proactive recall in the chat server

**Files:** Modify `cousin_lib/server/app.py` (`/api/send`: after persistence, before delivery, when the sender is an operator and `len(text) >= recall.min_chars`, run `memory_search.search(text, top=recall.top, home=home)`, keep hits with score >= `recall.min_score`, and append one line `[fw-recall] possibly relevant from your memory: <title> (<collection>:<relpath>); ... - cousin-memory search for details; ignore if not.` to the delivered text only, never to the stored message); `cousin_lib/config.py` `CousinConfig.proactive_recall: bool = True` from `cousin.toml [memory] proactive_recall`; test `tests/server/test_recall.py`.

**Source:** `SOURCE/chat_server.py` lines around `_RECALL_MIN_CHARS`, `_read_proactive_recall_cfg`, and the recall-line builder; `tests/unit/test_proactive_recall.py`.

- [ ] Tests (app harness with an injected `deliver` fake): a long operator message with a planted memory gets the recall line delivered and the stored message is unchanged; short messages get none; `proactive_recall = false` gets none; search failure is swallowed (delivery still happens); thresholds come from `config/embedding.toml [recall]` with the defaults when absent.
- [ ] Commit `"chat: proactive recall line on operator messages"`.

---

### Task 10: Docs, provenance, template, guide, results line

**Files:** Modify `docs/provenance.md` (one row per new module), `docs/guide.md` (a worked example per new CLI: `cousin-reason`, `cousin-callback`, `cousin-backup`, `cousin-sync-state`, `cousin-memory distill`, `compact --target raw`, `search --json`), `templates/cousin-CLAUDE.template.md` (tool rows for the new scripts), `docs/memory-tiers.md` (remove the stale "NOT IMPLEMENTED" status: the shared tier ships), `docs/port-plan.md` (Results log line for phase 0 with the commit range and test count).

- [ ] Run the full suite and the gate; both clean.
- [ ] Commit `"docs: phase 0 memory parity landed"`.

---

## Self-review notes

- Spec coverage: every phase 0 bullet in `docs/port-plan.md` maps to a task (hybrid search T2, self-heal T2, reinforce T3, proactive recall T9, distill/fold T4, capsules T5, corrections T6, transcript mining T7, callbacks/backup/sync-state/decide-stdin/consolidate T4+T8, config seams T1, docs T10).
- Dependencies: T1 before T2, T7, T9; T2 before T3 and T9; T4 before T5 (marker). T4, T5, T6, T8 can run in parallel with T2.
- Type consistency: `search()` signature in T2 is what T3 and T9 call; `distill.AUTO_MARKER` in T4 is what T5 uses; `memory._append_raw(home, entry)` (existing) is what T7 uses; `config.harness_config` (T1) is what T2 and T7 use.
