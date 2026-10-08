# Development

This page is for working on the framework itself: where things live,
how to run the tests and the gate, and what the tests will make you do
when you add a command, a config file, a console route or an MCP tool.

## Set up

```sh
git clone <repo> cousins-framework && cd cousins-framework
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp,sdk]"
python3 -m unittest discover -s tests
```

Python 3.11 or newer. The core has no third-party dependencies; two
extras are optional. `sdk` (the Claude Agent SDK) is what an `sdk`
[cousin](glossary.md#cousin) runs on, and that is the default
[runner](glossary.md#runner) kind, so a working install needs it. `mcp` (the MCP SDK) is needed only by `cousin-mcp`. The suite runs
without either (CI installs plain `-e .`). The console's frontend is JSX compiled in the
browser (React and Babel from a CDN), so there's no build step either:
edit a `.jsx` file and reload.

## Repo layout

| path | what's in it |
|---|---|
| `cousin_lib/` | all the Python. One module per feature, most with a CLI: `memory.py` is `cousin-memory`, `jobs.py` is `cousin-job`, `hive.py` is `cousin-hive`, and so on. `config.py` finds the root and reads `cousin.toml` |
| `cousin_lib/console/` | the web console's server: `app.py` (the HTTP server, login, network guard), `router.py`, one `routes_*.py` per area, `hive.py` (the queen routes), `proxy.py` (chat), `pane.py` (the tmux pane), `sse.py` (the live event stream) |
| `cousin_lib/console_static/` | the console's frontend: `index.html`, one `.jsx` per view, `styles.css` |
| `cousin_lib/server/` | the chat API in-process (no per-cousin chat server runs): `chat_api.py` (history, search, send, reply, archive, reactions), `storage.py` (the SQLite chat store), `inbound.py` (what follows a stored message), `netguard.py` (who may connect) |
| `cousin_lib/delivery.py` | the one way anything reaches a [cousin](glossary.md#cousin): a typed, thread-keyed `Item` handed to `deliver()`, which picks the backend. Producers never build an injector themselves |
| `cousin_lib/gate/` | the contamination gate (`cousin-gate`) |
| `templates/` | `cousin-CLAUDE.template.md` (every cousin's identity file) and `hive-node/` (the remote node runtime, installer and identity) |
| `config/` | only `*.example` files and `harness.lock.toml` (the tested harness versions, read-only data) are tracked. A live install's real configs sit next to them and are gitignored |
| `systemd/` | user unit templates and their README |
| `hooks/` | the shell hooks the agent harness runs at session start, stop and before compaction |
| `examples/wren/` | a sample cousin home, rendered from the template |
| `tests/` | the suite. `tests/console/`, `tests/server/` and `tests/gate/` mirror the code; the rest are `test_<module>.py` |

`cousins/`, `data/` and `shared/` appear under the checkout once you run
an install from it. They're gitignored, as are `config/*` (except the
examples and the harness lock), `.secrets/` and `*-node.tar.gz`.

## Running the tests

```sh
python3 -m unittest discover -s tests            # everything
python3 -m unittest tests.test_hive              # one module
python3 -m unittest tests.console.test_hive_console -k revoke
```

The full run is about 4800 tests. How long it takes depends on the host
(about ten minutes on a 2-core VM). CI runs the same tests, split into
shards (see [CI](#ci)).

To see where the time goes, run the whole suite timed:

```sh
python3 .github/scripts/suite.py time            # then the 20 slowest modules and classes
python3 .github/scripts/suite.py time --write    # and save the module times CI splits by
```

It discovers exactly what `unittest discover -s tests` does, and its
summary line and exit code mean the same.

### What counts as a gate

A run is a gate when it reached its own end, and the two things that say
so are its exit code and its `OK` / `FAILED (failures=..., errors=...)`
line. A log with neither is unfinished, and unfinished is not a gate in
either direction. So:

- Gate on the exit code. Never on a count of `... ok`: `-v` prints one per
  test, so counting them counts tests that ran, not tests that passed, and
  a run stopped at a timeout or a moved branch leaves a prefix that reads
  like a whole suite.
- The deciding run goes alone: no second suite beside it, and nothing of
  yours spawning Python in a loop while it runs. The timing-sensitive
  tests in the runner suite turn on CPU contention, so a verdict taken
  from a busy box belongs to nobody.
- Read the tail of a log for the line, never the middle for a count.
- Piping the run into `grep`, `tail` or `tee` hands the row's exit code to
  that last command, not to the run: `false | tail -1` exits 0. With such a
  pipeline the `OK` / `FAILED` line is the gate; with `set -o pipefail`
  ahead of it, the exit code is the run's again.
- When you report the result, name the commit, the command and the line.
  "All tests pass" without those three cannot be checked by whoever reads
  it, which is how a killed run becomes a shipped defect report.
- A timing-sensitive failure in a run on a loaded host isn't yours yet:
  re-run it alone, and on the base, before you own it.

Every test runs hermetic: `tests/_hermetic.py` strips `FRAMEWORK_ROOT`,
`COUSIN_HOME`, `COUSIN_SLUG`, `COUSIN_TMUX_SOCKET`,
`COUSIN_FILTER_OVERRIDE` and `INVOCATION_ID` from the environment for
the duration of each test and puts them back afterwards. So you can run
the suite from a shell that has a live install activated, and it won't
touch the install's config, databases or cousins. A test that needs one
of those variables sets it itself, pointing at its own temp directory.

Tests, docs and examples share one small invented cast. Cousins and
people: Wren, Kestrel, Testa, Sam, Priya, Toki, Mallory. The operator:
`ana`. Hosts: 192.0.2.x addresses or names under `example.invalid`. Use
those. Don't make up a name on the spot; that's how real ones slip in.

## CI

`.github/workflows/ci.yml` runs on every push and pull request, on
Python 3.11, 3.12 and 3.13. Each version runs the suite in three shards
side by side, each a set of whole test modules:

```sh
pip install -e .
python .github/scripts/suite.py check --of 3       # every module in exactly one shard
python .github/scripts/suite.py run --shard 1 --of 3 --result shard-1.json
```

The split puts the slowest modules first, each on the shard with the
least time so far, by the module times in `.github/test-timings.json`; a
module that file does not name yet counts as a typical one. Refresh the
file with `suite.py time --write` when the split drifts out of balance
(each shard prints its slowest modules at the end of its log). The job
named `test (3.x)` is the one branch protection requires: it reads the
three shards' records and passes only if all three passed and, together,
ran every discovered module exactly once.

The gate's generic checks run inside the suite (see below), so CI gates
every commit. Your denylist of real names is never in CI, since it can't
be in the repo.

The checkout fetches the whole history and its tags (`fetch-depth: 0`),
and the suite runs with `COUSIN_REQUIRE_TAGS=1`: `tests/test_docs_reference.py`
compares each released CHANGELOG section with its tag, and there a clone
without tags fails instead of skipping. Locally, with no tags, that one
check skips and prints why.

`.github/workflows/image.yml` runs on every push and pull request too. It
builds the image from the checkout, runs the runner contract suite
(`tests/runner/contract`) inside it, runs `tests.test_docker_files` on the
host against that image (`COUSIN_DOCKER=1`), and fails if the compressed
image is over its size budget (`docker/image-size.sh`, 240 MB).

`.github/workflows/quickstart.yml` runs the README's Docker quick start as
written. `.github/scripts/quickstart.py` extracts the one code block under
"Quick start: Docker" (the README stays the only copy), drops the `git
clone` and `cd` lines, and runs it under a pty that types the console
password at `adduser`'s prompts. Then it checks that the container is
healthy, that `ana` can log in, that a "hi" reaches `wren`, and that a
memory entry survives `docker compose restart` and `down` + `up -d`. On
every push and pull request (tier 1) the spawn line's opencode
[lane](glossary.md#lane) becomes `--runner fake`, so no model runs and the
check is the fake [turn](glossary.md#turn). On every `v*` tag (tier 2) the
block runs literally on opencode's free model and needs a real reply. Each
edit the extractor makes has to match exactly once, so if you change the
quick start block, the job fails until
`quickstart.py` agrees with it.

## The image's pins

The Dockerfile's inputs are pinned, so a build of one checkout installs the
same packages on any day. pyproject.toml keeps ranges for pip users, except
the `sdk` extra, which is exact (see [The harness lock](#the-harness-lock)).

- **The base image**: one global `ARG PYTHON_IMAGE`, every `FROM` names it.
  Its default is the tag with its multi-arch index digest (not a
  single-platform manifest's, so arm64 still builds). To move it to a newer
  release (a security update), take the `Digest:` line of
  `docker buildx imagetools inspect python:3.13-slim` and put it there.
- **The Python packages**: `docker/requirements.txt`, the sdk extra, what it
  pulls and the build backend, each at one exact version with the sha256 of
  every file PyPI has for it. The builder installs it with
  `--require-hashes` (a missing package or a changed file stops the build),
  then the framework in place with `--no-deps --no-build-isolation`, then
  `pip check`. Never edit it by hand; re-resolve it:

  ```sh
  sh docker/lock.sh
  ```

  It runs pip-tools in the Dockerfile's pinned base image (so the resolve
  sees the image's Python and platform), reads the checkout read-only and
  writes only `docker/requirements.txt`. Run it after changing a range in
  pyproject.toml or the base digest, rebuild both targets
  (`docker build .` and `docker build --target slim .`), and commit the
  lock with the change. `tests.test_docker_files` checks the lock pins the
  sdk extra and the backend, every pin hashed.
- **The opencode binary**: its version and two sha256s in the
  `opencode-fetch` stage (see the comment there).

## The harness lock

The agent harness is a toolchain: the Agent SDK, the Claude Code CLI its
wheel bundles, opencode and the model ids. `config/harness.lock.toml` names
the versions the framework is tested with, and a version changes there, in
a pull request of its own, never by being found out from a broken
[turn](glossary.md#turn).

`tests/test_harness_lock.py` (the unit suite, every pull request, no
network) is red while any other place disagrees with the lock:

| place | what must equal the lock |
|---|---|
| `docker/requirements.txt` | the `claude-agent-sdk==` pin: `[sdk] claude-agent-sdk` |
| `Dockerfile` | the `opencode-fetch` stage's `version=`: `[opencode] version` |
| `pyproject.toml` | the `sdk` extra, exactly `claude-agent-sdk==<[sdk] claude-agent-sdk>` |
| `cousin_lib/config.py` | `DEFAULT_MODELS`, the same ids in the same order: `[models] claude` |
| `README.md` | the opencode quick start's `--model`: `[models] opencode_default` |

`tests/test_docker_files.py` reads its opencode version from the lock too.
At start, `cousin-runner` compares what is installed with the lock: a
`harness` event, a warning on a mismatch, a refusal under `[agent]
strict_harness` ([configuration](configuration.md#agent-strict_harness)).

### Bumping it

1. Change the lock and every place in the table above in one pull request:
   `docker/requirements.txt` by re-resolving it (`sh docker/lock.sh`, after
   the `sdk` extra moved), `[sdk] bundled_cli` from the new wheel
   (`claude_agent_sdk/_cli_version.py`), the opencode version and its two
   sha256s in the Dockerfile. The unit suite says what is still behind.
2. Install the new versions on a host with a login (`pip install -e
   ".[mcp,sdk]"`, the pinned opencode) and run the live matrix there.
3. Paste its "tested with" block into the pull request's description.

### The live matrix

`tests/live/` tests what the harness does, not the arguments the framework
passes it. It needs a login and spends a few small model turns, so it is
opt-in and never in public CI: without `COUSIN_LIVE=1` every item is a
skip that says so (and the unit suite checks no workflow sets the
variable).

```sh
COUSIN_LIVE=1 python -m tests.live
```

runs it on this host's default account and prints the block: the locked and
the installed versions, then one line per item, `pass`, `FAIL`, `ERROR` or
`skipped: <why>`. The exit status is the test run's (0 every item passed, 1
otherwise), so it gates without a pipe. The items: 0 the installed versions
are the lock's; 1 a session started with `tools=[]` and no MCP server lists
exactly the expected tools in its init message (none: a connector that
attaches anyway is red); 2 a turn with thinking on has the usage keys
`usage.py` reads and no thinking count apart from `output_tokens`; 3 a
session's transcript file grows with each turn and holds the prompt; 4 every
model in `[models] claude` answers one turn on the locked CLI; 5 `opencode
--version` is the lock's and one turn on `[models] opencode_default` returns
text (`OPENCODE_BIN`, else `COUSIN_OPENCODE_BIN`, else `opencode` on PATH;
skipped without one). `COUSIN_LIVE_MODEL` picks the model of items 1 to 3.

Prove item 1 can fail before trusting it green: run it with a tool injected,
and it must fail naming that tool.

```sh
COUSIN_LIVE=1 COUSIN_LIVE_INJECT_TOOL=Bash python -m unittest tests.live.test_matrix -k test_1
```

Item 6, the README's bare-host quick start, is manual: on a host with
Claude Code logged in, in a fresh clone and a fresh venv, run the README's
"Quick start: bare host" block as written, then check `pip show
claude-agent-sdk` prints the lock's version, the first cousin answers in the
console, and its runner's [stream](glossary.md#stream) has a `harness` event with
`"ok": true`.
Note the result as item 6 in the block.

## The contamination gate

The gate stops private stuff from getting into the tree: real names,
home addresses on your LAN, paths with your username, tokens. It fails
on any hit.

Without any configuration it catches:

- private IPv4 literals: 10/8, 172.16/12, 192.168/16 and the CGNAT
  100.64/10 block. The four range definitions written as CIDRs
  (`10.0.0.0/8` etc.) are allowed, since the network guard needs them.
  For examples use the documentation ranges, like 192.0.2.10.
- absolute `/home/<user>` paths
- JWT-shaped strings
- binary files, except `.png .jpg .jpeg .gif .ico .woff .woff2`. It
  can't read inside a binary, so it fails it.

On top of that you give it a denylist: your real names, hosts and
vocabulary, one term per line, `#` for comments. Terms match
case-insensitively on word boundaries, so `ana` doesn't hit `banana`.

```sh
cousin-gate --root . --git-visible --denylist ~/.config/cousins-framework/denylist.txt
```

Exit 0 means clean, 1 means hits. Each hit prints as
`file:line:col kind/position context`.

Where the denylist lives matters, and the loader enforces it. It refuses
a file inside any git work tree (committing it would publish exactly
the list of things you're hiding), and one that resolves into
`/nix/store` (a managed config store is one tidy-up away from a repo).
Keep it somewhere plain, like `~/.config/cousins-framework/`.

`--git-visible` scans only what git would publish: tracked files plus
untracked ones `.gitignore` doesn't exclude. Use it on a checkout that
also hosts a live install, so the gitignored `cousins/` and `config/`
full of real paths don't count. Without it the whole tree is scanned
(skipping `.git`, `__pycache__`, `.venv`, `node_modules`).

The suite runs the gate on its own repo in `tests/gate/test_self.py`,
with `--git-visible` and the generic checks only. Two console tests
also scan `console_static/` with your denylist when they find one:
`test_static_chat.py` reads `COUSIN_DENYLIST` (default
`~/.config/cousins-framework/denylist.txt`) and skips if it's missing,
`test_static_files.py` reads `COUSIN_GATE_DENYLIST`. I run the gate with
my denylist by hand before every commit.

### Positions and triage

Every hit carries a position: where in the file it sits.

- Python: `comment`, `docstring`, `code-string` (a string literal in
  running code), or `code`.
- Shell: a full-line comment is `comment`; everything else, including
  the shebang and trailing comments, is `code`.
- Markdown: `prose`.
- Other text files: `unclassified`. Binaries: `binary`.

In gate mode, the default, position doesn't matter: a hit is a hit.
`--mode triage` is for auditing a tree you're about to clean up. It
prints one JSON record per hit instead of failing, and sorts positions
into mechanical (comment, docstring, prose: a rename fixes them) and
structural (everything else: the term is part of how the code behaves).
Anything it can't place counts as structural.

```sh
cousin-gate --root ../some-private-tree --mode triage --denylist ~/.config/cousins-framework/denylist.txt
```

A green gate means nothing it knows about is in the tree. It doesn't
mean a file is good; that's still review.

## Conventions

- **Stdlib first.** No new runtime dependency without a very good
  reason. If a feature needs one, make it an optional extra like `mcp`
  and keep the rest working without it.
- **Off until configured.** Optional features (hive, media, Telegram,
  embeddings) do nothing when their config file is missing. A config
  file that's present but broken is an error you can see, never a
  silent "off".
- **Tests for new behaviour.** Every change that does something new
  comes with a test. A bug fix comes with a test that fails without the
  fix.
- **Generic names in code.** No real people, cousins or hosts in code,
  comments, tests, docs or commit messages. Use the cast above
  everywhere. The gate will catch the rest: `cousin-gate --git-visible`
  for the tree and `cousin-gate --commits origin/main..HEAD` for the
  messages, both with your denylist from outside the tree.
- **CLI exit codes.** 0 for success, 1 for a failure while doing the
  work, 2 for bad usage or configuration. Some commands add their own
  (`cousin-reply` uses 3 for a message the outbound filter blocked).

## Adding things

### A CLI

1. Write a `<name>_main(argv=None)` function in the module, using
   `argparse` with `prog="cousin-<name>"`, returning the exit code.
2. Add it to `[project.scripts]` in `pyproject.toml` and reinstall
   (`pip install -e .`) so the entry point exists.
3. If a cousin calls it, wrap the main in
   `@traced_cli("cousin-<name>")` from `cousin_lib/trace.py`. That logs
   every call to the trace ledger, which the next session's boot packet
   replays.
4. Document it in [commands](commands.md).

Tests that will stop you:

- `tests/test_packaging.py` imports every `[project.scripts]` target,
  and fails if a script is missing from `docs/commands.md`.
- If you run it from a systemd unit, `tests/test_systemd_templates.py`
  checks that every `ExecStart` runs a declared script, that
  `systemd/README.md` names every unit and placeholder, and that the
  units and README are ASCII only.

### A config file

1. Ship `config/<name>.toml.example` with every key commented. Only
   `.example` files are tracked.
2. Read it from `<root>/config/<name>.toml`. Missing means the feature
   is off; unparsable means a clear error.
3. Add it and every key to [configuration](configuration.md).

`tests/test_packaging.py` scans `cousin_lib/` for `"config" / "<file>"`
and `"config", "<file>"` and fails if any file it finds isn't mentioned
in `docs/configuration.md`. It also checks, with real git, that
`config/<name>.toml` is ignored and `config/<name>.toml.example` isn't.

### A console route

Routes live in `cousin_lib/console/routes_*.py`:

```python
from cousin_lib.console import router
from cousin_lib.console.app import HttpError

def register():
    @router.route("GET", "/api/widgets/{slug}")
    def widget(req, slug):
        if not ok(slug):
            raise HttpError(404, "no such widget")
        return 200, {"slug": slug}

register()
```

`{name}` segments become keyword arguments, and `req` carries the
server, query and parsed JSON body. A new module has to be listed in
`ROUTE_MODULES` in `cousin_lib/console/app.py`. Every `/api/` route
needs a login except the few in `AUTH_EXEMPT`. The console is a view:
read from the store that owns the data on each request, and write
through the same library function the CLI uses.

Add the route to [the console API reference](reference/console-api.md),
as a heading. `tests/console/test_static_chat.py` and
`test_static_files.py` check that every `/api/...` path the frontend
calls is one that page defines, so a new call from a `.jsx` file fails
until it's documented. Route tests go in `tests/console/`, using the
harness in `tests/console/_harness.py`.

### An MCP tool

A cousin's MCP tools come from a registry, not from code. The default
is `config/mcp-registry.toml.example`, copied into every new cousin's
home. A tool is a `[tools.<name>]` table naming a console script, its
input properties, and how each subcommand maps to argv. The registry's
`ceiling` (12 by default) caps how many tools can be enabled.

1. Make the thing a CLI first (above).
2. Add a `[tools.<name>]` entry to `config/mcp-registry.toml.example`.
   Never register a CLI that runs a shell on its input, and keep
   operator-only verbs (spawn, [flip](glossary.md#flip), lifecycle, loop control, shared
   memory review) out.
3. Document it in [MCP](mcp.md).
4. When you change a value already shipped (a description, an `options`
   table, an `argv`, a property's enum), run
   `python -m cousin_lib.registry_history --write` from a full clone after
   committing the registry, and commit what it writes. It records every
   value a past release shipped, so the sync can bring existing cousins'
   copies up to date; `tests/test_registry_history.py` fails until you do.

`tests/test_mcp_server.py` loads the shipped registry and builds every
schema, so a broken entry fails. Nothing forces the docs page here; do
it anyway. `cousin-mcp --selftest` checks a registry by hand.

### A law rule or a house rule

Every numbered rule of `templates/law.md` and every `kind: rule` file in
`templates/shared/` has a row in the
[rules inventory](reference/rules-inventory.md), ENFORCED with the tests
that prove the refusal or PROSE with a note. `tests/test_rules_inventory.py`
fails on a rule without a row, a row without a rule, a row whose first
words no longer open its rule, and a named test that does not exist. A
test that makes a rule a refusal carries `# enforces: law <n>` (or
`# enforces: house <file>`) inside it, and the row flips to ENFORCED
naming that test; a marker on a PROSE rule fails the same test.

### Changing the cousin template

`examples/wren/CLAUDE.md` must be exactly the template rendered with
the values in `tests/test_examples.py`, or that test fails. After
editing `templates/cousin-CLAUDE.template.md`, regenerate it:

```sh
python3 -c 'import pathlib
from cousin_lib.template import render_template
from tests.test_examples import WREN_VALUES
t = pathlib.Path("templates/cousin-CLAUDE.template.md").read_text()
pathlib.Path("examples/wren/CLAUDE.md").write_text(render_template(t, WREN_VALUES))'
```

### A doc page

Link it from the README or another page. `tests/test_packaging.py`
fails on any file under `docs/` that nothing links to, and on any
`docs/*.md` the README mentions that doesn't exist.

## Versioning

The version is one number, `version` in `pyproject.toml`.
`cousin_lib.__version__`, `cousin-version` and the console's
`GET /api/version` all read it from there (from the installed package
metadata when not running from a checkout).

```sh
cousin-version               # 1.24.0 (6fd4510)
cousin-version bump          # patch: 1.24.0 -> 1.24.1
cousin-version bump minor    # 1.24.0 -> 1.25.0
```

`bump` edits only that line, in the `pyproject.toml` of the checkout you
run it in (so a bump in a worktree stays in the worktree). The console shows the
version it's running, read once at start, so restart it after a bump
or a pull.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): a type, an
optional scope, and a summary in the imperative.

```
feat(loops): flip every cousin daily, with an install default
fix(mcp): skip a tool that does not validate instead of exiting
docs(changelog): rewrite terse
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `chore`.
A `!` after the type or scope, or a `BREAKING CHANGE:` footer, marks an
incompatible change. They line up with the version bump: `feat` is a minor,
`fix` is a patch, a breaking change is a major.

Keep the subject under about 70 characters. Add a body only when a reader
needs the reason; the code says what changed, the body says why.

## Changelog

Every version gets an entry in [CHANGELOG.md](../CHANGELOG.md) under a
`## <version> - <date>` heading, grouped under `### Added`, `### Changed`,
`### Fixed` or `### Removed`. One line per change: what a reader needs to
decide whether the version affects them. Reasoning belongs in the commit or
the pull request, not here. `tests/test_version.py` fails if the
current version has no `## <version>` heading there, and if the version
isn't plain `major.minor.patch`.

A released section is closed: once `v<version>` is tagged, its section
stays the text that tag shipped, and a change that lands later goes under
the version it lands in. `tests/test_docs_reference.py` compares each
section with its own tag (sections older than the oldest tag with that
tag); a section with no tag yet is unreleased and free to change. When a
released section has to be corrected, the correction is one line in
`tests/data/changelog_corrections.txt`: the version, the commit that
corrected it, and why. The test then accepts that section only as that
commit left it.
