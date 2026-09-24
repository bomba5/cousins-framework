// WP-A, agent and cousin settings: this package's own file (index.html's package block).
// Its routes are cousin_lib/console/routes_agent.py (docs/reference/console-api.md,
// "Agent and cousin settings").
//
// Inspector (inspector.lane, props { cousin }):
//   AgentSettingsPanel   the lane's [agent] keys, as GET /api/cousins/<slug>/agent
//                        describes them (agent_settings.describe): kind read-only
//                        with "switch kind" (the window event fw-open-kind-switch,
//                        WP-B's dialog), model, effort, account, auto_start and the
//                        hold, rollover, side sessions, the opencode keys, env_allow.
//   CousinSettingsPanel  name, peer_visible, the [memory] recall and review keys,
//                        [lifecycle] flip_at, [agent] commit_attribution; [chat]
//                        and [session] read-only.
// Every value, choice and default comes from the server; nothing here lists the
// kinds, the accounts or the models. Every write answers restart_required, and
// AgentRestartOffer offers the fleet's restart route. An sdk model change is a
// long operation (one validating turn), shown with <LongOpStatus>.
// The chat header's effort select uses agentLaneReads to show itself only on a
// lane that reads effort.

// ---- pure helpers (the tests run this block under node) -------------------
const AGENT_TMUX_LEGACY = "tmux-legacy";

// Whether a cousin on `lane` reads [agent] `key`: the tmux-legacy lane (or none
// known) reads model and effort from [runtime], so both count as read there;
// a runner kind reads what lane_keys (GET /api/spawn/options) lists for it.
function agentLaneReads(lane, key, laneKeys) {
  if (!lane || lane === AGENT_TMUX_LEGACY) return key === "model" || key === "effort";
  return ((laneKeys || {})[lane] || []).includes(key);
}

function agentSame(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// The changes a draft makes against described settings: {key: value}, null to
// unset. [agent.sessions] sends only the kinds whose mode changed.
function agentChanges(settings, draft) {
  const out = {};
  for (const key of Object.keys(draft || {})) {
    const row = (settings || {})[key];
    if (!row || row.readonly) continue;
    const want = draft[key];
    if (key === "sessions") {
      const diff = {};
      for (const kind of Object.keys(want || {})) {
        if ((row.value || {})[kind] !== want[kind]) diff[kind] = want[kind];
      }
      if (Object.keys(diff).length) out.sessions = diff;
      continue;
    }
    if (want === null) { if (row.set) out[key] = null; continue; }
    if (!agentSame(want, row.value)) out[key] = want;
  }
  return out;
}

// A variable name list typed as text: split on commas and white space.
function agentEnvList(text) {
  return String(text || "").split(/[\s,]+/).filter(Boolean);
}

// Why a name cannot go in env_allow (null when it can): not a variable name,
// or one the pane's hard deny takes (the server also refuses credential names).
function agentEnvProblem(name, denyPrefixes) {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return name + " is not a variable name";
  const hit = (denyPrefixes || []).find(p => name.startsWith(p));
  if (hit) return name + ": " + hit + "* never reaches the pane (the hard deny)";
  return null;
}

// A client-side hint for a model on a lane (the server stays the authority).
function agentModelProblem(lane, value) {
  if (!value) return null;
  if (/\s/.test(value)) return "one word, no spaces";
  if (lane === "opencode" && !/^[^/]+\/.+/.test(value)) return "\"<provider>/<model>\"";
  return null;
}

// The changes a draft makes against GET .../settings fields ("table.key").
function cousinSettingChanges(fields, draft) {
  const out = {};
  for (const name of Object.keys(draft || {})) {
    const row = (fields || {})[name];
    if (!row) continue;
    const want = draft[name];
    if (want === null) { if (row.set) out[name] = null; continue; }
    if (!agentSame(want, row.value)) out[name] = want;
  }
  return out;
}

function flipAtProblem(value) {
  if (value === "" || value === "never") return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(value);
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return "\"HH:MM\" or \"never\"";
  return null;
}
// ---- end pure helpers -------------------------------------------------------

const agentSmall = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const agentMono = { fontFamily: "var(--mono)", fontSize: 11 };
const agentHint = { fontSize: 10, color: "var(--fg-3)", lineHeight: 1.5 };
const agentErr = { fontFamily: "var(--mono)", fontSize: 10, color: "var(--red)", whiteSpace: "pre-wrap" };

// Ask WP-B's kind-switch dialog to open for this cousin.
function openKindSwitch(slug) {
  window.dispatchEvent(new CustomEvent("fw-open-kind-switch", { detail: { slug } }));
}

// "applies at the next start" and, on a running cousin, the fleet's restart
// route (a second click confirms).
function AgentRestartOffer({ cousin, note }) {
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
    <div data-agent-restart style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
      <Pill tone="amber">saved</Pill>
      <span style={agentHint}>{note || "restart to apply: the running cousin keeps what it started with"}</span>
      {cousin.status === "running" && (
        <button className="btn" style={agentSmall} disabled={busy} onClick={restart}
                title="POST /api/cousins/<slug>/restart: stop, then start on the new settings">
          {busy ? "restarting..." : confirm ? "click again to restart" : "restart now"}
        </button>
      )}
      {cousin.status !== "running" && <span style={agentHint}>the cousin is stopped: its next start uses it</span>}
      {msg && <span style={{ ...agentErr, color: msg.ok ? "var(--green)" : "var(--red)" }}>{msg.text}</span>}
    </div>
  );
}

// A list of variable names: chips, an add box, and the client-side check.
function AgentEnvList({ value, onChange, disabled, denyPrefixes }) {
  const [draft, setDraft] = React.useState("");
  const [err, setErr] = React.useState(null);
  const items = value || [];
  const add = () => {
    const names = agentEnvList(draft);
    for (const n of names) {
      const why = agentEnvProblem(n, denyPrefixes);
      if (why) { setErr(why); return; }
    }
    setErr(null); setDraft("");
    onChange(items.concat(names.filter(n => !items.includes(n))));
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
        {items.map(n => (
          <span key={n} className="pill gray" style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
            {n}
            <button className="btn ghost" style={{ ...agentSmall, padding: "0 4px" }} disabled={disabled}
                    onClick={() => onChange(items.filter(x => x !== n))}>×</button>
          </span>
        ))}
        {!items.length && <span style={agentHint}>none</span>}
      </div>
      <div style={{ display: "flex", gap: 4 }}>
        <input className="txt" value={draft} disabled={disabled} placeholder="NAME" style={{ width: 160 }}
               onChange={e => setDraft(e.target.value)}
               onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
        <button className="btn ghost" style={agentSmall} disabled={disabled || !draft.trim()} onClick={add}>add</button>
      </div>
      {err && <span style={agentErr}>{err}</span>}
    </div>
  );
}

// One [agent] key's editor, by the type describe() gives it.
function AgentField({ name, row, lane, value, onChange, disabled }) {
  const listId = `agent-${name}-suggestions`;
  if (row.readonly) {
    return <span style={agentMono}>{row.value == null || row.value === "" ? "-" : String(row.value)}</span>;
  }
  switch (row.type) {
    case "bool":
      return (
        <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={!!value} disabled={disabled} onChange={e => onChange(e.target.checked)} />
          <span style={agentMono}>{value ? "true" : "false"}</span>
        </label>
      );
    case "percent":
      return (
        <input className="txt" type="number" min={row.min} max={row.max} step="1" disabled={disabled}
               value={value ?? ""} style={{ width: 90 }}
               onChange={e => onChange(e.target.value === "" ? null : Number(e.target.value))} />
      );
    case "effort":
    case "account": {
      const choices = row.choices || [];
      return (
        <select className="sel" value={value ?? ""} disabled={disabled} onChange={e => onChange(e.target.value)}>
          {value != null && !choices.includes(value) && <option value={value}>{value} (not on this lane)</option>}
          {value == null && <option value="">{row.type === "effort" ? "the runner's default" : "-"}</option>}
          {choices.map(ch => <option key={ch} value={ch}>{ch}</option>)}
        </select>
      );
    }
    case "model": {
      const problem = agentModelProblem(lane, value || "");
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <input className="txt" value={value ?? ""} disabled={disabled} list={listId}
                 placeholder={lane === "opencode" ? "<provider>/<model>" : "the runner's default"}
                 style={{ width: "100%", fontFamily: "var(--mono)" }}
                 onChange={e => onChange(e.target.value === "" ? null : e.target.value)} />
          {(row.suggestions || []).length > 0 && (
            <datalist id={listId}>{row.suggestions.map(m => <option key={m} value={m} />)}</datalist>
          )}
          {problem && <span style={agentHint}>{problem}</span>}
        </div>
      );
    }
    case "sessions": {
      const modes = value || {};
      return (
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 8px", alignItems: "center" }}>
          {(row.kinds || []).map(kind => {
            const fixed = (row.always_primary || []).includes(kind);
            return (
              <React.Fragment key={kind}>
                <span style={agentMono}>{kind}</span>
                <select className="sel" value={modes[kind] || "primary"} disabled={disabled || fixed}
                        title={fixed ? `${kind} threads belong to the primary session, which holds the generation` : undefined}
                        onChange={e => onChange(Object.assign({}, modes, { [kind]: e.target.value }))}>
                  {(row.choices || []).map(m => <option key={m} value={m}>{m}{fixed ? " (always)" : ""}</option>)}
                </select>
              </React.Fragment>
            );
          })}
        </div>
      );
    }
    case "env_list":
      return <AgentEnvList value={value} onChange={onChange} disabled={disabled} denyPrefixes={row.deny_prefixes} />;
    default:
      return (
        <input className="txt" value={value ?? ""} disabled={disabled} style={{ width: "100%" }}
               onChange={e => onChange(e.target.value === "" ? null : e.target.value)} />
      );
  }
}

function AgentSettingsPanel({ cousin }) {
  const c = cousin;
  const [data, setData] = React.useState(null);
  const [loadErr, setLoadErr] = React.useState(null);
  const [draft, setDraft] = React.useState({});
  const [errors, setErrors] = React.useState({});
  const [msg, setMsg] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [waitOp, setWaitOp] = React.useState(false);
  const [op] = useLongOp(c.remote ? null : c.slug);
  const load = React.useCallback(async () => {
    const d = await apiGet(`/api/cousins/${c.slug}/agent`);
    if (!d) { setLoadErr("could not read the agent settings"); return; }
    setLoadErr(null); setData(d);
  }, [c.slug]);
  React.useEffect(() => {
    setData(null); setDraft({}); setErrors({}); setMsg(null); setSaved(false); setWaitOp(false);
    if (!c.remote) load();
  }, [c.slug, load]);
  // an sdk model change is a long operation: reload when it ends
  React.useEffect(() => {
    if (!waitOp || !op || op.kind !== "agent-settings" || op.status === "running") return;
    setWaitOp(false); setBusy(false);
    if (op.status === "done") {
      setDraft({});
      setSaved(!!(op.result && op.result.restart_required));
      setMsg(op.result && !op.result.changed.length ? { ok: true, text: "nothing changed" } : null);
    } else {
      setErrors({ model: op.error });
    }
    load();
  }, [op, waitOp, load]);
  if (c.remote) return null;
  if (!data) return <div style={{ ...agentMono, color: "var(--fg-3)" }}>{loadErr || "loading agent settings..."}</div>;

  const s = data.settings || {};
  const legacy = data.lane === AGENT_TMUX_LEGACY;
  const valueOf = (key) => (key in draft ? draft[key] : (s[key] || {}).value);
  const set = (key, v) => { setDraft(d => Object.assign({}, d, { [key]: v })); setSaved(false); setMsg(null); };
  const changes = agentChanges(s, draft);
  const dirty = Object.keys(changes).length > 0;
  const turns = data.lane === "sdk" && typeof changes.model === "string" && changes.model;

  const save = async () => {
    if (!dirty || busy) return;
    setBusy(true); setErrors({}); setMsg(null); setSaved(false);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${c.slug}/agent`, { changes });
      if (r.status === 202) { setWaitOp(true); return; }
      if (r.status === 400 && d.errors) { setErrors(d.errors); setBusy(false); return; }
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setDraft({});
      if (d.agent) setData(Object.assign({ slug: c.slug }, d.agent));
      setSaved(!!d.restart_required);
      if (!d.changed.length) setMsg({ ok: true, text: "nothing changed" });
      setBusy(false);
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
      setBusy(false);
    }
  };
  const order = Object.keys(s).filter(k => k !== "runner" && k !== "api_key_file");
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>agent</SectionLabel>
      <div data-agent-settings style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <span style={agentHint}>kind</span>
          <Pill tone={legacy ? "gray" : "cyan"}>{data.lane}</Pill>
          {data.held && (
            <span title="a stop wrote run/held: the supervisor keeps the runner down until its next start">
              <Pill tone="amber">held</Pill>
            </span>
          )}
          <button className="btn ghost" style={agentSmall} onClick={() => openKindSwitch(c.slug)}
                  title="switch this cousin to another kind (a migration)">switch kind</button>
        </div>
        {legacy && (
          <span style={agentHint}>
            the tmux-legacy lane has no [agent] settings: its model, effort and auth mode are the
            identity rows above ([runtime]). Switch kind to run it on a runner.
          </span>
        )}
        {s.api_key_file && (
          <div style={{ ...agentMono, fontSize: 10, color: "var(--amber)" }} data-api-key-file>
            [agent] api_key_file is deprecated: move the key to an account (config/accounts.toml)
          </div>
        )}
        {!legacy && (
          <dl className="kv" style={{ gridTemplateColumns: "110px 1fr" }}>
            {order.map(key => {
              const row = s[key];
              const err = errors[key] || (data.errors || {})[key];
              return (
                <React.Fragment key={key}>
                  <dt title={row.hint}>{key.replace(/_/g, " ")}</dt>
                  <dd style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                    <AgentField name={key} row={row} lane={data.lane} value={valueOf(key)}
                                onChange={v => set(key, v)} disabled={busy} />
                    {key === "env_allow" && row.base && (
                      <span style={agentHint}>
                        always passed: {row.base.join(" ")}; never passed (the hard deny, it wins):
                        {" "}{(row.deny_prefixes || []).map(p => p + "*").join(" ")} and credential names
                      </span>
                    )}
                    {key === "auto_start" && (
                      <span style={agentHint}>the supervisor starts it with itself; false keeps it down until started</span>
                    )}
                    {row.hint && key !== "auto_start" && <span style={agentHint}>{row.hint}</span>}
                    {row.set && !row.readonly && !(key in draft && draft[key] === null) && key !== "sessions" && (
                      <button className="btn ghost" style={{ ...agentSmall, alignSelf: "flex-start" }} disabled={busy}
                              onClick={() => set(key, null)}
                              title="remove the key from cousin.toml: the default applies">unset (default {row.default == null ? "none" : String(row.default)})</button>
                    )}
                    {err && <span style={agentErr}>{err}</span>}
                  </dd>
                </React.Fragment>
              );
            })}
          </dl>
        )}
        {!legacy && (
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <button className="btn primary" style={agentSmall} disabled={!dirty || busy} onClick={save}>
              {busy ? (waitOp ? "validating..." : "saving...") : turns ? "save · validates the model (one turn)" : "save"}
            </button>
            {dirty && <button className="btn ghost" style={agentSmall} disabled={busy}
                              onClick={() => { setDraft({}); setErrors({}); }}>discard</button>}
            {turns && <span style={agentHint}>a new sdk model is checked with one smallest turn on the cousin's account before it is written</span>}
          </div>
        )}
        {errors.changes && <span style={agentErr}>{errors.changes}</span>}
        {waitOp && <LongOpStatus slug={c.slug} kind="agent-settings" />}
        {msg && <span style={{ ...agentErr, color: msg.ok ? "var(--fg-2)" : "var(--red)" }}>{msg.text}</span>}
        {saved && <AgentRestartOffer cousin={c} />}
      </div>
      <CousinSettingsPanel cousin={c} />
    </>
  );
}

const COUSIN_SETTING_LABELS = {
  "cousin.name": "name", "cousin.peer_visible": "peer visible",
  "memory.proactive_recall": "recall lines", "memory.recall_keyword_only": "keyword recall",
  "memory.review_batch": "review batch", "memory.review_model": "review model",
  "lifecycle.flip_at": "flip at", "agent.commit_attribution": "commit attribution",
};

function CousinSettingsPanel({ cousin }) {
  const c = cousin;
  const [data, setData] = React.useState(null);
  const [draft, setDraft] = React.useState({});
  const [errors, setErrors] = React.useState({});
  const [busy, setBusy] = React.useState(false);
  const [saved, setSaved] = React.useState(null);
  const [msg, setMsg] = React.useState(null);
  const load = React.useCallback(async () => {
    const d = await apiGet(`/api/cousins/${c.slug}/settings`);
    if (d) setData(d);
  }, [c.slug]);
  React.useEffect(() => { setData(null); setDraft({}); setErrors({}); setSaved(null); setMsg(null); load(); }, [c.slug, load]);
  if (!data) return null;
  const f = data.fields || {};
  const valueOf = (n) => (n in draft ? draft[n] : (f[n] || {}).value);
  const set = (n, v) => { setDraft(d => Object.assign({}, d, { [n]: v })); setSaved(null); setMsg(null); };
  const changes = cousinSettingChanges(f, draft);
  const dirty = Object.keys(changes).length > 0;
  const flipDraft = valueOf("lifecycle.flip_at") ?? "";
  const flipBad = flipAtProblem(flipDraft);

  const save = async () => {
    if (!dirty || busy || flipBad) return;
    setBusy(true); setErrors({}); setMsg(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${c.slug}/settings`, { changes });
      if (r.status === 400 && d.errors) { setErrors(d.errors); return; }
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setDraft({});
      if (d.settings) setData(Object.assign({ slug: c.slug }, d.settings));
      setSaved(d.changed.length ? { restart: d.restart_required, note: d.note } : null);
      if (!d.changed.length) setMsg("nothing changed");
    } catch (e) {
      setErrors({ changes: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  const attribution = f["agent.commit_attribution"] || {};
  const install = attribution.install || {};
  const field = (n) => {
    const row = f[n];
    const v = valueOf(n);
    if (n === "agent.commit_attribution") {
      const sel = v === true ? "on" : v === false ? "off" : "";
      return (
        <select className="sel" value={sel} disabled={busy}
                onChange={e => set(n, e.target.value === "" ? null : e.target.value === "on")}>
          <option value="">install default ({install.value === false ? "off" : install.value === true ? "on" : "unreadable"})</option>
          <option value="on">on: the harness adds its attribution</option>
          <option value="off">off: no Co-Authored-By, no "Generated with"</option>
        </select>
      );
    }
    if (row.type === "bool") {
      return (
        <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={!!v} disabled={busy} onChange={e => set(n, e.target.checked)} />
          <span style={agentMono}>{v ? "true" : "false"}</span>
        </label>
      );
    }
    if (row.type === "count") {
      return <input className="txt" type="number" min="0" step="1" value={v ?? ""} disabled={busy} style={{ width: 90 }}
                    onChange={e => set(n, e.target.value === "" ? null : Number(e.target.value))} />;
    }
    return <input className="txt" value={v ?? ""} disabled={busy} style={{ width: "100%", fontFamily: "var(--mono)" }}
                  placeholder={n === "lifecycle.flip_at" ? `install default (${row.install || "never"})`
                               : n === "memory.review_model" ? "the cousin's own model" : ""}
                  onChange={e => set(n, e.target.value === "" && n !== "cousin.name" ? null : e.target.value)} />;
  };
  const ro = data.readonly || {};
  const roText = (v) => Array.isArray(v) ? (v.length ? v.map(h => typeof h === "string" ? h : (h.name || h.cmd || JSON.stringify(h))).join("; ") : "none")
                                         : (v == null || v === "" ? "-" : String(v));
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>cousin settings</SectionLabel>
      <div data-cousin-settings style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <dl className="kv" style={{ gridTemplateColumns: "110px 1fr" }}>
          {Object.keys(f).map(n => (
            <React.Fragment key={n}>
              <dt title={`cousin.toml [${n.split(".")[0]}] ${n.split(".")[1]}`}>{COUSIN_SETTING_LABELS[n] || n}</dt>
              <dd style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                {field(n)}
                {n === "lifecycle.flip_at" && (
                  <span style={agentHint}>effective: {f[n].effective || "never"}{flipBad ? ` · ${flipBad}` : ""}</span>
                )}
                {n === "agent.commit_attribution" && (
                  <span style={agentHint}>
                    effective: {attribution.effective ? "on" : "off"} · install default {String(install.value)} from {install.source}
                  </span>
                )}
                <span style={agentHint}>{f[n].hint}{f[n].restart ? "" : " · applies without a restart"}</span>
                {(errors[n] || (data.errors || {})[n]) && <span style={agentErr}>{errors[n] || data.errors[n]}</span>}
              </dd>
            </React.Fragment>
          ))}
          {Object.keys(ro).map(n => (
            <React.Fragment key={n}>
              <dt title={(data.readonly_why || {})[n]}>{n.replace(".", " ").replace(/_/g, " ")}</dt>
              <dd style={{ color: "var(--fg-2)" }} title={(data.readonly_why || {})[n]}>{roText(ro[n])} <span style={agentHint}>read-only</span></dd>
            </React.Fragment>
          ))}
        </dl>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn primary" style={agentSmall} disabled={!dirty || busy || !!flipBad} onClick={save}>
            {busy ? "saving..." : "save"}
          </button>
          {dirty && <button className="btn ghost" style={agentSmall} disabled={busy}
                            onClick={() => { setDraft({}); setErrors({}); }}>discard</button>}
          {msg && <span style={agentHint}>{msg}</span>}
        </div>
        {errors.changes && <span style={agentErr}>{errors.changes}</span>}
        {saved && saved.note && <span style={agentHint}>{saved.note}</span>}
        {saved && (saved.restart
          ? <AgentRestartOffer cousin={c} />
          : <span style={agentHint}><Pill tone="green">saved</Pill> applies without a restart</span>)}
      </div>
    </>
  );
}

registerSlot("inspector.lane", { id: "agent", order: 10,
                                 render: ({ cousin }) => <AgentSettingsPanel cousin={cousin} /> });

Object.assign(window, {
  AgentSettingsPanel, CousinSettingsPanel, AgentRestartOffer, AgentField, agentLaneReads,
  agentChanges, cousinSettingChanges, openKindSwitch,
});
