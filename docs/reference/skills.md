# Skills

**Status:** phase 1 is built (3.58.0, completed in 3.59.0): the repositories and approval
records, loading on the `sdk` and `tmux` [lanes](../glossary.md#lane), the guard, the `skill` tool
and `cousin-skill`, the console's inspect, approve, reject and remove, the
trigger test, and the console setting for which repositories load. Phases 2 to 4 (installing from outside, proposals,
staleness checks) are not built yet. Agreed in a team meeting (2026-10-21). Requirement words follow RFC 2119.

A skill is a procedure a [cousin](../glossary.md#cousin) loads when it needs it: a folder with a
`SKILL.md` (a name, a one-line "what it does and when to use it", then the
steps) and any files the steps use. The agent sees every loaded skill's name
and description in each [turn](../glossary.md#turn), and reads the body only when it decides the
skill fits. This is the [Agent Skills](https://agentskills.io) format the
Claude agent already understands; the framework decides which skills a
cousin gets, who approves them, and how they load.

## What is a skill, and what is not

A skill is a **procedure with a trigger** ("when you are about to X"):
steps, maybe scripts, and a closing pass/fail check.

- A rule that MUST hold on every turn is an operator rule or law, never a
  skill: a skill loads only when the agent judges that it fits.
- A fact or a decision stays in memory. Knowledge stays a shared reference.
- When a skill takes over procedural memories or a procedural shared
  reference, those are retired with a pointer to the skill (obsoleted in
  memory, removed from the shared references in `<install>/shared/<name>.md`)
  **when the skill is approved**, never at
  draft time. There MUST NOT be two copies that drift apart.

## What a skill may hold

- It MUST cite an operator rule or operator fact and read it when it runs.
  It MUST NOT restate one, and MUST NOT encode an exception to one.
- It SHOULD derive facts from their source (read `ci.yml`, run
  `tool --version`) instead of copying them into the text.
- It MUST point at a repository's own scripts instead of copying them. A
  skill folder holds only scripts that belong to no repository.
- It MUST NOT hard-code where a tool is installed (a Nix store path, a
  virtualenv, an `/opt` path). It names the tool and how this host provides
  it (the distribution's package, `nix shell nixpkgs#<attr>` on NixOS, a
  container), and its check runs `<tool> --version` first.
- It MUST hold the procedure, never the state. Household lists, budgets,
  device lists and playthroughs stay in memory and are read at run time; a
  skill that needs one and finds none MUST stop (fail closed).
- It MUST NOT carry third-party text (guides, recipes, pages) or saved
  third-party pages as fixtures. It keeps the source URL; dry runs fetch
  live, and offline tests use hand-written samples.
- Repositories a skill reads are read-only to it. Its logs, check output
  and job records stay outside them.
- It ends with a check that passes or fails. The check runs as a job (the
  `job` tool's `run`), so its log and exit code are the evidence. A
  self-reported "passed" is not.

## SKILL.md

```markdown
---
name: board-verification
description: Verify a regenerated board against its approved revision. Use after re-running the fab scripts on a board.
license: MIT
metadata:
  context: personal
  side-effect: read-only
  reads: "wren-lab/widget@<commit>:tools/boardhash.py"
  hosts: ""
  rests-on: "wren-lab/widget:tools/boardhash.py, kicad-cli --version"
---
# Board verification

Steps...
```

`name` and `description` are the Agent Skills keys: `name` equals the
folder name, 1-64 lowercase letters, digits and `-`, not starting or ending
with `-` and with no `--`; `description` is one line of 1-1024 characters. `license` is an SPDX
identifier; it is REQUIRED for a skill from outside. The framework's own keys
sit under `metadata`, as the format asks. The format makes `metadata` a map
from string keys to string *values*, so a list is one comma-separated string.
The draft tool writes every value but the name double-quoted, as a JSON
string, which a YAML reader takes as the same text; a double-quoted value
with an escape JSON lacks (`\x41`) is refused, since it would read
differently here and in the agent:

| key | values | meaning |
|---|---|---|
| `context` | `personal`, `work`, `private` | which repository it may live in; the approval checks it against the content |
| `side-effect` | `read-only`, `writes-local`, `outward` | what it can change. An `outward` step (a push, an upload, a message, an order) MUST stop at the operator's go and never carries the go as a step |
| `reads` | `repo@commit:path` (the full commit) or `path#sha256:<hex>`, comma-separated | what it reads; a change there flags the skill as stale |
| `hosts` | host names, comma-separated | every host it may contact |
| `rests-on` | paths, tool versions, memory topics, comma-separated | what it depends on, checked for drift |
| `should-trigger` | a prompt | one that SHOULD make the cousin use the skill (the trigger test); a draft without it is not approved |
| `must-not-trigger` | a prompt | one that MUST NOT; a draft without it is not approved |
| `scope` | `own`, `shared` | the cousin's folder (the default) or `shared/`; `shared` only for a general skill in `personal` |
| `source` | `<url>@<commit>`, the full commit | a skill from outside: where it came from |

`hosts`, `reads`, `rests-on` and `source` left out, or with nothing after
the colon (YAML's null), are not declared; `""` declares none (`hosts: ""`
contacts no host). The draft tool and
approval refuse a host that is not a host name, a `reads` pin in neither
form or not pinned to a full commit, a `source` that is not a URL pinned to a
full commit, a `license` that is not an
SPDX expression, and a `source` without a `license`. Loading does not check
them, so a format rule never unloads an approved skill.

Public interface, under the same rule (adding is MINOR, renaming or
removing is MAJOR): these metadata keys, the `[skills] repos` key in
`cousin.toml`, the layout under `<install>/skills/<repo>/`, the shape of the
approval records, the `skill` tool's commands, the `cousin-skill` command
and its arguments, and the
console's skills routes.

## Where skills live

Skills live in **git repositories, one folder per cousin**, all private,
none published as a whole:

| repository | holds | loaded by |
|---|---|---|
| `personal` | `shared/<name>/` (every cousin) and `<slug>/<name>/` | every cousin that lists it |
| `work` | `<slug>/<name>/` for work done for an employer | only cousins that list it; it MUST have no remote, ever |
| `private` | `<slug>/<name>/` for a private cousin, and private-life skills | only those cousins; never listed anywhere else |

They sit under `<install>/skills/<repo>/`. A cousin's `cousin.toml` names the
repositories it loads from, set in the console's inspector (cousin settings,
"skill repos"; it takes effect at the cousin's next start):

```toml
[skills]
repos = ["personal"]          # the default; ["personal", "work"] for a cousin that does both
```

A cousin loads `shared/` (from `personal`) and its own `<slug>/` folder from
each repository it lists. It never loads another cousin's folder. Nothing
cousin-specific is shared: `shared/` holds only general procedures,
written fresh for it (never stripped down from a work skill) and passed by
the operator, who asks other cousins to review one when he wants to.

"Per cousin" means *loaded* only by that cousin, not hidden: every cousin
runs as the same OS user and can read every folder with Bash. A private
cousin's folder lives only in `private`, so its name never appears in a
commit of a repository other cousins work in. Real separation needs
per-cousin OS users, which are not built.

For skills, a **private cousin** is one whose `[skills] repos` lists
`private` (the cousin's `memory_scope` says nothing here: it is `private`
for every cousin by default). Its own skills go only to `private`: a draft
of its own (`scope` `own`) for `personal` or `work` is refused when it is
written and when it is approved, even when the cousin lists `personal` to
load `shared/`. A general skill it writes for `shared/` is fine, since
that folder is not named for it.

When a cousin needs another cousin's skill, it asks that cousin to run it: a
peer message naming the skill and the inputs. The answer carries the job id
and exit code of the run. The requester's files are read-only inputs.

## The skill tool and the command

A cousin works with skills through the `skill` tool on its framework tool
server, beside `memory`, `job` and `schedule`:

| command | does |
|---|---|
| `list` | the skills it loads (from which repository) and its drafts |
| `show` | one skill's `SKILL.md` and file list, approved or draft |
| `draft` | writes or edits a draft in `<home>/skill-drafts/<name>/`, with the metadata keys above |
| `diff` | a draft against the approved version |

`list` and `show` cover only what the cousin loads (`shared/` and its own
folder in each repository it lists) and its own drafts. They never list or
show another cousin's skill, or another cousin's `private` folder, whose
name is a private cousin's: Bash can read those (per cousin is not hidden), but the
framework's own tool does not hand them out.

`cousin-skill` is the same for a terminal, with the same commands, and the
fallback where the tool is missing. Neither can approve, reject or remove
anything. Those happen only in the console, so no cousin can approve its own
skill.

Two tools carry the word, and a `policy.toml` rule must name the right one:
`Skill` is the agent's own loader, and denying it stops a cousin *using*
skills; `mcp__cousin__skill` (on opencode `cousin_skill`) is this tool, and
denying it stops *drafting*. On a [lane](../glossary.md#lane) that loads no skills (opencode), the
tool still answers: `list` says "none: this lane loads no skills".

## Drafting and approval

A cousin drafts a skill as plain files in `<home>/skill-drafts/<name>/`,
after the operator said yes to it. Cousins never commit to a skills
repository: every commit there is the operator's approval, made by the
console as him, so the house commit rule holds unchanged.

- **A draft never loads.** Only the approved state does.
- **Where it goes** follows its frontmatter: `metadata.context` names the
  repository, and `metadata.scope` is `own` (the default, the cousin's
  folder) or `shared` (`shared/` in `personal`, for a general skill only).
- **Approval happens in the console only** (System > skills), never through
  the `skill` tool or the CLI, both of which a cousin can run. The approve view leads with the
  side-effect class, the diff against the approved state, the hosts, the
  `reads` pins and the trigger test result. Approval needs a trigger test
  run on the very diff shown ([below](#the-trigger-test)). Approving
  commits the change on
  `main` with the git identity configured for the repository (set it once,
  to the operator's), and appends a record:
  the console session user, the repository, the commit, the skill, the
  sha256 of the diff shown and the trigger test's outcome (`trigger`:
  `pass` or `fail`). Records for `personal` and `work` go to
  `<install>/data/skills/approvals.jsonl`. Records for `private` go to their
  own file, `<install>/data/skills/private/approvals.jsonl` (mode `0600`,
  which keeps out other OS users, not other cousins): a
  record names the folder, and a private cousin's folder is its name.
  `private` never shows in System > skills, only in its own cousin's
  inspector.
- **The record is the evidence, not git.** Every commit on this host is made
  by one OS user under one configured identity, so a git author proves
  nothing. A repository whose `main` has moved past its last approved commit
  loads nothing at all until the operator approves the difference, and
  `cousin-doctor` says so.
- **Removing** an approved skill is a change like any other: proposed,
  shown, approved.
- **The policy hook denies** tool writes under `<install>/skills/`, under
  `<home>/run/skills/` (the exports below), to the approval records and to
  the trigger test results (all under `<install>/data/skills/`). For
  git it allows only `log`, `show`, `diff`, `status`, `cat-file` and
  `ls-files` with `-C` or a working directory under `<install>/skills/`;
  every other git command there is denied (an allowlist, so `reset`,
  `checkout`, `rebase` and the rest are covered). The guard runs in the
  `sdk` lane's tool gate, for every session; the `tmux` and `opencode`
  lanes have no gate, as for the memory perimeter. Bash is checked on its
  command text, best-effort. This slows a confused session; it is not a
  wall against a hostile one until per-cousin OS users exist.

A new skill, an edit and a skill from outside all go through the same path.

## Loading, per lane

Skills load when the cousin's agent starts, and a resumed session picks up
a changed set (probed 2026-10-21 with claude-agent-sdk 0.2.166 and its
bundled Claude Code 2.1.299). A change
therefore takes effect at the cousin's **next restart**, which the console
offers after an approval. Nothing reloads mid-session, so the cached prompt
stays byte-stable.

| [lane](../glossary.md#lane) | how |
|---|---|
| `sdk` | At every start the [runner](../glossary.md#runner) exports the approved commit of each listed repository (`shared/` and the cousin's folder) afresh to `<home>/run/skills/<plugin>/`, read-only, with a plugin manifest, and passes each as a local plugin. The agent reads a skill's body when it uses the skill, not at start, so the export is denied to tools as well. Settings files stay unread (`setting_sources=[]`). The agent sees `shared:<name>`, `personal:<name>`, `work:<name>`, `private:<name>`: the plugin is named for the repository, never the cousin. |
| `tmux` | The same exports, passed as `--plugin-dir`. This lane reads project settings (`--setting-sources project,local`), which load `<home>/.claude/skills/` unapproved (probed 2026-10-21 with the host's Claude Code 2.1.282), so the launcher MUST refuse to start while that directory exists, naming it. That refusal is the only guard on this lane, which has no tool gate. |
| `opencode` | Not supported. opencode has a skill tool of its own; the framework passes it nothing. |

## The trigger test

Before approval, each new or changed skill gets one prompt that SHOULD make
the agent use it and one that MUST NOT. Both run on the cousin's own model,
with the cousin's whole set loaded plus the draft. The result (fired or not,
which skill fired) shows in the approve view. The test costs cents.

Approval needs a result run on the very diff the approve view shows (an
edit after the test makes it stale), but not a passing one: the operator
sees the result and MAY approve a known miss, after a confirm that says
so, and the approval record keeps the outcome. The console runs the test
in a child process and keeps the last result in
`<install>/data/skills/trigger-tests/<slug>/<name>.json`
(`<install>/data/skills/private/trigger-tests/` for a cousin that loads
`private`), where the policy hook denies tool writes (best-effort for
Bash; the `tmux` lane has no gate), so a session does not write the pass
by accident.

## Proposals

Every cousin follows an operator rule: when it notices it is repeating
multi-step work, it considers a skill. Proposals go **only to the operator**.

- The framework detects repetition across generations, with no model call.
  The signals, strongest first: the same operator correction twice, the same
  lesson written to memory twice, then a repeated tool sequence in the
  ledger.
- A proposal rides the existing `propose` turn (a turn nobody in the chat
  started, so the cousin's `reply` goes to its operator). It costs no new
  wake.
- A proposal carries its evidence as counts and entry ids, never entry
  content or topic names, plus the expected payoff. There is a cap per
  cousin per week.
- The operator answers in the chat. A yes means the cousin drafts the skill.

## Life after approval

- The cousin that uses a skill most owns it. Others ask the owner to run it,
  or report a problem to the owner; they never fork it.
- A change to anything in `reads` or `rests-on` flags the skill as stale.
- A scheduled check is read-only and rate-limited, and names itself to any
  host it calls. It never runs an outward step or a long job.
- The console shows each skill's last use and its last check result. A skill
  unused for 30 days is flagged for removal.
- A skill is judged by "its check passed and no correction followed", not by
  how often it fires.

## Acceptance tests

The spec is met when these install, pass the trigger test and pass their own
check:

- **commit gate** (`shared`, `outward`): reads the commit-authorship rule at
  use time and never has the go as a step; checked against a local bare
  repository, so testing pushes nothing.
- **board verification** (`personal`, `read-only`): wraps a repository's
  scripts pinned by commit.
- **Zephyr upstream prep** (`personal`): reads the identity rules at use
  time and never picks up the work address.
- **recipe filing** (`writes-local`), and an **allergen pass** that MUST
  refuse to plan a dish when the household list is missing (fail closed).

## Phases

1. Repositories and the approval record; the loader for `sdk` and `tmux`
   (closing the `.claude/skills` path); the policy deny; the `skill` tool
   and `cousin-skill` (list, show, draft, diff); the console's inspect, approve,
   reject and remove, per cousin (inspector) and for `shared/` (System);
   the trigger test.
2. Installing a skill from outside: a pinned commit, a licence, its text and
   scripts reviewed, terms of use, a dry run on this host.
3. Proposals.
4. Staleness: drift flags, scheduled checks, usage.
