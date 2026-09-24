// cousin-policy: the framework's policy veto on the opencode lane.
//
// The runner (cousin_lib/runner/opencode.py) renders the cousin's
// policy.toml into a JSON file and names it in COUSIN_POLICY_FILE; its
// rendered opencode config loads this file by a file:// URL. At init the
// plugin reads that file once (a rewrite of policy.toml applies at the next
// start, as on the SDK lane) and writes an acknowledgement carrying the
// file's nonce, which the runner waits for before its first turn: opencode
// lists a plugin in GET /config whether or not it loaded, so the
// acknowledgement is the proof that the veto is in force.
//
// In `tool.execute.before` a denied call throws; opencode turns the throw
// into a tool error the model sees ("denied by policy: <reason>") and the
// turn goes on. The decision is policy.Policy.decide's: deny_tools, then
// deny_bash_patterns against a string `command` argument (any tool but the
// cousin's own), then ask, which is enforced as deny. Tool names are matched
// in ONE form, the SDK lane's (`Bash`, `mcp__cousin__reply`), mapped from
// opencode's (`bash`, `cousin_reply`) by the table the runner renders, so a
// policy.toml written for either lane means the same on both.
//
// Fail closed: a policy file that cannot be read or is malformed denies
// every call; a pattern that is not a valid JavaScript RegExp (compiled with
// the `u` flag, so an unknown escape is an error, not a literal) denies
// every call that carries a command.
//
// Plain ES module, no import: opencode loads it as is and installs nothing
// for it. Node's `process.getBuiltinModule` (Bun has it too) reaches `fs`.

const ENV = "COUSIN_POLICY_FILE";
const PREFIX = "denied by policy: ";
const ASK_SUFFIX = " (no operator approval surface yet: ask is enforced as deny)";

function fs() {
  const get = typeof process !== "undefined" ? process.getBuiltinModule : undefined;
  return get ? get("node:fs") : null;
}

function strings(value, key) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || !value.every((v) => typeof v === "string")) {
    throw new Error(key + " must be a list of strings");
  }
  return value;
}

// The rendered policy (a parsed object) -> the form `decide` reads. Never
// throws: a malformed file becomes `fatal`, which denies every call.
function compile(policy) {
  try {
    if (!policy || typeof policy !== "object" || policy.version !== 1) {
      throw new Error("not a version 1 cousin policy");
    }
    const patterns = (policy.deny_bash_patterns || []).map((p) => {
      if (!p || typeof p.source !== "string" || typeof p.reason !== "string") {
        throw new Error("deny_bash_patterns entries are {source, reason}");
      }
      try {
        return { source: p.source, reason: p.reason, rx: new RegExp(p.source, "u"), error: null };
      } catch (err) {
        return { source: p.source, reason: p.reason, rx: null, error: String(err.message || err) };
      }
    });
    const names = policy.names && typeof policy.names === "object" ? policy.names : {};
    const prefixes = policy.prefixes && typeof policy.prefixes === "object" ? policy.prefixes : {};
    return {
      fatal: null,
      file: String(policy.file || "policy.toml"),
      deny_tools: strings(policy.deny_tools, "deny_tools"),
      ask: strings(policy.ask, "ask"),
      own_prefix: String(policy.own_tool_prefix || "mcp__cousin__"),
      names,
      prefixes,
      patterns,
      errors: patterns.filter((p) => p.rx === null).map((p) => ({ source: p.source, error: p.error })),
    };
  } catch (err) {
    return { fatal: "malformed policy: " + String(err.message || err), errors: [] };
  }
}

// opencode's tool name in the SDK lane's form: the rendered table first
// (`bash` -> `Bash`), then a prefix (`cousin_` -> `mcp__cousin__`); any other
// name is its own.
function canonical(compiled, tool) {
  const name = String(tool == null ? "" : tool);
  const names = (compiled && compiled.names) || {};
  if (Object.prototype.hasOwnProperty.call(names, name)) return String(names[name]);
  const prefixes = (compiled && compiled.prefixes) || {};
  for (const from of Object.keys(prefixes)) {
    if (from && name.startsWith(from)) return String(prefixes[from]) + name.slice(from.length);
  }
  return name;
}

function named(list, tool) {
  for (const n of list) {
    if (n === tool || (n.endsWith("*") && tool.startsWith(n.slice(0, -1)))) return n;
  }
  return null;
}

// ["allow", ""] | ["deny", reason] | ["ask", reason], for opencode's tool
// name and its arguments.
function decide(compiled, tool, args) {
  if (!compiled || compiled.fatal) {
    return ["deny", "cousin-policy: " + ((compiled && compiled.fatal) || "no policy loaded")];
  }
  const name = canonical(compiled, tool);
  let hit = named(compiled.deny_tools, name);
  if (hit) return ["deny", compiled.file + ": deny_tools lists " + hit];
  const command = args && typeof args === "object" ? args.command : undefined;
  if (typeof command === "string" && !name.startsWith(compiled.own_prefix)) {
    for (const p of compiled.patterns) {
      if (p.rx === null) {
        return ["deny", "cousin-policy: deny_bash_patterns " + JSON.stringify(p.source)
          + " is not a valid JavaScript RegExp (" + p.error + "), so every command is denied"];
      }
      if (p.rx.test(command)) return ["deny", p.reason];
    }
  }
  hit = named(compiled.ask, name);
  if (hit) return ["ask", compiled.file + ": ask lists " + hit];
  return ["allow", ""];
}

// The Error a denied call throws: the text the model sees.
function refusal(decision, reason) {
  return new Error(PREFIX + reason + (decision === "ask" ? ASK_SUFFIX : ""));
}

function load(path) {
  const io = fs();
  if (!path) return { fatal: ENV + " is not set", errors: [] };
  if (!io) return { fatal: "no filesystem access (process.getBuiltinModule)", errors: [] };
  let policy;
  try {
    policy = JSON.parse(io.readFileSync(path, "utf8"));
  } catch (err) {
    return { fatal: "cannot read " + path + ": " + String(err.message || err), errors: [] };
  }
  const compiled = compile(policy);
  compiled.nonce = policy && typeof policy === "object" ? policy.nonce : undefined;
  compiled.ack = policy && typeof policy.ack === "string" ? policy.ack : null;
  return compiled;
}

// What the runner waits for: this start's nonce, what was loaded, and why
// the plugin denies everything when it does (`fatal`). No acknowledgement
// is possible when the file itself cannot be read: the runner then refuses.
function acknowledge(compiled) {
  const io = fs();
  if (!io || !compiled.ack) return;
  const tmp = compiled.ack + ".tmp";
  io.writeFileSync(tmp, JSON.stringify({
    nonce: compiled.nonce,
    pid: process.pid,
    fatal: compiled.fatal,
    deny_tools: compiled.fatal ? 0 : compiled.deny_tools.length,
    deny_bash_patterns: compiled.fatal ? 0 : compiled.patterns.length,
    ask: compiled.fatal ? 0 : compiled.ask.length,
    errors: compiled.errors,
  }) + "\n", { mode: 0o600 });
  io.renameSync(tmp, compiled.ack);
}

async function server(_input, _options) {
  const compiled = load(typeof process !== "undefined" ? process.env[ENV] : undefined);
  acknowledge(compiled);
  return {
    "tool.execute.before": async (input, output) => {
      const [decision, reason] = decide(compiled, input && input.tool, output && output.args);
      if (decision !== "allow") throw refusal(decision, reason);
    },
  };
}

// opencode 1.18.31 loads the default export (the 1.17+ PluginModule shape)
// and, when there is one, no other export. The helpers ride on it for the
// tests; opencode reads only `id` and `server`.
export default { id: "cousin-policy", server, compile, canonical, decide, refusal, load };
