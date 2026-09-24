// WP-D, MCP and policy: this package's own file (index.html's package block).
// Its routes are cousin_lib/console/routes_mcp.py (docs/reference/console-api.md,
// "MCP and policy").
//
// Inspector panels (inspector.panels, props { cousin }):
//   McpPanel     the tool registry, the .mcp.json servers and cousin-mcp's
//                diagnostics (selftest, last connection, approve)
//   PolicyPanel  policy.toml: deny_tools, deny_bash_patterns, ask, outbound_filter
// Settings panel (settings.panels): McpInstallPanel, the install's default
// registry, with "copy from example".
//
// Every read answers an etag that the write sends back: a file changed on disk
// meanwhile is refused (409) and the panel says reload. Every write answers
// restart_required: the change applies at the next start, and RestartOffer
// offers the fleet's restart route.

// ---- pure helpers (the tests run this block under node) -------------------
const MCP_HANDOFF_TOOL = "mcp__cousin__handoff";

// Whether a deny_tools / ask entry names the handoff (exactly or as prefix*),
// the same rule as runner/policy.Policy._named.
function mcpNamesTool(entry, tool) {
  entry = String(entry || "");
  return entry === tool || (entry.endsWith("*") && tool.startsWith(entry.slice(0, -1)));
}

// What a policy change removes: each list's entries that are gone, and whether
// the outbound filter goes from on to off. `any` says whether it loosens.
function policyRemovals(before, after) {
  const out = { any: false };
  for (const key of ["deny_tools", "deny_bash_patterns", "ask"]) {
    const next = new Set((after && after[key]) || []);
    out[key] = ((before && before[key]) || []).filter(v => !next.has(v));
    if (out[key].length) out.any = true;
  }
  out.outbound_filter = !!(before && before.outbound_filter !== false && after && after.outbound_filter === false);
  if (out.outbound_filter) out.any = true;
  return out;
}

// Why a pattern would fail on the opencode lane, where the plugin runs it as a
// JavaScript RegExp; null when JavaScript compiles it.
function jsRegexProblem(pattern) {
  try { new RegExp(pattern); return null; } catch (e) { return String(e.message || e); }
}

// A server as GET answers it -> the editable draft (masked values stay null).
function mcpServerDraft(s) {
  const d = { name: s.name, type: s.type, ignored_keys: s.ignored_keys || [], masked: s.masked || [],
              account_vars: s.account_vars || [], unset_vars: s.unset_vars || [] };
  if (s.type === "stdio") {
    d.command = s.command || "";
    d.args = (s.args || []).map(a => (a && typeof a === "object") ? a.value : a);
    d.env = (s.env || []).map(e => ({ name: e.name, value: e.value }));
  } else {
    d.url = s.url_masked ? null : (s.url || "");
    d.headers = (s.headers || []).map(h => ({ name: h.name, value: h.value }));
  }
  return d;
}

// The draft -> the POST body's server.
function mcpServerBody(d) {
  const out = { name: String(d.name || "").trim(), type: d.type };
  if (d.type === "stdio") {
    out.command = d.command;
    out.args = (d.args || []).slice();
    out.env = (d.env || []).filter(e => e.name !== "" || e.value !== "");
  } else {
    out.url = d.url;
    out.headers = (d.headers || []).filter(h => h.name !== "" || h.value !== "");
  }
  return out;
}

// Apply one refusal's suggested reference to a draft: the value it names is
// replaced by the suggestion. Returns a new draft (or the same one when the
// problem names nothing in it).
function mcpApplySuggestion(d, p) {
  if (!p || !p.suggest || p.server !== d.name) return d;
  const next = JSON.parse(JSON.stringify(d));
  if ((p.field === "env" || p.field === "headers") && next[p.field]) {
    next[p.field] = next[p.field].map(e => e.name === p.key ? { name: e.name, value: p.suggest } : e);
  } else if (p.field === "args" && typeof p.key === "number" && next.args) {
    next.args[p.key] = p.suggest;
  } else if (p.field === "url" || p.field === "command") {
    next[p.field] = p.suggest;
  } else {
    return d;
  }
  return next;
}
// ---- end pure helpers -------------------------------------------------------

const mcpSmall = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const mcpMono = { fontFamily: "var(--mono)", fontSize: 11 };
const mcpHint = { fontSize: 10, color: "var(--fg-3)", lineHeight: 1.5 };
const mcpBox = { border: "1px solid var(--line)", borderRadius: 3, background: "var(--bg-0)", padding: 8,
                 display: "flex", flexDirection: "column", gap: 6 };

function McpMsg({ msg }) {
  if (!msg) return null;
  return (
    <div style={{ ...mcpMono, fontSize: 10, color: msg.ok ? "var(--green)" : "var(--red)", whiteSpace: "pre-wrap" }}>
      {msg.text}
    </div>
  );
}

// "applies at the next start" and, on a running cousin, a restart through the
// fleet's restart route (a second click confirms).
function RestartOffer({ cousin, note }) {
  const [confirm, setConfirm] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  React.useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(false), 4000);
    return () => clearTimeout(t);
  }, [confirm]);
  const restart = async () => {
    if (!confirm) { setConfirm(true); return; }
    setConfirm(false); setBusy(true); setMsg(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/restart`);
      if (!r.ok || d.ok === false) throw new Error(d.error || (d.start && d.start.error) || `HTTP ${r.status}`);
      setMsg({ ok: true, text: r.status === 202 ? "restarting: the runner stops, then starts again" : "restarted" });
    } catch (e) {
      setMsg({ ok: false, text: "restart failed: " + String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div data-restart-offer style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
      <Pill tone="amber">saved</Pill>
      <span style={mcpHint}>{note || "applies at the next start"}</span>
      {cousin && cousin.status === "running" && (
        <button className="btn" style={mcpSmall} disabled={busy} onClick={restart}
                title="POST /api/cousins/<slug>/restart: stop, then start on the new settings">
          {busy ? "restarting..." : confirm ? "click again to restart" : "restart now"}
        </button>
      )}
      {cousin && cousin.status !== "running" && <span style={mcpHint}>the cousin is stopped: its next start uses it</span>}
      <McpMsg msg={msg} />
    </div>
  );
}

// A typed confirmation: the action runs only once the word is typed.
function TypedConfirm({ word, label, busy, onConfirm, onCancel }) {
  const [typed, setTyped] = React.useState("");
  return (
    <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
      <span style={mcpHint}>type <code>{word}</code> to confirm</span>
      <input className="txt" value={typed} onChange={e => setTyped(e.target.value)} style={{ width: 90 }} autoFocus />
      <button className="btn danger" style={mcpSmall} disabled={busy || typed !== word} onClick={onConfirm}>{label}</button>
      <button className="btn ghost" style={mcpSmall} onClick={onCancel}>cancel</button>
    </div>
  );
}

// The load/save plumbing every editor here shares: GET the url, POST a body,
// a 409 etag answer turned into "reload".
function useMcpResource(url) {
  const [data, setData] = React.useState(null);
  const [loadErr, setLoadErr] = React.useState(null);
  const load = React.useCallback(async () => {
    try {
      const r = await fetch(url, { cache: "no-store" });
      const d = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
      if (!r.ok) { setLoadErr(d.error || `HTTP ${r.status}`); return null; }
      setData(d); setLoadErr(null);
      return d;
    } catch (e) {
      setLoadErr(String(e.message || e));
      return null;
    }
  }, [url]);
  React.useEffect(() => { setData(null); load(); }, [load]);
  return [data, setData, load, loadErr];
}

// ---- the tool registry --------------------------------------------------------

function RegistryEditor({ url, scope, cousin }) {
  const [data, setData, load, loadErr] = useMcpResource(url);
  const [numbers, setNumbers] = React.useState({});
  const [tools, setTools] = React.useState({});
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const [saved, setSaved] = React.useState(false);
  const [stale, setStale] = React.useState(false);
  const [replacing, setReplacing] = React.useState(false);

  React.useEffect(() => {
    if (!data) return;
    setNumbers({ ceiling: data.ceiling, timeout: data.timeout, max_output: data.max_output });
    const t = {};
    (data.tools || []).forEach(x => { t[x.name] = x.enabled; });
    setTools(t);
  }, [data]);

  const copyPath = scope === "install" ? "/copy-example" : "/copy-default";
  const copyLabel = scope === "install" ? "copy from example" : "copy the install default here";

  const post = async (path, body) => {
    setBusy(true); setMsg(null); setStale(false);
    try {
      const { r, d } = await apiSend("POST", url + path, body);
      if (r.status === 409 && d.etag) setStale(true);
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setData(d); setSaved(true);
      return d;
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
      return null;
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return <div style={{ ...mcpMono, color: "var(--fg-3)" }}>{loadErr ? `registry unavailable: ${loadErr}` : "loading..."}</div>;
  }
  const limits = data.limits || {};
  const changed = {};
  for (const k of ["ceiling", "timeout", "max_output"]) {
    if (numbers[k] !== undefined && Number(numbers[k]) !== data[k]) changed[k] = Number(numbers[k]);
  }
  const toolChanges = {};
  (data.tools || []).forEach(t => { if (tools[t.name] !== undefined && tools[t.name] !== t.enabled) toolChanges[t.name] = tools[t.name]; });
  const dirty = Object.keys(changed).length > 0 || Object.keys(toolChanges).length > 0;
  const enabledCount = Object.values(tools).filter(Boolean).length;
  const ceiling = Number(numbers.ceiling ?? data.ceiling);
  const badNumber = Object.entries(changed).find(([k, v]) => {
    const [lo, hi] = limits[k] || [0, Infinity];
    return !Number.isInteger(v) || v < lo || v > hi;
  });
  const save = () => post("", Object.assign({ etag: data.etag }, changed, Object.keys(toolChanges).length ? { tools: toolChanges } : {}));
  const sourceLabel = { own: "the cousin's own", install: "the install default", example: "the shipped example", shipped: "the shipped example" }[data.source] || data.source;

  return (
    <div data-mcp-registry={scope} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ ...mcpMono, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ color: "var(--fg-0)" }}>{scope === "install" ? "config/" : ""}{data.file}</span>
        {data.exists ? <Pill tone="green">present</Pill> : <Pill tone="gray">absent</Pill>}
        {!data.exists && data.shown && <span style={mcpHint}>showing {sourceLabel}: {data.shown}</span>}
        <button className="btn ghost" style={mcpSmall} disabled={busy} onClick={() => { setMsg(null); setStale(false); load(); }}>reload</button>
      </div>
      {data.error && <div style={{ ...mcpMono, fontSize: 10, color: "var(--red)" }}>{data.error}</div>}
      {(data.skipped || []).map(s => (
        <div key={s.name} style={{ ...mcpMono, fontSize: 10, color: "var(--amber)" }}>cousin-mcp skips {s.name}: {s.reason}</div>
      ))}

      {!data.exists && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn primary" style={mcpSmall} disabled={busy || (scope === "install" && !data.example_exists)}
                  onClick={() => post(copyPath, {})}>{copyLabel}</button>
          <span style={mcpHint}>
            {scope === "install"
              ? "The example is never edited in place: copy it to config/mcp-registry.toml, then edit the copy. Cousins without a registry of their own read it."
              : "This cousin has no registry of its own and reads the one shown. A copy here is its own to edit (the operator is filled in, as spawn does)."}
          </span>
        </div>
      )}

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        {["ceiling", "timeout", "max_output"].map(k => (
          <label key={k} style={{ ...mcpMono, display: "flex", gap: 4, alignItems: "center", color: "var(--fg-2)" }}>
            {k}
            <input className="txt" type="number" disabled={!data.exists || busy}
                   min={(limits[k] || [])[0]} max={(limits[k] || [])[1]}
                   value={numbers[k] ?? ""} style={{ width: k === "max_output" ? 90 : 64 }}
                   onChange={e => setNumbers(Object.assign({}, numbers, { [k]: e.target.value === "" ? "" : Number(e.target.value) }))} />
          </label>
        ))}
      </div>
      <div style={mcpHint}>ceiling: most enabled tools allowed · timeout: seconds per call · max_output: characters returned per call</div>

      <div style={mcpBox}>
        <div style={{ ...mcpMono, fontSize: 10, color: enabledCount > ceiling ? "var(--red)" : "var(--fg-3)" }}>
          {enabledCount} of {ceiling} enabled
        </div>
        {(data.tools || []).length === 0 && <div style={{ ...mcpMono, color: "var(--fg-3)" }}>no tools</div>}
        {(data.tools || []).map(t => (
          <label key={t.name} style={{ ...mcpMono, display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap",
                                       opacity: t.editable ? 1 : 0.6 }}>
            <input type="checkbox" checked={!!tools[t.name]} disabled={!data.exists || busy || !t.editable}
                   onChange={e => setTools(Object.assign({}, tools, { [t.name]: e.target.checked }))} />
            <span style={{ color: "var(--fg-0)" }}>{t.name}</span>
            <span style={{ color: "var(--fg-3)" }}>{t.kind}{t.commands.length ? ` · ${t.commands.join(", ")}` : ""}</span>
            {t.description && <span style={{ ...mcpHint, flexBasis: "100%", paddingLeft: 22 }}>{t.description}</span>}
          </label>
        ))}
      </div>
      <div style={mcpHint}>Adding or changing a tool's commands is a file edit: a registry tool runs a command, so it is not built here.</div>

      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn primary" style={mcpSmall} disabled={!data.exists || busy || !dirty || !!badNumber || enabledCount > ceiling}
                onClick={save}>save</button>
        {badNumber && <span style={{ ...mcpHint, color: "var(--amber)" }}>{badNumber[0]} must be {(limits[badNumber[0]] || []).join(" to ")}</span>}
        {enabledCount > ceiling && <span style={{ ...mcpHint, color: "var(--amber)" }}>more tools enabled than the ceiling</span>}
        {data.exists && !replacing && (
          <button className="btn ghost" style={mcpSmall} disabled={busy} onClick={() => setReplacing(true)}
                  title={scope === "install" ? "overwrite config/mcp-registry.toml with the example" : "overwrite this registry with the install default"}>
            replace with the {scope === "install" ? "example" : "default"}
          </button>
        )}
      </div>
      {replacing && (
        <TypedConfirm word="replace" label="replace" busy={busy}
                      onCancel={() => setReplacing(false)}
                      onConfirm={async () => { const d = await post(copyPath, { replace: true, etag: data.etag }); if (d) setReplacing(false); }} />
      )}
      {stale && <div style={{ ...mcpHint, color: "var(--amber)" }}>the file changed on disk since it was loaded: reload, then make the change again</div>}
      <McpMsg msg={msg} />
      {saved && (scope === "install"
        ? <div style={mcpHint}>saved: each cousin that reads the install default picks it up at its next start</div>
        : <RestartOffer cousin={cousin} note={data.restart_note} />)}
    </div>
  );
}

// ---- .mcp.json servers ---------------------------------------------------------

function McpRows({ rows, onChange, namePlaceholder, valuePlaceholder, disabled, problems, field }) {
  const set = (i, patch) => onChange(rows.map((r, j) => j === i ? Object.assign({}, r, patch) : r));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {rows.map((r, i) => {
        const p = (problems || []).find(x => x.field === field && x.key === r.name);
        return (
          <div key={i} style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
            <input className="txt" value={r.name} placeholder={namePlaceholder} disabled={disabled}
                   style={{ width: 130 }} onChange={e => set(i, { name: e.target.value })} />
            <input className="txt" value={r.value ?? ""} disabled={disabled}
                   placeholder={r.value === null ? "a literal secret is in the file: write ${NAME}" : valuePlaceholder}
                   style={{ flex: "1 1 160px", minWidth: 0 }} onChange={e => set(i, { value: e.target.value })} />
            <button className="btn ghost" style={mcpSmall} disabled={disabled}
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}>×</button>
            {r.value === null && !p && <span style={{ ...mcpHint, color: "var(--amber)", flexBasis: "100%" }}>masked: the file holds a literal secret here; a save needs a ${"{VAR}"} reference instead</span>}
          </div>
        );
      })}
      <button className="btn ghost" style={{ ...mcpSmall, alignSelf: "flex-start" }} disabled={disabled}
              onClick={() => onChange(rows.concat([{ name: "", value: "" }]))}>+ {field === "env" ? "variable" : "header"}</button>
    </div>
  );
}

function McpServerCard({ draft, onChange, onRemove, problems, busy }) {
  const mine = (problems || []).filter(p => p.server === draft.name);
  const set = (patch) => onChange(Object.assign({}, draft, patch));
  const field = (label, children) => (
    <label style={{ ...mcpMono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", color: "var(--fg-2)" }}>
      <span style={{ width: 60 }}>{label}</span>{children}
    </label>
  );
  return (
    <div data-mcp-server={draft.name} style={{ ...mcpBox, borderColor: mine.length ? "var(--red)" : "var(--line)" }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <input className="txt" value={draft.name} placeholder="server name" disabled={busy} style={{ width: 140 }}
               onChange={e => set({ name: e.target.value })} />
        <select className="sel" value={draft.type} disabled={busy}
                onChange={e => {
                  const type = e.target.value;
                  set(type === "stdio" ? { type, command: draft.command || "", args: draft.args || [], env: draft.env || [] }
                                       : { type, url: draft.url || "", headers: draft.headers || [] });
                }}>
          <option value="stdio">stdio</option>
          <option value="http">http</option>
          <option value="sse">sse</option>
        </select>
        <span style={{ flex: 1 }} />
        <button className="btn danger" style={mcpSmall} disabled={busy} onClick={onRemove}>remove</button>
      </div>
      {draft.type === "stdio" ? (
        <>
          {field("command", <input className="txt" value={draft.command || ""} disabled={busy} placeholder="/path/to/server"
                                   style={{ flex: "1 1 200px", minWidth: 0 }} onChange={e => set({ command: e.target.value })} />)}
          {field("args", (
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap", flex: "1 1 200px" }}>
              {(draft.args || []).map((a, i) => (
                <span key={i} style={{ display: "inline-flex", gap: 2 }}>
                  <input className="txt" value={a ?? ""} disabled={busy} style={{ width: 120 }}
                         placeholder={a === null ? "masked secret: write ${NAME}" : "arg"}
                         onChange={e => set({ args: draft.args.map((x, j) => j === i ? e.target.value : x) })} />
                  <button className="btn ghost" style={mcpSmall} disabled={busy}
                          onClick={() => set({ args: draft.args.filter((_, j) => j !== i) })}>×</button>
                </span>
              ))}
              <button className="btn ghost" style={mcpSmall} disabled={busy} onClick={() => set({ args: (draft.args || []).concat([""]) })}>+ arg</button>
            </div>
          ))}
          <div style={{ ...mcpMono, fontSize: 10, color: "var(--fg-3)" }}>env</div>
          <McpRows rows={draft.env || []} field="env" disabled={busy} problems={mine}
                   namePlaceholder="NAME" valuePlaceholder="value or ${VAR}" onChange={env => set({ env })} />
        </>
      ) : (
        <>
          {field("url", <input className="txt" value={draft.url ?? ""} disabled={busy}
                               placeholder={draft.url === null ? "masked secret in the url: write ${NAME}" : "https://host/mcp"}
                               style={{ flex: "1 1 200px", minWidth: 0 }} onChange={e => set({ url: e.target.value })} />)}
          <div style={{ ...mcpMono, fontSize: 10, color: "var(--fg-3)" }}>headers</div>
          <McpRows rows={draft.headers || []} field="headers" disabled={busy} problems={mine}
                   namePlaceholder="Header" valuePlaceholder="value or Bearer ${VAR}" onChange={headers => set({ headers })} />
        </>
      )}
      {draft.ignored_keys && draft.ignored_keys.length > 0 && (
        <div style={mcpHint}>kept as written, not edited here: {draft.ignored_keys.join(", ")} (the runner ignores these keys)</div>
      )}
      {draft.account_vars && draft.account_vars.length > 0 && (
        <div style={{ ...mcpHint, color: "var(--red)" }}>names an account variable ({draft.account_vars.join(", ")}): the runner skips this server</div>
      )}
      {draft.unset_vars && draft.unset_vars.length > 0 && (
        <div style={{ ...mcpHint, color: "var(--amber)" }}>
          not set in the console's environment: {draft.unset_vars.join(", ")}. The runner skips a server whose ${"{VAR}"} has no default and is unset in its own environment.
        </div>
      )}
      {mine.map((p, i) => (
        <div key={i} style={{ ...mcpMono, fontSize: 10, color: "var(--red)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <span>{p.field}{p.key !== null && p.key !== undefined ? ` ${p.key}` : ""}: {p.reason}</span>
          {p.suggest && (
            <button className="btn" style={mcpSmall} disabled={busy} onClick={() => onChange(mcpApplySuggestion(draft, p))}>
              use {p.suggest}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function McpServersEditor({ cousin }) {
  const url = `/api/cousins/${cousin.slug}/mcp/servers`;
  const [data, setData, load, loadErr] = useMcpResource(url);
  const [drafts, setDrafts] = React.useState([]);
  const [drop, setDrop] = React.useState([]);
  const [problems, setProblems] = React.useState([]);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const [saved, setSaved] = React.useState(false);
  const [stale, setStale] = React.useState(false);
  const [replacing, setReplacing] = React.useState(false);

  React.useEffect(() => {
    if (!data) return;
    setDrafts((data.servers || []).map(mcpServerDraft));
    setDrop([]); setProblems([]);
  }, [data]);

  const save = async (replaceBroken) => {
    setBusy(true); setMsg(null); setStale(false); setProblems([]);
    try {
      const body = { etag: data.etag, servers: drafts.map(mcpServerBody), drop };
      if (replaceBroken) body.replace_broken = true;
      const { r, d } = await apiSend("POST", url, body);
      if (r.status === 409 && d.etag) setStale(true);
      if (!r.ok) {
        if (d.problems) setProblems(d.problems);
        throw new Error(d.error || `HTTP ${r.status}`);
      }
      setData(d); setSaved(true); setReplacing(false);
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  if (!data) return <div style={{ ...mcpMono, color: "var(--fg-3)" }}>{loadErr ? `servers unavailable: ${loadErr}` : "loading..."}</div>;
  const ev = data.last_event;
  const tmux = data.lane === "tmux-legacy";

  return (
    <div data-mcp-servers style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ ...mcpMono, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ color: "var(--fg-0)" }}>{data.file}</span>
        {data.exists ? <Pill tone="green">present</Pill> : <Pill tone="gray">absent</Pill>}
        <button className="btn ghost" style={mcpSmall} disabled={busy} onClick={() => { setMsg(null); setStale(false); load(); }}>reload</button>
      </div>
      <div style={{ ...mcpHint, borderLeft: "2px solid var(--amber)", paddingLeft: 8 }}>
        Secrets: the runner hands these servers to the agent CLI on its command line (--mcp-config),
        which any user on this host can read. Write a secret as <code>${"{NAME}"}</code> and set NAME in the
        runner's environment: the CLI fills it in, and only the name is on the command line. A value that
        looks like a secret is refused, with the reference to write instead. Account variables are never
        passed to a server.
      </div>
      {tmux && (
        <div style={mcpHint}>
          This cousin is on the tmux lane: Claude Code reads this file itself at session start, and a new server
          also needs the harness's approval for this home.
        </div>
      )}
      {ev && (
        <div style={{ ...mcpBox, gap: 2 }}>
          <div style={{ ...mcpMono, fontSize: 10, color: "var(--fg-3)" }}>last runner start{ev.ts ? ` · ${fmtAgo(Date.now() / 1000 - ev.ts)}` : ""}</div>
          {((ev.payload || {}).servers || []).map(s => (
            <div key={s.name} style={{ ...mcpMono, color: "var(--green)" }}>loaded {s.name} ({s.type}){s.ignored_keys ? `, ignored ${s.ignored_keys.join(", ")}` : ""}</div>
          ))}
          {((ev.payload || {}).skipped || []).map((s, i) => (
            <div key={i} style={{ ...mcpMono, color: "var(--amber)" }}>skipped {s.name ?? data.file}: {s.reason}</div>
          ))}
        </div>
      )}
      {data.parse_error && <div style={{ ...mcpMono, fontSize: 10, color: "var(--red)" }}>{data.parse_error}</div>}

      {drafts.map((d, i) => (
        <McpServerCard key={i} draft={d} busy={busy} problems={problems}
                       onChange={next => setDrafts(drafts.map((x, j) => j === i ? next : x))}
                       onRemove={() => setDrafts(drafts.filter((_, j) => j !== i))} />
      ))}
      {(data.kept || []).length > 0 && (
        <div style={mcpBox}>
          <div style={{ ...mcpMono, fontSize: 10, color: "var(--fg-3)" }}>kept as they are (not edited here)</div>
          {data.kept.map(k => (
            <div key={k.name} style={{ ...mcpMono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap",
                                       textDecoration: drop.includes(k.name) ? "line-through" : "none" }}>
              <span style={{ color: "var(--fg-0)" }}>{k.name}</span>
              <span style={{ color: "var(--fg-3)" }}>{k.reason}</span>
              {k.name !== data.reserved && (
                <button className="btn ghost" style={mcpSmall} disabled={busy}
                        onClick={() => setDrop(drop.includes(k.name) ? drop.filter(n => n !== k.name) : drop.concat([k.name]))}>
                  {drop.includes(k.name) ? "keep" : "drop"}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {problems.filter(p => !drafts.some(d => d.name === p.server)).map((p, i) => (
        <div key={i} style={{ ...mcpMono, fontSize: 10, color: "var(--red)" }}>{p.server || "server"} {p.field}: {p.reason}</div>
      ))}
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn ghost" style={mcpSmall} disabled={busy}
                onClick={() => setDrafts(drafts.concat([{ name: "", type: "stdio", command: "", args: [], env: [] }]))}>+ server</button>
        {!data.parse_error && <button className="btn primary" style={mcpSmall} disabled={busy} onClick={() => save(false)}>save</button>}
        {data.parse_error && !replacing && (
          <button className="btn danger" style={mcpSmall} disabled={busy} onClick={() => setReplacing(true)}>replace the broken file</button>
        )}
      </div>
      {replacing && <TypedConfirm word="replace" label="replace" busy={busy} onCancel={() => setReplacing(false)} onConfirm={() => save(true)} />}
      {stale && <div style={{ ...mcpHint, color: "var(--amber)" }}>the file changed on disk since it was loaded: reload, then make the change again</div>}
      <McpMsg msg={msg} />
      {saved && <RestartOffer cousin={cousin} note={data.restart_note} />}
    </div>
  );
}

// ---- cousin-mcp: approve, selftest, last connection ------------------------------

function McpDiagnostics({ cousin }) {
  const [status, , loadStatus] = useMcpResource(`/api/cousins/${cousin.slug}/mcp/status`);
  const [selftest, setSelftest] = React.useState(null);
  const [last, setLast] = React.useState(undefined);
  const [busy, setBusy] = React.useState(null);
  const [confirm, setConfirm] = React.useState(false);
  const [msg, setMsg] = React.useState(null);

  React.useEffect(() => { setSelftest(null); setLast(undefined); setMsg(null); }, [cousin.slug]);
  React.useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(false), 4000);
    return () => clearTimeout(t);
  }, [confirm]);

  const run = async (what) => {
    setBusy(what); setMsg(null);
    try {
      if (what === "selftest") {
        const d = await apiGet(`/api/cousins/${cousin.slug}/mcp/selftest`);
        if (!d) throw new Error("selftest unavailable");
        setSelftest(d);
      } else if (what === "last") {
        const d = await apiGet(`/api/cousins/${cousin.slug}/mcp/last-connection`);
        if (!d) throw new Error("last connection unavailable");
        setLast(d.last);
      } else if (what === "approve") {
        if (!confirm) { setConfirm(true); return; }
        setConfirm(false);
        const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/mcp/approve`, {});
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        setMsg({ ok: true, text: `approved in ${d.settings_file}: ${d.note}` });
        loadStatus();
      }
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally {
      setBusy(null);
    }
  };

  const runner = status && status.lane && status.lane !== "tmux-legacy";
  const stateTone = { connected: "green", failed: "red", unrecorded: "amber" };
  return (
    <div data-mcp-diagnostics style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn" style={mcpSmall} disabled={!!busy} onClick={() => run("selftest")}
                title="load the registry, build every schema, resolve every command">{busy === "selftest" ? "testing..." : "selftest"}</button>
        <button className="btn" style={mcpSmall} disabled={!!busy} onClick={() => run("last")}
                title="what the harness recorded the last time it connected this cousin's MCP server">last connection</button>
        <button className="btn" style={mcpSmall} disabled={!!busy || !status || !status.mcp_json || !status.settings_file}
                onClick={() => run("approve")}
                title="trust the home and enable `cousin` in the harness settings file config/harness.toml names">
          {confirm ? "click again to approve" : "approve"}
        </button>
        {status && status.approved === true && <Pill tone="green">approved</Pill>}
        {status && status.approved === false && <Pill tone="amber">not approved</Pill>}
      </div>
      {status && (
        <div style={mcpHint}>
          {runner
            ? "A runner cousin serves `cousin` in-process: approval matters only to the tmux lane's harness."
            : status.settings_file
              ? `approve edits ${status.settings_file} (this home's entry only); the harness reads it at the next session start`
              : (status.reason || "no harness settings file configured")}
        </div>
      )}
      <McpMsg msg={msg} />
      {selftest && (
        <div style={mcpBox} data-mcp-selftest>
          <div style={{ ...mcpMono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <Pill tone={selftest.ok ? "green" : "red"}>{selftest.ok ? "selftest ok" : "selftest failed"}</Pill>
            <span style={{ color: "var(--fg-3)", wordBreak: "break-all" }}>{selftest.registry || "no registry"}</span>
          </div>
          {selftest.error && <div style={{ ...mcpMono, color: "var(--red)" }}>{selftest.error}</div>}
          {selftest.ceiling !== undefined && (
            <div style={{ ...mcpMono, color: "var(--fg-2)" }}>
              {selftest.tools.length} tools · ceiling {selftest.ceiling} · timeout {selftest.timeout}s · output cap {selftest.max_output} chars
            </div>
          )}
          {(selftest.tools || []).map(t => (
            <div key={t.name} style={{ ...mcpMono, color: "var(--fg-1)" }}>
              {t.name} <span style={{ color: "var(--fg-3)" }}>{t.commands.join(", ")}{t.operators ? ` · operators: ${t.operators.join(", ") || "none"}` : ""}</span>
            </div>
          ))}
          {(selftest.commands || []).map(c => (
            <div key={c.command} style={{ ...mcpMono, color: c.found ? "var(--fg-2)" : "var(--red)" }}>{c.command} -&gt; {c.where}</div>
          ))}
          {selftest.sdk && <div style={{ ...mcpMono, color: "var(--fg-3)" }}>mcp sdk: {selftest.sdk.present ? `present (${selftest.sdk.versions.join(", ")})` : "absent"}</div>}
          {(selftest.skipped || []).map(s => <div key={s.name} style={{ ...mcpMono, color: "var(--amber)" }}>skipped {s.name}: {s.reason}</div>)}
          {(selftest.missing_handlers || []).length > 0 && (
            <div style={{ ...mcpMono, color: "var(--red)" }}>no in-process handler: {selftest.missing_handlers.join(", ")}</div>
          )}
        </div>
      )}
      {last !== undefined && (
        <div style={mcpBox} data-mcp-last>
          {last === null ? (
            <div style={{ ...mcpMono, color: "var(--fg-3)" }}>no MCP connection recorded for this home</div>
          ) : (
            <>
              <div style={{ ...mcpMono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                <Pill tone={stateTone[last.state] || "gray"}>{last.state}</Pill>
                <span style={{ color: "var(--fg-3)" }}>{last.when || "?"} · session {last.session_id || "?"}</span>
              </div>
              {last.earlier && <div style={{ ...mcpMono, color: "var(--fg-3)" }}>an earlier attempt in this session: {last.earlier}</div>}
              {last.detail && <div style={{ ...mcpMono, color: "var(--fg-1)", whiteSpace: "pre-wrap" }}>{last.detail}</div>}
              {last.stderr && <pre style={{ ...mcpMono, fontSize: 10, color: "var(--fg-2)", whiteSpace: "pre-wrap", margin: 0 }}>{last.stderr}</pre>}
              <div style={{ ...mcpHint, wordBreak: "break-all" }}>{last.path}</div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function McpPanel({ cousin }) {
  const [tab, setTab] = React.useState("tools");
  const tabs = [["tools", "tool registry"], ["servers", "servers (.mcp.json)"], ["diag", "cousin-mcp"]];
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>mcp</SectionLabel>
      <div data-mcp-panel style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {tabs.map(([id, label]) => (
            <button key={id} className={`btn ${tab === id ? "active" : "ghost"}`} style={mcpSmall} onClick={() => setTab(id)}>{label}</button>
          ))}
        </div>
        {tab === "tools" && <RegistryEditor url={`/api/cousins/${cousin.slug}/mcp/registry`} scope="cousin" cousin={cousin} />}
        {tab === "servers" && <McpServersEditor cousin={cousin} />}
        {tab === "diag" && <McpDiagnostics cousin={cousin} />}
      </div>
    </>
  );
}

// ---- policy.toml ------------------------------------------------------------------

function PolicyChips({ label, items, onChange, disabled, hint, check }) {
  const [draft, setDraft] = React.useState("");
  const [err, setErr] = React.useState(null);
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    const why = check ? check(v) : null;
    if (why) { setErr(why); return; }
    if (!items.includes(v)) onChange(items.concat([v]));
    setDraft(""); setErr(null);
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ ...mcpMono, fontSize: 10, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>{label}</div>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
        {items.length === 0 && <span style={{ ...mcpMono, color: "var(--fg-3)" }}>none</span>}
        {items.map(v => (
          <span key={v} className="pill gray" style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
            {v}
            <button className="btn ghost" style={{ ...mcpSmall, padding: "0 4px" }} disabled={disabled}
                    onClick={() => onChange(items.filter(x => x !== v))} title={`remove ${v}`}>×</button>
          </span>
        ))}
      </div>
      <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
        <input className="txt" value={draft} disabled={disabled} placeholder="tool name, or a prefix*" style={{ width: 200 }}
               onChange={e => { setDraft(e.target.value); setErr(null); }}
               onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
        <button className="btn ghost" style={mcpSmall} disabled={disabled || !draft.trim()} onClick={add}>add</button>
        {hint && <span style={mcpHint}>{hint}</span>}
      </div>
      {err && <div style={{ ...mcpMono, fontSize: 10, color: "var(--red)" }}>{err}</div>}
    </div>
  );
}

function PolicyPatterns({ items, onChange, disabled, lane, problems }) {
  const [draft, setDraft] = React.useState("");
  const add = () => {
    if (!draft) return;
    if (!items.includes(draft)) onChange(items.concat([draft]));
    setDraft("");
  };
  const opencode = lane === "opencode";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ ...mcpMono, fontSize: 10, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>deny_bash_patterns</div>
      {items.length === 0 && <span style={{ ...mcpMono, color: "var(--fg-3)" }}>none</span>}
      {items.map((p, i) => {
        const js = jsRegexProblem(p);
        const py = (problems || []).find(x => x.key === "deny_bash_patterns" && x.entry === p);
        return (
          <div key={i} style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
            <input className="txt" value={p} disabled={disabled} style={{ flex: "1 1 220px", minWidth: 0, fontFamily: "var(--mono)" }}
                   onChange={e => onChange(items.map((x, j) => j === i ? e.target.value : x))} />
            <button className="btn ghost" style={mcpSmall} disabled={disabled} onClick={() => onChange(items.filter((_, j) => j !== i))}>×</button>
            {py && <span style={{ ...mcpHint, color: "var(--red)", flexBasis: "100%" }}>does not compile: {py.reason}</span>}
            {js && (
              <span style={{ ...mcpHint, color: opencode ? "var(--red)" : "var(--amber)", flexBasis: "100%" }}>
                not a JavaScript RegExp ({js}){opencode ? ": on opencode every command is denied until it is rewritten" : ": it would fail on the opencode lane"}
              </span>
            )}
          </div>
        );
      })}
      <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
        <input className="txt" value={draft} disabled={disabled} placeholder="\bgit\s+push\s+--force" style={{ flex: "1 1 220px", minWidth: 0, fontFamily: "var(--mono)" }}
               onChange={e => setDraft(e.target.value)}
               onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
        <button className="btn ghost" style={mcpSmall} disabled={disabled || !draft} onClick={add}>add</button>
      </div>
      <div style={mcpHint}>
        A regex (Python syntax, checked on save) searched in the `command` string of any tool call except the cousin's
        own mcp__cousin__* tools. {opencode ? "On opencode the plugin runs it as a JavaScript RegExp, so it must compile in both." : ""}
      </div>
    </div>
  );
}

function PolicyPanel({ cousin }) {
  const url = `/api/cousins/${cousin.slug}/policy`;
  const [data, setData, load, loadErr] = useMcpResource(url);
  const [draft, setDraft] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const [problems, setProblems] = React.useState([]);
  const [saved, setSaved] = React.useState(false);
  const [stale, setStale] = React.useState(false);
  const [loosen, setLoosen] = React.useState(null);     // the server's `removed`, awaiting the second word
  const [replacing, setReplacing] = React.useState(false);

  React.useEffect(() => {
    if (!data) return;
    setDraft({ deny_tools: data.deny_tools || [], deny_bash_patterns: data.deny_bash_patterns || [],
               ask: data.ask || [], outbound_filter: data.outbound_filter !== false });
    setLoosen(null); setProblems([]);
  }, [data]);

  if (!data || !draft) {
    return (
      <>
        <SectionLabel style={{ marginTop: 20 }}>policy</SectionLabel>
        <div style={{ ...mcpMono, color: "var(--fg-3)" }}>{loadErr ? `policy unavailable: ${loadErr}` : "loading..."}</div>
      </>
    );
  }

  const protectedTool = data.protected || MCP_HANDOFF_TOOL;
  const noHandoff = (v) => mcpNamesTool(v, protectedTool)
    ? `never deny ${protectedTool}: every generation ends through it` : null;
  const set = (patch) => { setDraft(Object.assign({}, draft, patch)); setLoosen(null); };
  const before = { deny_tools: data.deny_tools, deny_bash_patterns: data.deny_bash_patterns, ask: data.ask,
                   outbound_filter: data.outbound_filter };
  const removal = policyRemovals(data.error ? {} : before, draft);
  const dirty = JSON.stringify(before) !== JSON.stringify(draft) || !data.exists;

  const save = async (opts) => {
    setBusy(true); setMsg(null); setStale(false); setProblems([]);
    try {
      const body = Object.assign({ etag: data.etag }, draft, opts || {});
      const { r, d } = await apiSend("POST", url, body);
      if (r.status === 409 && d.needs_confirm) { setLoosen(d.removed); return; }
      if (r.status === 409 && d.etag) setStale(true);
      if (!r.ok) {
        if (d.problems) setProblems(d.problems);
        throw new Error(d.error || `HTTP ${r.status}`);
      }
      setData(d); setSaved(true); setReplacing(false);
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  const ev = data.last_event;
  const tmux = data.lane === "tmux-legacy";
  const shownRemoval = loosen || (removal.any ? removal : null);
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>policy</SectionLabel>
      <div data-policy-panel style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ ...mcpMono, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ color: "var(--fg-0)" }}>{data.file}</span>
          {data.exists ? <Pill tone="green">present</Pill> : <Pill tone="gray">absent: every tool allowed</Pill>}
          <button className="btn ghost" style={mcpSmall} disabled={busy} onClick={() => { setMsg(null); setStale(false); load(); }}>reload</button>
          {!data.exists && data.template && (
            <button className="btn ghost" style={mcpSmall} disabled={busy}
                    onClick={() => set({ deny_tools: (data.template.deny_tools || []).filter(v => !noHandoff(v)),
                                         deny_bash_patterns: data.template.deny_bash_patterns || [],
                                         ask: data.template.ask || [],
                                         outbound_filter: data.template.outbound_filter !== false })}>
              fill from the template
            </button>
          )}
        </div>
        <div style={mcpHint}>
          A guardrail, not a sandbox: a pattern stops the command line it names, not every way to the same effect,
          and the model can rewrite this file. It is read once, at the runner's start.
          {tmux ? " This cousin is on the tmux lane: the runner enforces policy.toml, so it has no effect here until the cousin moves to a runner." : ""}
        </div>
        {data.error && <div style={{ ...mcpMono, fontSize: 10, color: "var(--red)" }}>{data.error} (the runner refuses to start with it)</div>}
        {ev && ev.payload && ev.payload.describe && (
          <div style={{ ...mcpMono, fontSize: 10, color: "var(--fg-3)" }}>in force since the last start: {ev.payload.describe}</div>
        )}

        <PolicyChips label="deny_tools" items={draft.deny_tools} disabled={busy} check={noHandoff}
                     hint="exact names or prefix*: mcp__ha__* denies a whole server"
                     onChange={v => set({ deny_tools: v })} />
        <PolicyPatterns items={draft.deny_bash_patterns} disabled={busy} lane={data.lane} problems={problems}
                        onChange={v => set({ deny_bash_patterns: v })} />
        <PolicyChips label="ask" items={draft.ask} disabled={busy} check={noHandoff}
                     hint="no approval surface yet: enforced as a deny"
                     onChange={v => set({ ask: v })} />
        <label style={{ ...mcpMono, display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={draft.outbound_filter} disabled={busy}
                 onChange={e => set({ outbound_filter: e.target.checked })} />
          outbound_filter
          <span style={mcpHint}>reply and send cross config/outbound-filter.json</span>
        </label>
        <div style={mcpHint}>{protectedTool} can never be denied or asked for: every generation ends through it.</div>

        {problems.filter(p => p.key !== "deny_bash_patterns").map((p, i) => (
          <div key={i} style={{ ...mcpMono, fontSize: 10, color: "var(--red)" }}>{p.key} {p.entry}: {p.reason}</div>
        ))}
        {shownRemoval && (
          <div data-policy-loosen style={{ ...mcpHint, color: "var(--amber)", borderLeft: "2px solid var(--amber)", paddingLeft: 8 }}>
            This change loosens the policy:
            {["deny_tools", "deny_bash_patterns", "ask"].filter(k => (shownRemoval[k] || []).length).map(k => (
              <div key={k}>removes from {k}: {shownRemoval[k].join(", ")}</div>
            ))}
            {shownRemoval.outbound_filter && <div>turns the outbound filter off</div>}
          </div>
        )}
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          {!data.error && (
            loosen
              ? <button className="btn danger" style={mcpSmall} disabled={busy} onClick={() => save({ confirm_loosening: true })}>save and loosen</button>
              : <button className="btn primary" style={mcpSmall} disabled={busy || !dirty} onClick={() => save()}>save</button>
          )}
          {data.error && !replacing && (
            <button className="btn danger" style={mcpSmall} disabled={busy} onClick={() => setReplacing(true)}>replace the broken file</button>
          )}
        </div>
        {replacing && (
          <TypedConfirm word="replace" label="replace" busy={busy} onCancel={() => setReplacing(false)}
                        onConfirm={() => save({ replace_broken: true })} />
        )}
        {stale && <div style={{ ...mcpHint, color: "var(--amber)" }}>the file changed on disk since it was loaded: reload, then make the change again</div>}
        <McpMsg msg={msg} />
        {saved && <RestartOffer cousin={cousin} note={data.restart_note} />}
      </div>
    </>
  );
}

// ---- the install default, in Settings ------------------------------------------------

function McpInstallPanel() {
  return (
    <div className="panel" style={{ marginTop: 14 }} data-mcp-install>
      <div className="panel-hdr">
        <span className="title">mcp tool registry</span>
        <span style={{ color: "var(--fg-3)", fontSize: 11, marginLeft: 8 }}>
          the install default: what a cousin without its own mcp-registry.toml reads, and what spawn copies into a new home
        </span>
      </div>
      <div className="panel-body" style={{ padding: 14 }}>
        <RegistryEditor url="/api/mcp/registry" scope="install" />
      </div>
    </div>
  );
}

registerSlot("inspector.panels", { id: "mcp", order: 30, render: ({ cousin }) => <McpPanel cousin={cousin} /> });
registerSlot("inspector.panels", { id: "policy", order: 31, render: ({ cousin }) => <PolicyPanel cousin={cousin} /> });
registerSlot("settings.panels", { id: "mcp", order: 30, render: () => <McpInstallPanel /> });

Object.assign(window, {
  McpPanel, PolicyPanel, McpInstallPanel, RegistryEditor, McpServersEditor, McpDiagnostics, RestartOffer,
  policyRemovals, jsRegexProblem, mcpNamesTool, mcpServerDraft, mcpServerBody, mcpApplySuggestion,
});
