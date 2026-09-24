# The opencode plugin pack

`cousin-policy.js` is the framework's policy veto on the opencode lane
(`runner = "opencode"`). It does one thing opencode's event stream cannot:
refuse a tool call before it runs. Everything else the SDK lane's hooks do
(recording each call with its arguments, subagent jobs, checkpoints) the
runner does from the event stream, in `cousin_lib/runner/opencode.py`.

## How it is loaded

Nobody installs it. The runner renders opencode's config at every start
(`<data_dir>/opencode.runner.json`) with this file in `plugin` as a
`file://` URL to the checkout's copy, and opencode loads it from there.
It is a plain ES module with no `import` and no `require`, so opencode
fetches no package for it. (opencode itself still installs its own
`@opencode-ai/plugin` package into its config directory at boot, whatever
the config says; that is opencode's behaviour, not this pack's, and it
delays plugin loading on a fresh data dir by tens of seconds.)

The module has one export, the default, in opencode's 1.17+ `PluginModule`
shape: `{id: "cousin-policy", server}`. Measured on 1.18.31: a default
export and a lone named export both load from a `file://` entry; with a
default export present, the other exports are ignored. The helpers riding
on the default object (`compile`, `canonical`, `decide`, `refusal`, `load`)
are for the tests; opencode reads only `id` and `server`.

## What it does

1. At init it reads the JSON file named by `COUSIN_POLICY_FILE` (set by the
   runner: `<data_dir>/cousin-policy.json`, rendered from the cousin's
   `policy.toml`), compiles it once, and writes the acknowledgement the file
   asks for (`<data_dir>/cousin-policy.ack.json`, carrying the file's nonce).
   The runner refuses to run turns until it sees that acknowledgement:
   opencode lists a configured plugin in `GET /config` even when the plugin
   failed to load, so the listing alone proves nothing.
2. In `tool.execute.before` it decides as `Policy.decide` does: `deny_tools`,
   then `deny_bash_patterns` against a string `command` argument (any tool
   but the cousin's own `mcp__cousin__*`), then `ask`, which is enforced as
   deny. A denied call throws `Error("denied by policy: <reason>")`; opencode
   turns that into a tool error the model reads, and the turn goes on.

Tool names are matched in ONE form, the SDK lane's (`Bash`,
`mcp__cousin__reply`). opencode's names (`bash`, `cousin_reply`) are mapped
by the table in the rendered file (`opencode.SDK_NAMES` and the `cousin_`
prefix), so a `policy.toml` means the same on both lanes. A tool only
opencode has (`apply_patch`, `question`) keeps its own name.

## Failing closed

- No `COUSIN_POLICY_FILE`, an unreadable file or a malformed one: every call
  is denied, and (when the file named an acknowledgement path) the
  acknowledgement carries `fatal`, which the runner refuses to start on.
- A pattern JavaScript cannot compile (patterns are compiled with the `u`
  flag, so an unknown escape is an error rather than a literal): every call
  that carries a command is denied, and the acknowledgement lists the
  pattern, which the runner reports as an `error` event.

## Tests

`tests/runner/test_opencode_policy.py`: the plugin runs under `node` when
one is on `PATH` (its decisions checked against `Policy.decide` on the same
policy; skipped without node), and the opt-in live proof
(`COUSIN_LIVE_OPENCODE=1`, `OPENCODE_BIN`) loads it into the real
`opencode serve` against a loopback fake provider.
