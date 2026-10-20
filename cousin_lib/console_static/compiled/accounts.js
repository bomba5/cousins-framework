const ACCOUNT_OP_PREFIX = "account:";
const ACCOUNT_SMALL = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const ACCOUNT_MONO = { fontFamily: "var(--mono)", fontSize: 11 };
const ACCOUNT_HINT = { fontSize: 10, color: "var(--fg-3)" };
const ACCOUNT_FIELD_HINTS = {
  config_dir: "the CLI's config dir, under the root (default data/accounts/<name>)",
  secret_file: "the token or key file, under .secrets/ (default .secrets/accounts/<name>)",
  data_dir: "opencode's HOME, under the root (default .secrets/accounts/<name>.opencode)",
  providers: "the provider ids the account runs on, comma separated (never anthropic); each needs its key, except opencode (its hosted free models need none)",
  endpoint: "a local OpenAI-compatible base URL, e.g. http://127.0.0.1:11434/v1 (no credentials in it)",
  endpoint_model: "the model id the endpoint serves",
  endpoint_context: "the model's context window, in tokens (optional)",
  endpoint_output: "its output bound, in tokens, below the context (optional)"
};
const ACCOUNT_INT_FIELDS = ["endpoint_context", "endpoint_output"];
function accountOpKey(name) {
  return ACCOUNT_OP_PREFIX + name;
}
async function accountPost(path, body) {
  const { r, d } = await apiSend("POST", path, body || {});
  if (!r.ok) throw new Error(d && d.error || `HTTP ${r.status}`);
  return d;
}
function useAccountOp(name) {
  const [op, setOp] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!name) {
      setOp(null);
      return;
    }
    const d = await apiGet(`/api/accounts/${encodeURIComponent(name)}/op`);
    if (d) setOp(d.op || null);
  }, [name]);
  React.useEffect(() => {
    load();
    const on = (e) => {
      if ((e.detail || {}).slug === accountOpKey(name)) load();
    };
    window.addEventListener("fw-cousin-op", on);
    return () => window.removeEventListener("fw-cousin-op", on);
  }, [name, load]);
  return [op, load];
}
function AccountOpStages({ op }) {
  if (!op) return null;
  return /* @__PURE__ */ React.createElement("div", { className: "longop", "data-longop": op.kind }, /* @__PURE__ */ React.createElement("div", { className: "longop-hdr" }, /* @__PURE__ */ React.createElement(StatePill, { state: op.status }), /* @__PURE__ */ React.createElement("span", { className: "mono" }, op.kind), op.finished_at && /* @__PURE__ */ React.createElement("span", { className: "muted" }, fmtAgo(Date.now() / 1e3 - op.finished_at))), /* @__PURE__ */ React.createElement("ol", { className: "longop-stages" }, (op.stages || []).map((s) => /* @__PURE__ */ React.createElement("li", { key: s.name }, /* @__PURE__ */ React.createElement(Led, { state: s.status === "skipped" ? "disabled" : s.status, pulse: s.status === "running" }), /* @__PURE__ */ React.createElement("span", null, s.name), s.detail && /* @__PURE__ */ React.createElement("span", { className: "muted" }, " · ", s.detail)))), op.error && /* @__PURE__ */ React.createElement("div", { className: "longop-error" }, op.error));
}
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
  React.useEffect(() => {
    load();
  }, [load, op && op.status, op && (op.stages || []).length]);
  React.useEffect(() => {
    if (!running) return void 0;
    const id = setInterval(load, 3e3);
    return () => clearInterval(id);
  }, [running, load]);
  const wasRunning = React.useRef(false);
  React.useEffect(() => {
    if (wasRunning.current && op && op.status !== "running" && onDone) onDone(op);
    wasRunning.current = !!running;
  }, [op && op.status]);
  const sendCode = async (code) => {
    setErr(null);
    setNote(null);
    try {
      await accountPost(url + "/code", { code });
      setNote("code sent; the CLI checks it");
    } catch (e) {
      setErr(String(e.message || e));
    }
    load();
  };
  const cancel = async () => {
    setErr(null);
    try {
      await accountPost(url + "/cancel");
    } catch (e) {
      setErr(String(e.message || e));
    }
  };
  if (!op) return null;
  return /* @__PURE__ */ React.createElement("div", { "data-account-flow": name, style: { display: "flex", flexDirection: "column", gap: 8, marginTop: 8 } }, /* @__PURE__ */ React.createElement(AccountOpStages, { op }), running && flow && !flow.mine && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, color: "var(--fg-2)" } }, "this login was started from another console session: only that session sees its URL, sends its code or cancels it"), running && flow && flow.mine && flow.url && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, display: "flex", flexDirection: "column", gap: 4 } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-2)" } }, "open this sign-in URL and sign in:"), flow.url_is_https && /^https:\/\//.test(flow.url) ? /* @__PURE__ */ React.createElement(
    "a",
    {
      href: flow.url,
      target: "_blank",
      rel: "noopener noreferrer",
      style: { color: "var(--accent)", wordBreak: "break-all" }
    },
    flow.url
  ) : /* @__PURE__ */ React.createElement("span", { style: { color: "var(--amber)", wordBreak: "break-all" } }, "not https, so not a link; check it before you open it: ", flow.url), flow.instructions && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-1)" } }, "opencode says: ", flow.instructions), flow.kind === "opencode-login" && /* @__PURE__ */ React.createElement("span", { style: ACCOUNT_HINT }, "nothing to paste back: opencode finishes the login by itself. A browser method only completes on this host (its callback goes to localhost); prefer a headless or device method.")), running && flow && flow.awaiting_code && /* @__PURE__ */ React.createElement(
    SecretField,
    {
      onSubmit: sendCode,
      placeholder: "the whole code the page shows (code#state)",
      submitLabel: "send code",
      autoFocus: true,
      hint: "Used once, by this login only; never kept, never in a chat. A second code is refused."
    }
  ), running && flow && flow.mine && /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: ACCOUNT_SMALL, onClick: cancel }, "cancel login")), note && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, color: "var(--green)" } }, note), err && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, color: "var(--red)" } }, err));
}
function AccountStatusCell({ name }) {
  const [st, setSt] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const check = async () => {
    setBusy(true);
    const { r, d } = await apiSend("GET", `/api/accounts/${encodeURIComponent(name)}/status`);
    setSt(r.ok ? d : { ok: false, error: d && d.error || `HTTP ${r.status}` });
    setBusy(false);
  };
  return /* @__PURE__ */ React.createElement("div", { "data-account-status": name, style: { display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-start" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: ACCOUNT_SMALL,
      disabled: busy,
      onClick: check,
      title: "claude auth status under the account (opencode: its auth.json); no model call"
    },
    busy ? "checking..." : "check"
  ), st && /* @__PURE__ */ React.createElement("span", { style: { ...ACCOUNT_MONO, whiteSpace: "normal" } }, /* @__PURE__ */ React.createElement(Pill, { tone: st.ok ? "green" : "amber" }, st.ok ? "logged in" : "not logged in"), st.method ? ` ${st.method}` : "", st.missing && st.missing.length ? ` · missing ${st.missing.join(", ")}` : "", st.error && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--red)" } }, st.error), !st.ok && st.action && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-2)" } }, "fix: ", /* @__PURE__ */ React.createElement("code", null, st.action.replace(/`/g, "")))));
}
function AccountLogin({ account, onChanged }) {
  const name = account.name;
  const base = `/api/accounts/${encodeURIComponent(name)}`;
  const [op, reloadOp] = useAccountOp(name);
  const [err, setErr] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const [hostOk, setHostOk] = React.useState(false);
  const [methods, setMethods] = React.useState({});
  const [secret, setSecret] = React.useState(account.secret);
  React.useEffect(() => {
    setSecret(account.secret);
  }, [account.secret]);
  const running = op && op.status === "running";
  const start = async (path, body2) => {
    setErr(null);
    setNote(null);
    try {
      await accountPost(base + path, body2);
      reloadOp();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };
  const saveKey = async (key, provider) => {
    setErr(null);
    setNote(null);
    try {
      const d = await accountPost(base + "/key", provider ? { key, provider } : { key });
      if (d.secret) setSecret(d.secret);
      setNote(provider ? `${provider}: key saved` : "saved");
      if (onChanged) onChanged();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };
  let body = null;
  if (account.kind === "claude-login") {
    body = /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } }, account.implicit && /* @__PURE__ */ React.createElement("label", { style: { ...ACCOUNT_MONO, color: "var(--amber)", display: "inline-flex", gap: 4, alignItems: "center" } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: hostOk, onChange: (e) => setHostOk(e.target.checked) }), "re-log the host's own ~/.claude (every cousin without an account and your own claude use it)"), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn",
        style: ACCOUNT_SMALL,
        disabled: running || account.implicit && !hostOk,
        onClick: () => start("/login", account.implicit ? { confirm_host: true } : {})
      },
      "log in"
    ));
  } else if (account.kind === "claude-token") {
    body = /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 6 } }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn",
        style: ACCOUNT_SMALL,
        disabled: running,
        onClick: () => start("/token"),
        title: "claude setup-token in a throwaway config dir; the token is verified, then saved 0600"
      },
      "mint a token"
    )), /* @__PURE__ */ React.createElement(
      SecretField,
      {
        status: secret,
        onSubmit: (v) => saveKey(v),
        placeholder: "or paste a token you minted",
        submitLabel: "save token"
      }
    ));
  } else if (account.kind === "anthropic-key") {
    body = /* @__PURE__ */ React.createElement(SecretField, { status: secret, onSubmit: (v) => saveKey(v), placeholder: "paste the API key", submitLabel: "save key" });
  } else if (account.kind === "opencode" && account.entry && account.entry.endpoint) {
    body = /* @__PURE__ */ React.createElement("span", { style: ACCOUNT_HINT }, "a local endpoint: nothing to log in");
  } else if (account.kind === "opencode") {
    const providers = account.entry && account.entry.providers || [];
    body = /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 10 } }, providers.map((p) => /* @__PURE__ */ React.createElement("div", { key: p, "data-account-provider": p, style: { display: "flex", flexDirection: "column", gap: 4 } }, /* @__PURE__ */ React.createElement("span", { style: { ...ACCOUNT_MONO, color: "var(--fg-1)" } }, p), /* @__PURE__ */ React.createElement(SecretField, { onSubmit: (v) => saveKey(v, p), placeholder: `${p} API key`, submitLabel: "save key" }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "txt",
        style: { flex: "1 1 160px", minWidth: 0 },
        value: methods[p] || "",
        onChange: (e) => setMethods({ ...methods, [p]: e.target.value }),
        placeholder: "or an OAuth method, by its opencode label"
      }
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn",
        style: ACCOUNT_SMALL,
        disabled: running || !(methods[p] || "").trim(),
        onClick: () => start("/login", { provider: p, method: (methods[p] || "").trim() })
      },
      "sign in"
    )))), /* @__PURE__ */ React.createElement("span", { style: ACCOUNT_HINT }, "Keys never go to a chat. Anthropic and Claude are refused here: Claude runs on the sdk lane only."));
  }
  return /* @__PURE__ */ React.createElement("div", { "data-account-login": name, style: { display: "flex", flexDirection: "column", gap: 6 } }, body, /* @__PURE__ */ React.createElement(AccountFlow, { name, onDone: () => {
    if (onChanged) onChanged();
  } }), note && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, color: "var(--green)" } }, note), err && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, color: "var(--red)" } }, err));
}
function accountEntryFrom(kind, values) {
  const entry = { kind };
  for (const [key, raw] of Object.entries(values)) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) continue;
    if (key === "providers") entry.providers = text.split(",").map((s) => s.trim()).filter(Boolean);
    else if (ACCOUNT_INT_FIELDS.includes(key)) entry[key] = Number(text);
    else entry[key] = text;
  }
  return entry;
}
function AccountForm({ initial, kinds, fields, onClose, onSaved }) {
  const editing = !!initial;
  const [name, setName] = React.useState(initial ? initial.name : "");
  const [kind, setKind] = React.useState(initial ? initial.kind : kinds[0] || "claude-login");
  const start = {};
  const entry = initial && initial.entry || {};
  for (const [k, v] of Object.entries(entry)) {
    if (k !== "kind") start[k] = Array.isArray(v) ? v.join(", ") : String(v);
  }
  const [values, setValues] = React.useState(start);
  const [err, setErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const allowed = fields && fields[kind] || [];
  const keys = Object.keys(ACCOUNT_FIELD_HINTS).filter((k) => allowed.includes(k)).concat(allowed.filter((k) => !(k in ACCOUNT_FIELD_HINTS)));
  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const save = async () => {
    setBusy(true);
    setErr(null);
    const picked = {};
    for (const k of keys) picked[k] = values[k];
    try {
      const body = { entry: accountEntryFrom(kind, picked) };
      if (editing) await accountPost(`/api/accounts/${encodeURIComponent(name)}`, body);
      else await accountPost("/api/accounts", { name: name.trim(), ...body });
      onSaved();
    } catch (e) {
      setErr(String(e.message || e));
    }
    setBusy(false);
  };
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", onClick: (e) => e.stopPropagation(), "data-account-form": true }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, editing ? `edit account ${name}` : "add account"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", marginLeft: "auto" } }, "config/accounts.toml")), /* @__PURE__ */ React.createElement("div", { className: "body" }, /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "name", hint: "lowercase letters, digits, - and _; not host" }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: name,
      disabled: editing,
      autoFocus: !editing,
      onChange: (e) => setName(e.target.value.toLowerCase()),
      placeholder: "fleet"
    }
  )), /* @__PURE__ */ React.createElement(FormField, { label: "kind", hint: "claude kinds run on sdk and fake, opencode on opencode" }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: kind, onChange: (e) => setKind(e.target.value) }, kinds.map((k) => /* @__PURE__ */ React.createElement("option", { key: k, value: k }, k))))), keys.map((k) => /* @__PURE__ */ React.createElement("div", { key: k, style: { marginTop: 12 } }, /* @__PURE__ */ React.createElement(FormField, { label: k, hint: ACCOUNT_FIELD_HINTS[k] || "" }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: values[k] || "",
      type: ACCOUNT_INT_FIELDS.includes(k) ? "number" : "text",
      onChange: (e) => setValues({ ...values, [k]: e.target.value })
    }
  )))), kind === "opencode" && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_HINT, marginTop: 12 } }, "opencode: fill providers, or endpoint with endpoint_model, not both."), /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_HINT, marginTop: 12 } }, "No secret goes in this file: a key or token is set from the account's row, write-only."), err && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, color: "var(--red)", marginTop: 8 } }, err)), /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: onClose }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: busy || !editing && !name.trim(), onClick: save }, busy ? "saving..." : editing ? "save" : "add"))));
}
function AccountRemove({ account, onRemoved }) {
  const [open, setOpen] = React.useState(false);
  const [typed, setTyped] = React.useState("");
  const [err, setErr] = React.useState(null);
  if (account.implicit) return null;
  if (!open) {
    return /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("button", { className: "btn ghost danger-text", style: ACCOUNT_SMALL, onClick: () => setOpen(true) }, "remove"));
  }
  const remove = async () => {
    setErr(null);
    try {
      await accountPost(`/api/accounts/${encodeURIComponent(account.name)}/remove`, { confirm: typed });
      onRemoved();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };
  return /* @__PURE__ */ React.createElement("div", { "data-account-remove": true, style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      style: { width: 140 },
      value: typed,
      onChange: (e) => setTyped(e.target.value),
      placeholder: `type ${account.name}`,
      autoFocus: true
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: ACCOUNT_SMALL, disabled: typed !== account.name, onClick: remove }, "remove"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: ACCOUNT_SMALL, onClick: () => {
    setOpen(false);
    setTyped("");
    setErr(null);
  } }, "keep"), err && /* @__PURE__ */ React.createElement("span", { style: { ...ACCOUNT_MONO, color: "var(--red)" } }, err));
}
function AccountsView() {
  const [data, setData] = React.useState(null);
  const [loadErr, setLoadErr] = React.useState(false);
  const [open, setOpen] = React.useState(null);
  const [form, setForm] = React.useState(null);
  const load = React.useCallback(async () => {
    const d = await apiGet("/api/accounts");
    if (d) {
      setData(d);
      setLoadErr(false);
    } else setLoadErr(true);
  }, []);
  React.useEffect(() => {
    load();
    const on = () => load();
    window.addEventListener("fw-accounts-change", on);
    return () => window.removeEventListener("fw-accounts-change", on);
  }, [load]);
  if (!data) {
    return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad", style: ACCOUNT_MONO }, loadErr ? "accounts unavailable" : "loading...");
  }
  const rows = data.accounts || [];
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad", "data-accounts-view": true }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: { ...ACCOUNT_MONO, color: "var(--fg-3)" } }, rows.length, " accounts · the credentials runner cousins run on; no secret is ever shown"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: () => setForm({}) }, I.plus, " add account")), data.error && /* @__PURE__ */ React.createElement("div", { className: "panel", style: { ...ACCOUNT_MONO, color: "var(--amber)", padding: 10, marginBottom: 12 } }, "config/accounts.toml: ", data.error), /* @__PURE__ */ React.createElement("div", { className: "panel table-scroll" }, /* @__PURE__ */ React.createElement("table", { className: "data accounts-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "name"), /* @__PURE__ */ React.createElement("th", null, "kind"), /* @__PURE__ */ React.createElement("th", null, "where"), /* @__PURE__ */ React.createElement("th", null, "lanes"), /* @__PURE__ */ React.createElement("th", null, "cousins"), /* @__PURE__ */ React.createElement("th", null, "status"), /* @__PURE__ */ React.createElement("th", null))), /* @__PURE__ */ React.createElement("tbody", null, rows.map((a) => /* @__PURE__ */ React.createElement(React.Fragment, { key: a.name }, /* @__PURE__ */ React.createElement("tr", { "data-account-row": a.name }, /* @__PURE__ */ React.createElement("td", { className: "mono", "data-label": "name", style: { color: "var(--fg-0)" } }, a.name), /* @__PURE__ */ React.createElement("td", { className: "mono", "data-label": "kind" }, a.kind), /* @__PURE__ */ React.createElement("td", { className: "mono muted", "data-label": "where" }, a.where, a.secret && /* @__PURE__ */ React.createElement("div", null, secretStateText(a.secret), a.secret.error ? ` · ${a.secret.error}` : "")), /* @__PURE__ */ React.createElement("td", { className: "mono", "data-label": "lanes" }, (a.lanes || []).join(", ") || "-"), /* @__PURE__ */ React.createElement("td", { className: "mono", "data-label": "cousins" }, (a.cousins || []).map((s) => "@" + s).join(" ") || /* @__PURE__ */ React.createElement("span", { className: "muted" }, "none")), /* @__PURE__ */ React.createElement("td", { "data-label": "status" }, /* @__PURE__ */ React.createElement(AccountStatusCell, { name: a.name })), /* @__PURE__ */ React.createElement("td", { "data-label": "" }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("button", { className: "btn", style: ACCOUNT_SMALL, onClick: () => setOpen(open === a.name ? null : a.name) }, open === a.name ? "close" : "log in / keys"), !a.implicit && /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: ACCOUNT_SMALL, onClick: () => setForm({ initial: a }) }, "edit")))), open === a.name && /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", { colSpan: 7 }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 10, padding: "6px 0" } }, /* @__PURE__ */ React.createElement(AccountLogin, { account: a, onChanged: load }), /* @__PURE__ */ React.createElement(AccountRemove, { account: a, onRemoved: () => {
    setOpen(null);
    load();
  } }))))))))), form && /* @__PURE__ */ React.createElement(
    AccountForm,
    {
      initial: form.initial,
      kinds: data.kinds || [],
      fields: data.fields || {},
      onClose: () => setForm(null),
      onSaved: () => {
        setForm(null);
        load();
      }
    }
  ));
}
function CousinAccountPanel({ cousin }) {
  const c = cousin;
  const runnerLane = c.lane && c.lane !== "tmux-legacy" && !c.remote;
  const [err, setErr] = React.useState(null);
  const [account, setAccount] = React.useState(null);
  const [showLogin, setShowLogin] = React.useState(false);
  const [op] = useLongOp(runnerLane ? c.slug : null);
  React.useEffect(() => {
    setErr(null);
    setShowLogin(false);
    setAccount(null);
    if (!runnerLane) return;
    if (!c.account) return;
    apiGet("/api/accounts").then((d) => {
      const row = d && (d.accounts || []).find((a) => a.name === c.account);
      setAccount(row || null);
    });
  }, [c.slug, c.account, runnerLane]);
  if (!runnerLane) return null;
  const run = async (validate) => {
    if (validate && !window.confirm(`validate spends one smallest model turn on ${c.account ? "account " + c.account : "the cousin's key"}. Run it?`)) return;
    setErr(null);
    try {
      await accountPost(`/api/cousins/${encodeURIComponent(c.slug)}/check-auth`, { validate });
    } catch (e) {
      setErr(String(e.message || e));
    }
  };
  const busy = op && op.status === "running";
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "account"), /* @__PURE__ */ React.createElement("div", { "data-account-panel": true, style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, color: "var(--fg-1)" } }, c.account ? /* @__PURE__ */ React.createElement(React.Fragment, null, c.account, account ? ` · ${account.kind}` : "") : /* @__PURE__ */ React.createElement("span", { style: { color: "var(--amber)" } }, "[agent] api_key_file (deprecated: move the key to an account)")), c.loginRequired && /* @__PURE__ */ React.createElement("div", { className: "card-attention", "data-login-required": true }, c.loginRequired.reason === "billing" ? "billing stopped" : "login required", c.loginRequired.action ? /* @__PURE__ */ React.createElement(React.Fragment, null, " · run ", /* @__PURE__ */ React.createElement("code", null, c.loginRequired.action)) : null, account && c.loginRequired.reason !== "billing" && !showLogin && /* @__PURE__ */ React.createElement(React.Fragment, null, " · ", /* @__PURE__ */ React.createElement("button", { className: "btn", style: ACCOUNT_SMALL, onClick: () => setShowLogin(true) }, "log in here"))), showLogin && account && /* @__PURE__ */ React.createElement(AccountLogin, { account }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: ACCOUNT_SMALL,
      disabled: busy,
      onClick: () => run(false),
      title: "cousin-runner --check-auth: is the account logged in (no model call)"
    },
    "check auth"
  ), c.lane === "sdk" && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: ACCOUNT_SMALL,
      disabled: busy,
      onClick: () => run(true),
      title: "cousin-runner --check-auth --validate: spends one smallest model turn on a throwaway client"
    },
    "validate · spends one model turn"
  )), op && (op.kind === "check-auth" || op.kind === "validate") && /* @__PURE__ */ React.createElement(AccountOpStages, { op }), err && /* @__PURE__ */ React.createElement("div", { style: { ...ACCOUNT_MONO, color: "var(--red)" } }, err)));
}
registerView({
  id: "accounts",
  label: "Accounts",
  icon: I.host,
  order: 50,
  render: (props) => /* @__PURE__ */ React.createElement(AccountsView, { ...props })
});
registerSlot("inspector.panels", {
  id: "account",
  order: 20,
  render: ({ cousin }) => /* @__PURE__ */ React.createElement(CousinAccountPanel, { cousin })
});
Object.assign(window, {
  AccountsView,
  AccountFlow,
  AccountLogin,
  AccountForm,
  AccountRemove,
  AccountStatusCell,
  CousinAccountPanel,
  useAccountOp,
  accountEntryFrom
});
