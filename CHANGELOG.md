# Changelog

The version lives in `pyproject.toml` and nowhere else; `cousin-version`
prints it and `cousin-version bump [major|minor|patch]` edits it. The
console shows the running version (and, for a git checkout, the
commit) beside its brand.

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
