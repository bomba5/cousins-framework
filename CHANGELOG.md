# Changelog

The version lives in `pyproject.toml` and nowhere else; `cousin-version`
prints it and `cousin-version bump [major|minor|patch]` edits it. The
console shows the running version (and, for a git checkout, the
commit) beside its brand.

## 0.2.0 - 2026-09-18

The console is the hive's queen.

- `config/hive.toml` (off by default; `config/hive.toml.example`)
  makes the console answer the queen routes under `/hive/` on its own
  port, token-authenticated and outside the operator login. Absent or
  disabled: every `/hive/` path is a 404 and no hive state is created.
- Remote cousins are console cards: every node that checked in, and
  every built one that has not yet, with host:port, last seen and
  online/offline pushed live; chat is proxied to the node with its own
  token; Revoke and Forget replace the hand edit of `hive.db`.
- The spawn dialog builds a remote cousin: the node archive behind a
  one-time download link (15 minutes or one download) plus the install
  command.
- Queen API: `POST /hive/checkin` (nodes report port, name, role,
  version); `GET /hive/recall` is semantic when `config/embedding.toml`
  answers (vectors cached per row), substring otherwise, and returns
  scored `results` beside the old `memories`; `GET /hive/inbox` takes
  `wait` (long-poll, capped at 30 s); `POST /hive/memory` answers the
  row id and keeps a `kind`. Existing `hive.db` files migrate
  additively.
- `cousin-hive revoke`, `forget`, `nodes`, and `import-legacy` (the
  previous framework's queen: same token strings, memory with its
  original time and kind, idempotent).
- The node runtime checks in on start and every period, and off
  loopback answers chat only to its own token (`NODE_HOST`,
  `cousin-spawn-node --listen-all`, `NODE_ROLE`).

## 0.1.0 - 2026-09-18

The first numbered release of the renamed product. What exists:

- Cousins: one template, `cousin-spawn` with a cleanup contract, a
  per-cousin chat server, inter-cousin chat and replies.
- Memory: private per-cousin memory with keyword and optional semantic
  search, decisions, raw folding and distillation, a reviewed shared
  tier.
- Lifecycle: boot packets, the flip with a bounded handoff, the
  reviewed self-portrait, reincarnate and transplant.
- Recurring work: the loops daemon, heartbeats, one-shot schedules,
  timed and size-guarded flips; jobs and the in-flight tracker.
- Auth mode per cousin: the harness's own login (`claude`, the
  default) or a per-cousin API key (`api_key`) with an isolated
  harness config so the key, not the login, is billed; `cousin-auth`
  and the console switch it and restart on the same session.
- The web console (`cousin-console`): fleet, chat, pane, jobs, loops,
  memory, login; the running version in the top bar.
- Optional: media generation, a Telegram bridge, hive nodes across
  machines, the CLIs as MCP tools.
- Operations: systemd units, the sweep, the tool-surface manifest,
  backups, and the contamination gate on every commit.
