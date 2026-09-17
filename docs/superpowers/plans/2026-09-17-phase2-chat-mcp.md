# Phase 2: Chat Hooks, Watchdog and the MCP Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chat-pattern hooks evaluated by the chat server, a chat-server watchdog with a unit template, and the cousin's CLI surface over MCP stdio with per-cousin provisioning at spawn. Presence and engagement tracking are out by the operator's word (2026-09-17). Inbound image attachments already ship in this repository (`persist_inbound_file`, `docs/chat-server-spec.md`), so they are not a task.

**Architecture:** Same rules as phases 0 and 1 (see the phase 0 plan's Global Constraints). The MCP adapter's core is stdlib (registry to schemas, JSON arguments to an argv LIST, `subprocess.run(list)`, never a joined string, never `shell=`); only `serve()` imports the MCP SDK, lazily, and the SDK is an optional extra, never a core dependency. The source framework is readable at `SOURCE` and `SOURCE_TESTS` as handed to you by the integrator; port behaviour and tests, never private names, addresses or paths.

**Tech Stack:** Python 3.11+ stdlib; optional extra `mcp` for serving. `python3 -m unittest discover -s tests`. Gate: `python3 -m cousin_lib.gate.cli --root . --denylist ~/.config/cousin-framework/denylist.txt --mode gate` (must print nothing).

**Spec:** `docs/port-plan.md` (phase 2 section, as narrowed above).

## Global Constraints

Identical to the phase 0 plan's Global Constraints. Additionally: `pyproject.toml` may gain `[project.optional-dependencies] mcp = ["mcp>=1.0"]`; `[project.dependencies]` stays absent.

---

### Task 1: Chat-pattern hooks in the chat server

**Files:** Create `cousin_lib/chat_hooks.py`; modify `cousin_lib/server/app.py` (`_handle_send`: after persistence and delivery, evaluate hooks best-effort), `docs/chat-server-spec.md` (a "Chat-pattern hooks" section with the JSON schema), `docs/provenance.md`; tests `tests/test_chat_hooks.py`, `tests/server/test_hooks_fire.py`.

**Source:** `SOURCE/_chat_hooks.py` (`load_hooks`, `evaluate`, `fire`, `_fire_shell`), `SOURCE_TESTS/test_chat_hooks.py`.

**Contract:** `<home>/chat-hooks.json` is a list of `{pattern, user, handler, desc}`; `user` is case-insensitive, `*` matches anyone; `handler` is `shell:<path>` (relative to the home or absolute; run detached with env `COUSIN_HOOK_USER`, `COUSIN_HOOK_MESSAGE`, `COUSIN_HOOK_PATTERN`, `COUSIN_SLUG`, `COUSIN_HOME`, stdout and stderr appended to `<home>/data/chat-hooks.log`) or `inject:<text>` (delivered through the server's `deliver` seam as its own line, distinct from the message). Any failure (missing file, bad JSON, bad regex, missing script) is a no-op that never breaks message handling; a bad regex is reported once to stderr. Hooks fire AFTER persistence and delivery of the original message. A `shell:` path that resolves outside the home or the framework root is refused (report, no run).

- [ ] Tests red first (pure `evaluate` cases; a server test with a fake deliver asserting the inject line arrives after the message; a shell handler test writing a marker file; refusal of an outside path; malformed file no-op); implement; commit `"chat: chat-pattern hooks evaluated by the server"`.

---

### Task 2: Chat-server watchdog

**Files:** Create `cousin_lib/chat_watchdog.py` (script `cousin-chat-watchdog = "cousin_lib.chat_watchdog:watchdog_main"`), `systemd/cousin-chat-watchdog.service` + `.timer` (10 min) with the same placeholders as the existing templates; modify `docs/operations.md`, `docs/guide.md`, `docs/provenance.md`, `pyproject.toml`, the template (re-render `examples/wren/CLAUDE.md`); tests `tests/test_chat_watchdog.py` (extend `tests/test_systemd_templates.py` expectations if it enumerates files).

**Source:** `SOURCE/chat_watchdog.py` (`ensure_action` pure decision, `_has_tmux`, `_health`, `_port_in_use`, `_spawn`, `run` with a lock), `SOURCE_TESTS/test_chat_watchdog.py`.

**Contract:** for every cousin in the filesystem registry: no tmux session -> skip; no chat port -> skip; `/health` answers with the slug -> ok; port occupied but health wrong -> alert (log only, never kill); port free -> spawn `cousin-chat-server --home <home>` detached, log appended to `<home>/data/chat-server.log`. One pass per invocation, an `flock` on `<root>/data/chat-watchdog.lock`, `--dry-run` prints decisions. Exit non-zero when any cousin ended in alert or a spawn failed to answer within 5 s.

- [ ] Tests red first (the pure decision table; a run with fakes for tmux, health, port, spawn; dry-run; lock held -> exit 0 with a "another pass is running" line); implement; commit `"chat: chat-server watchdog with a unit template"`.

---

### Task 3: The MCP adapter and per-cousin provisioning

**Files:** Create `cousin_lib/mcp_server.py` (script `cousin-mcp = "cousin_lib.mcp_server:mcp_main"`), `config/mcp-registry.toml.example` (the framework default registry), `docs/mcp-spec.md`; modify `cousin_lib/spawn.py` (`create_cousin` writes `<home>/mcp-registry.toml` from the default with the operator filled in, and `<home>/.mcp.json` pointing the harness at `cousin-mcp --registry <home>/mcp-registry.toml` with env `COUSIN_HOME`, `COUSIN_SLUG`), `cousin_lib/config.py` (`harness_config` gains optional `settings_file`), `docs/configuration.md`, `docs/spawn-and-template-spec.md`, `docs/guide.md`, `docs/provenance.md`, `pyproject.toml` (script + optional extra), README (link the spec), the template (tool row; re-render wren); tests `tests/test_mcp_server.py`, `tests/test_spawn_mcp.py`.

**Source:** `SOURCE/mcp_server.py` (all of it), `SOURCE/mcp_registry.toml` (shape only: the default registry here names commands by console-script NAME, resolved on PATH or beside `sys.executable`, never by absolute path), `SOURCE`'s `bin/cousin-mcp` (the uv launcher is NOT ported; serving requires the `mcp` extra installed in the same interpreter), `SOURCE_TESTS/test_mcp_server.py`, `test_create_cousin_mcp.py`.

**Contract:**
- Registry: `ceiling`, `timeout`, `[tools.<name>]` with `command` (a console-script name or an absolute path; a bare name is resolved to the directory of `sys.executable` first, then PATH), `description`, `[tools.<name>.properties]` (JSON-schema-ish types), `[tools.<name>.commands.<cmd>]` with `argv` (elements are literals or `{prop}` placeholders), `options` (prop -> flag), `stdin` (props joined with `stdin_sep`); `kind = "send"` tools resolve `to` against operators (from the registry's `operators` list) and peers (discovered by running `peers_from`), and `errors_name_peers = false` gives counts, not names; `kind = "job"` tools return the job id and a log tail. Validation errors are `RegistryError` and nothing is served.
- Adapter: `cousin-mcp [--registry PATH] [--selftest]`; `--selftest` loads the registry, builds every schema, and prints the tool list without the SDK; serving imports `mcp` lazily and exits 2 with a remediation line (`pip install "cousin-framework[mcp]"`) when it is missing. Every call runs `subprocess.run(argv_list, input=..., env=..., timeout=...)`; output is truncated to the registry's cap; the client version is recorded to `<home>/data/mcp-client.json`.
- Provisioning: spawn writes the two files if absent; `cousin-mcp approve <slug>` edits the harness settings file named by `config/harness.toml settings_file` (adds the home to the trusted folders and `"cousin"` to `enabledMcpjsonServers`) and refuses with a remediation when the seam is absent; never touches a file it was not pointed at.

- [ ] Tests red first (registry validation, placeholder and option expansion into argv lists, stdin joining, refusal of non-string argv elements, send resolution with hidden peers, selftest output, spawn writes both files with the right command and env, approve edits a temp settings JSON and refuses without the seam); implement; commit `"mcp: the cousin's CLI surface over stdio, provisioned at spawn"`.

---

### Task 4 (integrator): results line, full suite, gate, push, live acceptance.

## Self-review

- Coverage: hooks (T1), watchdog (T2), MCP + provisioning (T3); presence/engagement excluded by the operator; attachments already present.
- Overlap: T1 and T3 do not share code files; T2 and T3 both touch pyproject/template/guide/provenance (union merge).
