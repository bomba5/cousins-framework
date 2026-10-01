// Accounts: this package's own file (index.html's package block).
// Its routes are cousin_lib/console/routes_accounts.py.
//
// The Accounts view (a NAV entry): config/accounts.toml's entries and the
// host's login, each with its status (no model call), add / edit / remove
// through the validated writer, the write-only keys, and the login flows:
// the sign-in URL is shown here, and the code the page shows goes to its
// own write-only route (POST /api/accounts/<name>/code), never into a
// chat. The Inspector panel (slot inspector.panels): a runner cousin's
// account, check-auth and validate (one model turn), and a login from the
// login-required banner.
//
// A login is the account's long operation (console/longop.py under the
// key "account:<name>"): its state is GET /api/accounts/<name>/op and its
// steps come as the re-dispatched `fw-cousin-op` event with that slug.

const ACCOUNT_OP_PREFIX = "account:";
const ACCOUNT_SMALL = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const ACCOUNT_MONO = { fontFamily: "var(--mono)", fontSize: 11 };
const ACCOUNT_HINT = { fontSize: 10, color: "var(--fg-3)" };
// The keys a kind's entry takes, in the order the form shows them, and
// what each one means (the server's `fields` decides which are allowed).
const ACCOUNT_FIELD_HINTS = {
  config_dir: "the CLI's config dir, under the root (default data/accounts/<name>)",
  secret_file: "the token or key file, under .secrets/ (default .secrets/accounts/<name>)",
  data_dir: "opencode's HOME, under the root (default .secrets/accounts/<name>.opencode)",
  providers: "the provider ids the account runs on, comma separated (never anthropic); each needs its key, except opencode (its hosted free models need none)",
  endpoint: "a local OpenAI-compatible base URL, e.g. http://127.0.0.1:11434/v1 (no credentials in it)",
  endpoint_model: "the model id the endpoint serves",
  endpoint_context: "the model's context window, in tokens (optional)",
  endpoint_output: "its output bound, in tokens, below the context (optional)",
};
const ACCOUNT_INT_FIELDS = ["endpoint_context", "endpoint_output"];

function accountOpKey(name) { return ACCOUNT_OP_PREFIX + name; }

async function accountPost(path, body) {
  const { r, d } = await apiSend("POST", path, body || {});
  if (!r.ok) throw new Error((d && d.error) || `HTTP ${r.status}`);
  return d;
}

// [op, reload] of an account's long operation (a login), like ui.jsx's
// useLongOp for a cousin's.
function useAccountOp(name) {
  const [op, setOp] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!name) { setOp(null); return; }
    const d = await apiGet(`/api/accounts/${encodeURIComponent(name)}/op`);
    if (d) setOp(d.op || null);
  }, [name]);
  React.useEffect(() => {
    load();
    const on = (e) => { if ((e.detail || {}).slug === accountOpKey(name)) load(); };
    window.addEventListener("fw-cousin-op", on);
    return () => window.removeEventListener("fw-cousin-op", on);
  }, [name, load]);
  return [op, load];
}

function AccountOpStages({ op }) {
  if (!op) return null;
  return (
    <div className="longop" data-longop={op.kind}>
      <div className="longop-hdr">
        <StatePill state={op.status} />
        <span className="mono">{op.kind}</span>
        {op.finished_at && <span className="muted">{fmtAgo(Date.now() / 1000 - op.finished_at)} ago</span>}
      </div>
      <ol className="longop-stages">
        {(op.stages || []).map(s => (
          <li key={s.name}>
            <Led state={s.status === "skipped" ? "disabled" : s.status} pulse={s.status === "running"} />
            <span>{s.name}</span>
            {s.detail && <span className="muted"> · {s.detail}</span>}
          </li>
        ))}
      </ol>
      {op.error && <div className="longop-error">{op.error}</div>}
    </div>
  );
}

// A running login: its steps, the sign-in URL for the operator to open,
// opencode's instruction line, and while the CLI waits for it, the
// write-only box the code goes through (once).
function AccountFlow({ name, onDone }) {
  const [op] = useAccountOp(name);
  const [flow, setFlow] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const url = `/api/accounts/${encodeURIComponent(name)}`;
  const load = React.useCallback(async () => {
    const d = await apiGet(url + "/flow");
    if (d) setFlow(d);
  }, [url]);
  const running = op && op.status === "running";
  React.useEffect(() => { load(); }, [load, op && op.status, op && (op.stages || []).length]);
  // the URL arrives with a stage event; poll lightly while it runs, in
  // case the event stream dropped one
  React.useEffect(() => {
    if (!running) return undefined;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [running, load]);
  const wasRunning = React.useRef(false);
  React.useEffect(() => {
    if (wasRunning.current && op && op.status !== "running" && onDone) onDone(op);
    wasRunning.current = !!running;
  }, [op && op.status]);

  // SecretField (ui.jsx) clears its box before this is called
  const sendCode = async (code) => {
    setErr(null); setNote(null);
    try {
      await accountPost(url + "/code", { code });
      setNote("code sent; the CLI checks it");
    } catch (e) { setErr(String(e.message || e)); }
    load();
  };
  const cancel = async () => {
    setErr(null);
    try { await accountPost(url + "/cancel"); } catch (e) { setErr(String(e.message || e)); }
  };

  if (!op) return null;
  return (
    <div data-account-flow={name} style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
      <AccountOpStages op={op} />
      {running && flow && !flow.mine && (
        <div style={{ ...ACCOUNT_MONO, color: "var(--fg-2)" }}>
          this login was started from another console session: only that session sees its URL,
          sends its code or cancels it
        </div>
      )}
      {running && flow && flow.mine && flow.url && (
        <div style={{ ...ACCOUNT_MONO, display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ color: "var(--fg-2)" }}>open this sign-in URL and sign in:</span>
          {flow.url_is_https && /^https:\/\//.test(flow.url) ? (
            <a href={flow.url} target="_blank" rel="noopener noreferrer"
               style={{ color: "var(--accent)", wordBreak: "break-all" }}>{flow.url}</a>
          ) : (
            <span style={{ color: "var(--amber)", wordBreak: "break-all" }}>
              not https, so not a link; check it before you open it: {flow.url}
            </span>
          )}
          {flow.instructions && <span style={{ color: "var(--fg-1)" }}>opencode says: {flow.instructions}</span>}
          {flow.kind === "opencode-login" && (
            <span style={ACCOUNT_HINT}>nothing to paste back: opencode finishes the login by itself. A browser
              method only completes on this host (its callback goes to localhost); prefer a headless or device method.</span>
          )}
        </div>
      )}
      {running && flow && flow.awaiting_code && (
        <SecretField onSubmit={sendCode} placeholder="the whole code the page shows (code#state)"
                     submitLabel="send code" autoFocus
                     hint="Used once, by this login only; never kept, never in a chat. A second code is refused." />
      )}
      {running && flow && flow.mine && <div><button className="btn ghost" style={ACCOUNT_SMALL} onClick={cancel}>cancel login</button></div>}
      {note && <div style={{ ...ACCOUNT_MONO, color: "var(--green)" }}>{note}</div>}
      {err && <div style={{ ...ACCOUNT_MONO, color: "var(--red)" }}>{err}</div>}
    </div>
  );
}

// The status check of one account: `claude auth status` under it (or
// the opencode auth.json read), never a model call.
function AccountStatusCell({ name }) {
  const [st, setSt] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const check = async () => {
    setBusy(true);
    const { r, d } = await apiSend("GET", `/api/accounts/${encodeURIComponent(name)}/status`);
    setSt(r.ok ? d : { ok: false, error: (d && d.error) || `HTTP ${r.status}` });
    setBusy(false);
  };
  return (
    <div data-account-status={name} style={{ display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-start" }}>
      <button className="btn ghost" style={ACCOUNT_SMALL} disabled={busy} onClick={check}
              title="claude auth status under the account (opencode: its auth.json); no model call">
        {busy ? "checking..." : "check"}
      </button>
      {st && (
        <span style={{ ...ACCOUNT_MONO, whiteSpace: "normal" }}>
          <Pill tone={st.ok ? "green" : "amber"}>{st.ok ? "logged in" : "not logged in"}</Pill>
          {st.method ? ` ${st.method}` : ""}
          {st.missing && st.missing.length ? ` · missing ${st.missing.join(", ")}` : ""}
          {st.error && <div style={{ color: "var(--red)" }}>{st.error}</div>}
          {!st.ok && st.action && <div style={{ color: "var(--fg-2)" }}>fix: <code>{st.action.replace(/`/g, "")}</code></div>}
        </span>
      )}
    </div>
  );
}

// What an account can be logged in with, per kind: a login flow, a token
// flow, a write-only key, or per opencode provider a key or an OAuth
// method. The key boxes are SecretFields; the flows are AccountFlow.
function AccountLogin({ account, onChanged }) {
  const name = account.name;
  const base = `/api/accounts/${encodeURIComponent(name)}`;
  const [op, reloadOp] = useAccountOp(name);
  const [err, setErr] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const [hostOk, setHostOk] = React.useState(false);
  const [methods, setMethods] = React.useState({});
  const [secret, setSecret] = React.useState(account.secret);
  React.useEffect(() => { setSecret(account.secret); }, [account.secret]);
  const running = op && op.status === "running";

  const start = async (path, body) => {
    setErr(null); setNote(null);
    try { await accountPost(base + path, body); reloadOp(); }
    catch (e) { setErr(String(e.message || e)); }
  };
  const saveKey = async (key, provider) => {
    setErr(null); setNote(null);
    try {
      const d = await accountPost(base + "/key", provider ? { key, provider } : { key });
      if (d.secret) setSecret(d.secret);
      setNote(provider ? `${provider}: key saved` : "saved");
      if (onChanged) onChanged();
    } catch (e) { setErr(String(e.message || e)); }
  };

  let body = null;
  if (account.kind === "claude-login") {
    body = (
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        {account.implicit && (
          <label style={{ ...ACCOUNT_MONO, color: "var(--amber)", display: "inline-flex", gap: 4, alignItems: "center" }}>
            <input type="checkbox" checked={hostOk} onChange={e => setHostOk(e.target.checked)} />
            re-log the host's own ~/.claude (every cousin without an account and your own claude use it)
          </label>
        )}
        <button className="btn" style={ACCOUNT_SMALL} disabled={running || (account.implicit && !hostOk)}
                onClick={() => start("/login", account.implicit ? { confirm_host: true } : {})}>log in</button>
      </div>
    );
  } else if (account.kind === "claude-token") {
    body = (
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div><button className="btn" style={ACCOUNT_SMALL} disabled={running} onClick={() => start("/token")}
                     title="claude setup-token in a throwaway config dir; the token is verified, then saved 0600">mint a token</button></div>
        <SecretField status={secret} onSubmit={v => saveKey(v)} placeholder="or paste a token you minted"
                     submitLabel="save token" />
      </div>
    );
  } else if (account.kind === "anthropic-key") {
    body = <SecretField status={secret} onSubmit={v => saveKey(v)} placeholder="paste the API key" submitLabel="save key" />;
  } else if (account.kind === "opencode" && account.entry && account.entry.endpoint) {
    body = <span style={ACCOUNT_HINT}>a local endpoint: nothing to log in</span>;
  } else if (account.kind === "opencode") {
    const providers = (account.entry && account.entry.providers) || [];
    body = (
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {providers.map(p => (
          <div key={p} data-account-provider={p} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ ...ACCOUNT_MONO, color: "var(--fg-1)" }}>{p}</span>
            <SecretField onSubmit={v => saveKey(v, p)} placeholder={`${p} API key`} submitLabel="save key" />
            <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
              <input className="txt" style={{ flex: "1 1 160px", minWidth: 0 }} value={methods[p] || ""}
                     onChange={e => setMethods({ ...methods, [p]: e.target.value })}
                     placeholder="or an OAuth method, by its opencode label" />
              <button className="btn" style={ACCOUNT_SMALL} disabled={running || !(methods[p] || "").trim()}
                      onClick={() => start("/login", { provider: p, method: (methods[p] || "").trim() })}>sign in</button>
            </div>
          </div>
        ))}
        <span style={ACCOUNT_HINT}>Keys never go to a chat. Anthropic and Claude are refused here: Claude runs on the sdk lane only.</span>
      </div>
    );
  }
  return (
    <div data-account-login={name} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {body}
      <AccountFlow name={name} onDone={() => { if (onChanged) onChanged(); }} />
      {note && <div style={{ ...ACCOUNT_MONO, color: "var(--green)" }}>{note}</div>}
      {err && <div style={{ ...ACCOUNT_MONO, color: "var(--red)" }}>{err}</div>}
    </div>
  );
}

// The entry form: add (a name and a kind) or edit (the kind and its keys;
// the entry is replaced whole). An empty field means the default applies.
function accountEntryFrom(kind, values) {
  const entry = { kind };
  for (const [key, raw] of Object.entries(values)) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) continue;
    if (key === "providers") entry.providers = text.split(",").map(s => s.trim()).filter(Boolean);
    else if (ACCOUNT_INT_FIELDS.includes(key)) entry[key] = Number(text);
    else entry[key] = text;
  }
  return entry;
}

function AccountForm({ initial, kinds, fields, onClose, onSaved }) {
  const editing = !!initial;
  const [name, setName] = React.useState(initial ? initial.name : "");
  const [kind, setKind] = React.useState(initial ? initial.kind : (kinds[0] || "claude-login"));
  const start = {};
  const entry = (initial && initial.entry) || {};
  for (const [k, v] of Object.entries(entry)) {
    if (k !== "kind") start[k] = Array.isArray(v) ? v.join(", ") : String(v);
  }
  const [values, setValues] = React.useState(start);
  const [err, setErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  // the allowed keys in the order the hints list them, then any other
  const allowed = (fields && fields[kind]) || [];
  const keys = Object.keys(ACCOUNT_FIELD_HINTS).filter(k => allowed.includes(k))
    .concat(allowed.filter(k => !(k in ACCOUNT_FIELD_HINTS)));
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const save = async () => {
    setBusy(true); setErr(null);
    const picked = {};
    for (const k of keys) picked[k] = values[k];
    try {
      const body = { entry: accountEntryFrom(kind, picked) };
      if (editing) await accountPost(`/api/accounts/${encodeURIComponent(name)}`, body);
      else await accountPost("/api/accounts", { name: name.trim(), ...body });
      onSaved();
    } catch (e) { setErr(String(e.message || e)); }
    setBusy(false);
  };
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()} data-account-form>
        <div className="hdr">
          <span>{editing ? `edit account ${name}` : "add account"}</span>
          <span style={{ color: "var(--fg-3)", marginLeft: "auto" }}>config/accounts.toml</span>
        </div>
        <div className="body">
          <div className="grid2">
            <FormField label="name" hint="lowercase letters, digits, - and _; not host">
              <input className="txt" value={name} disabled={editing} autoFocus={!editing}
                     onChange={e => setName(e.target.value.toLowerCase())} placeholder="fleet" />
            </FormField>
            <FormField label="kind" hint="claude kinds run on sdk and fake, opencode on opencode">
              <select className="sel" value={kind} onChange={e => setKind(e.target.value)}>
                {kinds.map(k => <option key={k} value={k}>{k}</option>)}
              </select>
            </FormField>
          </div>
          {keys.map(k => (
            <div key={k} style={{ marginTop: 12 }}>
              <FormField label={k} hint={ACCOUNT_FIELD_HINTS[k] || ""}>
                <input className="txt" value={values[k] || ""} type={ACCOUNT_INT_FIELDS.includes(k) ? "number" : "text"}
                       onChange={e => setValues({ ...values, [k]: e.target.value })} />
              </FormField>
            </div>
          ))}
          {kind === "opencode" && (
            <div style={{ ...ACCOUNT_HINT, marginTop: 12 }}>
              opencode: fill providers, or endpoint with endpoint_model, not both.
            </div>
          )}
          <div style={{ ...ACCOUNT_HINT, marginTop: 12 }}>
            No secret goes in this file: a key or token is set from the account's row, write-only.
          </div>
          {err && <div style={{ ...ACCOUNT_MONO, color: "var(--red)", marginTop: 8 }}>{err}</div>}
        </div>
        <div className="foot">
          <button className="btn" onClick={onClose}>cancel</button>
          <button className="btn primary" disabled={busy || (!editing && !name.trim())} onClick={save}>
            {busy ? "saving..." : editing ? "save" : "add"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Remove: the operator types the account's name (a destructive step).
function AccountRemove({ account, onRemoved }) {
  const [open, setOpen] = React.useState(false);
  const [typed, setTyped] = React.useState("");
  const [err, setErr] = React.useState(null);
  if (account.implicit) return null;
  if (!open) {
    return <div><button className="btn ghost danger-text" style={ACCOUNT_SMALL} onClick={() => setOpen(true)}>remove</button></div>;
  }
  const remove = async () => {
    setErr(null);
    try {
      await accountPost(`/api/accounts/${encodeURIComponent(account.name)}/remove`, { confirm: typed });
      onRemoved();
    } catch (e) { setErr(String(e.message || e)); }
  };
  return (
    <div data-account-remove style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
      <input className="txt" style={{ width: 140 }} value={typed} onChange={e => setTyped(e.target.value)}
             placeholder={`type ${account.name}`} autoFocus />
      <button className="btn danger" style={ACCOUNT_SMALL} disabled={typed !== account.name} onClick={remove}>remove</button>
      <button className="btn ghost" style={ACCOUNT_SMALL} onClick={() => { setOpen(false); setTyped(""); setErr(null); }}>keep</button>
      {err && <span style={{ ...ACCOUNT_MONO, color: "var(--red)" }}>{err}</span>}
    </div>
  );
}

function AccountsView() {
  const [data, setData] = React.useState(null);
  const [loadErr, setLoadErr] = React.useState(false);
  const [open, setOpen] = React.useState(null);        // the expanded row's name
  const [form, setForm] = React.useState(null);        // {initial} or {} for add
  const load = React.useCallback(async () => {
    const d = await apiGet("/api/accounts");
    if (d) { setData(d); setLoadErr(false); } else setLoadErr(true);
  }, []);
  React.useEffect(() => {
    load();
    // another console session added, edited or removed an entry, or set a key
    const on = () => load();
    window.addEventListener("fw-accounts-change", on);
    return () => window.removeEventListener("fw-accounts-change", on);
  }, [load]);

  if (!data) {
    return <div className="wrap-pad" style={ACCOUNT_MONO}>{loadErr ? "accounts unavailable" : "loading..."}</div>;
  }
  const rows = data.accounts || [];
  return (
    <div className="wrap-pad" data-accounts-view>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap" }}>
        <span style={{ ...ACCOUNT_MONO, color: "var(--fg-3)" }}>
          {rows.length} accounts · the credentials runner cousins run on; no secret is ever shown
        </span>
        <span style={{ flex: 1 }} />
        <button className="btn primary" onClick={() => setForm({})}>{I.plus} add account</button>
      </div>
      {data.error && (
        <div className="panel" style={{ ...ACCOUNT_MONO, color: "var(--amber)", padding: 10, marginBottom: 12 }}>
          config/accounts.toml: {data.error}
        </div>
      )}
      <div className="panel table-scroll">
        <table className="data accounts-table">
          <thead>
            <tr>
              <th>name</th>
              <th>kind</th>
              <th>where</th>
              <th>lanes</th>
              <th>cousins</th>
              <th>status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(a => (
              <React.Fragment key={a.name}>
                <tr data-account-row={a.name}>
                  <td className="mono" data-label="name" style={{ color: "var(--fg-0)" }}>{a.name}</td>
                  <td className="mono" data-label="kind">{a.kind}</td>
                  <td className="mono muted" data-label="where">
                    {a.where}
                    {a.secret && <div>{secretStateText(a.secret)}{a.secret.error ? ` · ${a.secret.error}` : ""}</div>}
                  </td>
                  <td className="mono" data-label="lanes">{(a.lanes || []).join(", ") || "-"}</td>
                  <td className="mono" data-label="cousins">{(a.cousins || []).map(s => "@" + s).join(" ") || <span className="muted">none</span>}</td>
                  <td data-label="status"><AccountStatusCell name={a.name} /></td>
                  <td data-label="">
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <button className="btn" style={ACCOUNT_SMALL} onClick={() => setOpen(open === a.name ? null : a.name)}>
                        {open === a.name ? "close" : "log in / keys"}
                      </button>
                      {!a.implicit && <button className="btn ghost" style={ACCOUNT_SMALL} onClick={() => setForm({ initial: a })}>edit</button>}
                    </div>
                  </td>
                </tr>
                {open === a.name && (
                  <tr>
                    <td colSpan={7}>
                      <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: "6px 0" }}>
                        <AccountLogin account={a} onChanged={load} />
                        <AccountRemove account={a} onRemoved={() => { setOpen(null); load(); }} />
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {form && (
        <AccountForm initial={form.initial} kinds={data.kinds || []} fields={data.fields || {}}
                     onClose={() => setForm(null)} onSaved={() => { setForm(null); load(); }} />
      )}
    </div>
  );
}

// The Inspector's account panel for a runner cousin: its account, the
// check-auth (no model call) and validate (one model turn) operations,
// and, while data/login-required.json stands, the action line and the
// account's login right here.
function CousinAccountPanel({ cousin }) {
  const c = cousin;
  const runnerLane = c.lane && c.lane !== "tmux-legacy" && !c.remote;
  const [err, setErr] = React.useState(null);
  const [account, setAccount] = React.useState(null);
  const [showLogin, setShowLogin] = React.useState(false);
  const [op] = useLongOp(runnerLane ? c.slug : null);
  React.useEffect(() => {
    setErr(null); setShowLogin(false); setAccount(null);
    if (!runnerLane) return;
    if (!c.account) return;       // the deprecated [agent] api_key_file: no account row
    apiGet("/api/accounts").then(d => {
      const row = d && (d.accounts || []).find(a => a.name === c.account);
      setAccount(row || null);
    });
  }, [c.slug, c.account, runnerLane]);
  if (!runnerLane) return null;

  const run = async (validate) => {
    if (validate && !window.confirm(`validate spends one smallest model turn on ${c.account ? "account " + c.account : "the cousin's key"}. Run it?`)) return;
    setErr(null);
    try { await accountPost(`/api/cousins/${encodeURIComponent(c.slug)}/check-auth`, { validate }); }
    catch (e) { setErr(String(e.message || e)); }
  };
  const busy = op && op.status === "running";
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>account</SectionLabel>
      <div data-account-panel style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ ...ACCOUNT_MONO, color: "var(--fg-1)" }}>
          {c.account
            ? <>{c.account}{account ? ` · ${account.kind}` : ""}</>
            : <span style={{ color: "var(--amber)" }}>[agent] api_key_file (deprecated: move the key to an account)</span>}
        </div>
        {c.loginRequired && (
          <div className="card-attention" data-login-required>
            {c.loginRequired.reason === "billing" ? "billing stopped" : "login required"}
            {c.loginRequired.action ? <> · run <code>{c.loginRequired.action}</code></> : null}
            {account && c.loginRequired.reason !== "billing" && !showLogin && (
              <> · <button className="btn" style={ACCOUNT_SMALL} onClick={() => setShowLogin(true)}>log in here</button></>
            )}
          </div>
        )}
        {showLogin && account && <AccountLogin account={account} />}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <button className="btn" style={ACCOUNT_SMALL} disabled={busy} onClick={() => run(false)}
                  title="cousin-runner --check-auth: is the account logged in (no model call)">check auth</button>
          {c.lane === "sdk" && (
            <button className="btn" style={ACCOUNT_SMALL} disabled={busy} onClick={() => run(true)}
                    title="cousin-runner --check-auth --validate: spends one smallest model turn on a throwaway client">
              validate · spends one model turn
            </button>
          )}
        </div>
        {op && (op.kind === "check-auth" || op.kind === "validate") && <AccountOpStages op={op} />}
        {err && <div style={{ ...ACCOUNT_MONO, color: "var(--red)" }}>{err}</div>}
      </div>
    </>
  );
}

registerView({ id: "accounts", label: "Accounts", icon: I.host, order: 50,
               render: (props) => <AccountsView {...props} /> });
registerSlot("inspector.panels", { id: "account", order: 20,
                                   render: ({ cousin }) => <CousinAccountPanel cousin={cousin} /> });

Object.assign(window, {
  AccountsView, AccountFlow, AccountLogin, AccountForm, AccountRemove, AccountStatusCell,
  CousinAccountPanel, useAccountOp, accountEntryFrom,
});
