function agentLaneReads(lane, key, laneKeys) {
  const keys = laneKeys || {};
  if (!lane || !Object.prototype.hasOwnProperty.call(keys, lane)) return key === "model" || key === "effort";
  return (keys[lane] || []).includes(key);
}
function agentSame(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
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
    if (want === null) {
      if (row.set) out[key] = null;
      continue;
    }
    if (!agentSame(want, row.value)) out[key] = want;
  }
  return out;
}
function agentEnvList(text) {
  return String(text || "").split(/[\s,]+/).filter(Boolean);
}
function agentEnvProblem(name, denyPrefixes) {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return name + " is not a variable name";
  const hit = (denyPrefixes || []).find((p) => name.startsWith(p));
  if (hit) return name + ": " + hit + "* never reaches the pane (the hard deny)";
  return null;
}
function agentModelProblem(rule, value, row) {
  if (!value) return null;
  if (/\s/.test(value)) return "one word, no spaces";
  if (rule && rule.provider_model && !/^[^/]+\/.+/.test(value)) return '"<provider>/<model>"';
  const listed = row && row.suggestions_from === "account" && row.suggestions || [];
  if (listed.length && !listed.includes(value)) return "not in the account's model list";
  return null;
}
function agentOpSettled(op, id) {
  return !!(op && id && op.id === id && op.status !== "running");
}
function cousinSettingChanges(fields, draft) {
  const out = {};
  for (const name of Object.keys(draft || {})) {
    const row = (fields || {})[name];
    if (!row) continue;
    const want = draft[name];
    if (want === null) {
      if (row.set) out[name] = null;
      continue;
    }
    if (!agentSame(want, row.value)) out[name] = want;
  }
  return out;
}
function cousinRepoToggle(choices, chosen, repo, on) {
  return (choices || []).filter((r) => r === repo ? on : (chosen || []).includes(r));
}
function flipAtProblem(value) {
  if (value === "" || value === "never") return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(value);
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return '"HH:MM" or "never"';
  return null;
}
const agentSmall = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const agentMono = { fontFamily: "var(--mono)", fontSize: 11 };
const agentHint = { fontSize: 10, color: "var(--fg-3)", lineHeight: 1.5 };
const agentErr = { fontFamily: "var(--mono)", fontSize: 10, color: "var(--red)", whiteSpace: "pre-wrap" };
function openKindSwitch(slug) {
  window.dispatchEvent(new CustomEvent("fw-open-kind-switch", { detail: { slug } }));
}
function AgentRestartOffer({ cousin, note }) {
  const [confirm, setConfirm] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  React.useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(false), 4e3);
    return () => clearTimeout(t);
  }, [confirm]);
  const restart = async () => {
    if (!confirm) {
      setConfirm(true);
      return;
    }
    setConfirm(false);
    setBusy(true);
    setMsg(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/restart`);
      if (!r.ok || d.ok === false) throw new Error(d.error || d.start && d.start.error || `HTTP ${r.status}`);
      setMsg({ ok: true, text: r.status === 202 ? "restarting: the runner stops, then starts again" : "restarted" });
    } catch (e) {
      setMsg({ ok: false, text: "restart failed: " + String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };
  return /* @__PURE__ */ React.createElement("div", { "data-agent-restart": true, style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(Pill, { tone: "amber" }, "saved"), /* @__PURE__ */ React.createElement("span", { style: agentHint }, note || "restart to apply: the running cousin keeps what it started with"), cousin.status === "running" && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: agentSmall,
      disabled: busy,
      onClick: restart,
      title: "POST /api/cousins/<slug>/restart: stop, then start on the new settings"
    },
    busy ? "restarting..." : confirm ? "click again to restart" : "restart now"
  ), cousin.status !== "running" && /* @__PURE__ */ React.createElement("span", { style: agentHint }, "the cousin is stopped: its next start uses it"), msg && /* @__PURE__ */ React.createElement("span", { style: { ...agentErr, color: msg.ok ? "var(--green)" : "var(--red)" } }, msg.text));
}
function AgentEnvList({ value, onChange, disabled, denyPrefixes }) {
  const [draft, setDraft] = React.useState("");
  const [err, setErr] = React.useState(null);
  const items = value || [];
  const add = () => {
    const names = agentEnvList(draft);
    for (const n of names) {
      const why = agentEnvProblem(n, denyPrefixes);
      if (why) {
        setErr(why);
        return;
      }
    }
    setErr(null);
    setDraft("");
    onChange(items.concat(names.filter((n) => !items.includes(n))));
  };
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4, flexWrap: "wrap" } }, items.map((n) => /* @__PURE__ */ React.createElement("span", { key: n, className: "pill gray", style: { display: "inline-flex", gap: 4, alignItems: "center" } }, n, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: { ...agentSmall, padding: "0 4px" },
      disabled,
      onClick: () => onChange(items.filter((x) => x !== n))
    },
    "×"
  ))), !items.length && /* @__PURE__ */ React.createElement("span", { style: agentHint }, "none")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4 } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft,
      disabled,
      placeholder: "NAME",
      style: { width: 160 },
      onChange: (e) => setDraft(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          add();
        }
      }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: agentSmall, disabled: disabled || !draft.trim(), onClick: add }, "add")), err && /* @__PURE__ */ React.createElement("span", { style: agentErr }, err));
}
function AgentField({ name, row, rule, value, onChange, disabled }) {
  const listId = `agent-${name}-suggestions`;
  if (row.readonly) {
    return /* @__PURE__ */ React.createElement("span", { style: agentMono }, row.value == null || row.value === "" ? "-" : String(row.value));
  }
  switch (row.type) {
    case "bool":
      return /* @__PURE__ */ React.createElement("label", { style: { display: "inline-flex", gap: 6, alignItems: "center" } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: !!value, disabled, onChange: (e) => onChange(e.target.checked) }), /* @__PURE__ */ React.createElement("span", { style: agentMono }, value ? "true" : "false"));
    case "percent":
      return /* @__PURE__ */ React.createElement(
        "input",
        {
          className: "txt",
          type: "number",
          min: row.min,
          max: row.max,
          step: "1",
          disabled,
          value: value ?? "",
          style: { width: 90 },
          onChange: (e) => onChange(e.target.value === "" ? null : Number(e.target.value))
        }
      );
    case "usd":
      return /* @__PURE__ */ React.createElement(
        "input",
        {
          className: "txt",
          type: "number",
          min: "0",
          step: "0.01",
          disabled,
          value: value ?? "",
          style: { width: 90 },
          onChange: (e) => onChange(e.target.value === "" ? null : Number(e.target.value))
        }
      );
    case "effort":
    case "choice":
    case "account": {
      const choices = row.choices || [];
      return /* @__PURE__ */ React.createElement("select", { className: "sel", value: value ?? "", disabled, onChange: (e) => onChange(e.target.value) }, value != null && !choices.includes(value) && /* @__PURE__ */ React.createElement("option", { value }, value, " (not on this lane)"), value == null && /* @__PURE__ */ React.createElement("option", { value: "" }, row.type === "effort" ? "the runner's default" : "-"), choices.map((ch) => /* @__PURE__ */ React.createElement("option", { key: ch, value: ch }, ch)));
    }
    case "model": {
      const problem = agentModelProblem(rule, value || "", row);
      return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 2 } }, /* @__PURE__ */ React.createElement(
        "input",
        {
          className: "txt",
          value: value ?? "",
          disabled,
          list: listId,
          placeholder: rule && rule.placeholder || "",
          style: { width: "100%", fontFamily: "var(--mono)" },
          onChange: (e) => onChange(e.target.value === "" ? null : e.target.value)
        }
      ), (row.suggestions || []).length > 0 && /* @__PURE__ */ React.createElement("datalist", { id: listId }, row.suggestions.map((m) => /* @__PURE__ */ React.createElement("option", { key: m, value: m }))), problem && /* @__PURE__ */ React.createElement("span", { style: agentHint }, problem));
    }
    case "sessions": {
      const modes = value || {};
      return /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 8px", alignItems: "center" } }, (row.kinds || []).map((kind) => {
        const fixed = (row.always_primary || []).includes(kind);
        return /* @__PURE__ */ React.createElement(React.Fragment, { key: kind }, /* @__PURE__ */ React.createElement("span", { style: agentMono }, kind), /* @__PURE__ */ React.createElement(
          "select",
          {
            className: "sel",
            value: modes[kind] || "primary",
            disabled: disabled || fixed,
            title: fixed ? `${kind} threads belong to the primary session, which holds the generation` : void 0,
            onChange: (e) => onChange(Object.assign({}, modes, { [kind]: e.target.value }))
          },
          (row.choices || []).map((m) => /* @__PURE__ */ React.createElement("option", { key: m, value: m }, m, fixed ? " (always)" : ""))
        ));
      }));
    }
    case "time":
      return /* @__PURE__ */ React.createElement(
        "input",
        {
          className: "txt",
          type: "time",
          value: value ?? "",
          disabled,
          style: { width: 110 },
          onChange: (e) => onChange(e.target.value === "" ? null : e.target.value)
        }
      );
    case "env_list":
      return /* @__PURE__ */ React.createElement(AgentEnvList, { value, onChange, disabled, denyPrefixes: row.deny_prefixes });
    default:
      return /* @__PURE__ */ React.createElement(
        "input",
        {
          className: "txt",
          value: value ?? "",
          disabled,
          style: { width: "100%" },
          onChange: (e) => onChange(e.target.value === "" ? null : e.target.value)
        }
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
  const [waitOp, setWaitOp] = React.useState(null);
  const [op] = useLongOp(c.remote ? null : c.slug);
  const load = React.useCallback(async () => {
    const d = await apiGet(`/api/cousins/${c.slug}/agent`);
    if (!d) {
      setLoadErr("could not read the agent settings");
      return;
    }
    setLoadErr(null);
    setData(d);
  }, [c.slug]);
  React.useEffect(() => {
    setData(null);
    setDraft({});
    setErrors({});
    setMsg(null);
    setSaved(false);
    setWaitOp(null);
    setBusy(false);
    if (!c.remote) load();
  }, [c.slug, load]);
  React.useEffect(() => {
    if (!c.remote && data) load();
  }, [c.lane, c.held, c.effort, c.model, c.account, c.autoStart]);
  const settle = React.useCallback((done) => {
    setWaitOp(null);
    setBusy(false);
    if (done.status === "done") {
      const r = done.result || {};
      setDraft({});
      setSaved(!!r.restart_required);
      setMsg(r.note ? { ok: true, text: r.note } : r.changed && !r.changed.length ? { ok: true, text: "nothing changed" } : null);
    } else {
      const r = done.result || {};
      setErrors(r.errors || { changes: done.error });
    }
    load();
  }, [load]);
  React.useEffect(() => {
    if (agentOpSettled(op, waitOp)) settle(op);
  }, [op, waitOp, settle]);
  React.useEffect(() => {
    if (!waitOp) return;
    const t = setInterval(async () => {
      const d = await apiGet(`/api/cousins/${c.slug}/op`);
      if (d && agentOpSettled(d.op, waitOp)) settle(d.op);
    }, 3e3);
    return () => clearInterval(t);
  }, [waitOp, c.slug, settle]);
  if (c.remote) return null;
  if (!data) return /* @__PURE__ */ React.createElement("div", { style: { ...agentMono, color: "var(--fg-3)" } }, loadErr || "loading agent settings...");
  const s = data.settings || {};
  const legacy = data.lane === data.tmux_lane;
  const valueOf = (key) => key in draft ? draft[key] : (s[key] || {}).value;
  const set = (key, v) => {
    setDraft((d) => Object.assign({}, d, { [key]: v }));
    setSaved(false);
    setMsg(null);
  };
  const changes = agentChanges(s, draft);
  const dirty = Object.keys(changes).length > 0;
  const turns = data.model_change_spends_turn && typeof changes.model === "string" && changes.model;
  const save = async () => {
    if (!dirty || busy) return;
    setBusy(true);
    setErrors({});
    setMsg(null);
    setSaved(false);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${c.slug}/agent`, { changes });
      if (r.status === 202 && d.op) {
        setWaitOp(d.op.id);
        return;
      }
      if (r.status === 400 && d.errors) {
        setErrors(d.errors);
        setBusy(false);
        return;
      }
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setDraft({});
      if (d.agent) setData(Object.assign({ slug: c.slug }, d.agent));
      setSaved(!!d.restart_required);
      if (d.note) setMsg({ ok: true, text: d.note });
      else if (!d.changed.length) setMsg({ ok: true, text: "nothing changed" });
      setBusy(false);
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
      setBusy(false);
      load();
    }
  };
  const order = Object.keys(s).filter((k) => k !== "runner" && k !== "api_key_file" && k !== "commit_attribution");
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "agent"), /* @__PURE__ */ React.createElement("div", { "data-agent-settings": true, style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: agentHint }, "kind"), /* @__PURE__ */ React.createElement(Pill, { tone: legacy ? "gray" : "cyan" }, data.lane), data.held && /* @__PURE__ */ React.createElement("span", { title: "a stop wrote run/held: the supervisor keeps the runner down until its next start" }, /* @__PURE__ */ React.createElement(Pill, { tone: "amber" }, "held")), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: agentSmall,
      onClick: () => openKindSwitch(c.slug),
      title: "switch this cousin to another kind (a migration)"
    },
    "switch kind"
  )), legacy && /* @__PURE__ */ React.createElement("span", { style: agentHint }, "the ", data.lane, " lane has no [agent] settings: its model, effort and auth mode are the identity rows above ([runtime]). Switch kind to run it on a runner."), s.api_key_file && /* @__PURE__ */ React.createElement("div", { style: { ...agentMono, fontSize: 10, color: "var(--amber)" }, "data-api-key-file": true }, "[agent] api_key_file is deprecated: move the key to an account (config/accounts.toml)"), !legacy && /* @__PURE__ */ React.createElement("dl", { className: "kv", style: { gridTemplateColumns: "110px 1fr" } }, order.map((key) => {
    const row = s[key];
    const err = errors[key] || (data.errors || {})[key];
    return /* @__PURE__ */ React.createElement(React.Fragment, { key }, /* @__PURE__ */ React.createElement("dt", { title: row.hint }, key.replace(/_/g, " ")), /* @__PURE__ */ React.createElement("dd", { style: { display: "flex", flexDirection: "column", gap: 3 } }, /* @__PURE__ */ React.createElement(
      AgentField,
      {
        name: key,
        row,
        rule: data.model_rule,
        value: valueOf(key),
        onChange: (v) => set(key, v),
        disabled: busy
      }
    ), key === "env_allow" && row.base && /* @__PURE__ */ React.createElement("span", { style: agentHint }, "always passed: ", row.base.join(" "), "; never passed (the hard deny, it wins):", " ", (row.deny_prefixes || []).map((p) => p + "*").join(" "), " and credential names"), key === "auto_start" && /* @__PURE__ */ React.createElement("span", { style: agentHint }, "the supervisor starts it with itself; false keeps it down until started"), row.hint && key !== "auto_start" && /* @__PURE__ */ React.createElement("span", { style: agentHint }, row.hint), row.set && !row.readonly && !(key in draft && draft[key] === null) && key !== "sessions" && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        style: { ...agentSmall, alignSelf: "flex-start" },
        disabled: busy,
        onClick: () => set(key, null),
        title: "remove the key from cousin.toml: the default applies"
      },
      "unset (default ",
      row.default == null || Array.isArray(row.default) && !row.default.length ? "none" : String(row.default),
      ")"
    ), err && /* @__PURE__ */ React.createElement("span", { style: agentErr }, err)));
  })), !legacy && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: agentSmall, disabled: !dirty || busy, onClick: save }, busy ? waitOp ? "validating..." : "saving..." : turns ? "save · validates the model (one turn)" : "save"), dirty && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: agentSmall,
      disabled: busy,
      onClick: () => {
        setDraft({});
        setErrors({});
      }
    },
    "discard"
  ), turns && /* @__PURE__ */ React.createElement("span", { style: agentHint }, "a new model on this kind is checked with one smallest turn on the account being written, before it is written")), errors.changes && /* @__PURE__ */ React.createElement("span", { style: agentErr }, errors.changes), waitOp && /* @__PURE__ */ React.createElement(LongOpStatus, { slug: c.slug, kind: "agent-settings" }), msg && /* @__PURE__ */ React.createElement("span", { style: { ...agentErr, color: msg.ok ? "var(--fg-2)" : "var(--red)" } }, msg.text), saved && /* @__PURE__ */ React.createElement(AgentRestartOffer, { cousin: c })));
}
const COUSIN_SETTING_LABELS = {
  "cousin.name": "name",
  "cousin.peer_visible": "peer visible",
  "memory.proactive_recall": "recall lines",
  "memory.recall_keyword_only": "keyword recall",
  "memory.review_batch": "review batch",
  "memory.review_model": "review model",
  "lifecycle.flip_at": "flip at",
  "agent.commit_attribution": "commit attribution",
  "skills.repos": "skill repos"
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
  React.useEffect(() => {
    setData(null);
    setDraft({});
    setErrors({});
    setSaved(null);
    setMsg(null);
    if (!c.remote) load();
  }, [c.slug, load]);
  if (c.remote || !data) return null;
  const f = data.fields || {};
  const valueOf = (n) => n in draft ? draft[n] : (f[n] || {}).value;
  const set = (n, v) => {
    setDraft((d) => Object.assign({}, d, { [n]: v }));
    setSaved(null);
    setMsg(null);
  };
  const changes = cousinSettingChanges(f, draft);
  const dirty = Object.keys(changes).length > 0;
  const flipDraft = valueOf("lifecycle.flip_at") ?? "";
  const flipBad = flipAtProblem(flipDraft);
  const save = async () => {
    if (!dirty || busy || flipBad) return;
    setBusy(true);
    setErrors({});
    setMsg(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${c.slug}/settings`, { changes });
      if (r.status === 400 && d.errors) {
        setErrors(d.errors);
        return;
      }
      if (d.written) {
        setDraft({});
        load();
        setErrors({ changes: d.error });
        return;
      }
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
      return /* @__PURE__ */ React.createElement(
        "select",
        {
          className: "sel",
          value: sel,
          disabled: busy,
          onChange: (e) => set(n, e.target.value === "" ? null : e.target.value === "on")
        },
        /* @__PURE__ */ React.createElement("option", { value: "" }, "install default (", install.value === false ? "off" : install.value === true ? "on" : "unreadable", ")"),
        /* @__PURE__ */ React.createElement("option", { value: "on" }, "on: the harness adds its attribution"),
        /* @__PURE__ */ React.createElement("option", { value: "off" }, 'off: no Co-Authored-By, no "Generated with"')
      );
    }
    if (row.type === "bool") {
      return /* @__PURE__ */ React.createElement("label", { style: { display: "inline-flex", gap: 6, alignItems: "center" } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: !!v, disabled: busy, onChange: (e) => set(n, e.target.checked) }), /* @__PURE__ */ React.createElement("span", { style: agentMono }, v ? "true" : "false"));
    }
    if (row.type === "count") {
      return /* @__PURE__ */ React.createElement(
        "input",
        {
          className: "txt",
          type: "number",
          min: "0",
          step: "1",
          value: v ?? "",
          disabled: busy,
          style: { width: 90 },
          onChange: (e) => set(n, e.target.value === "" ? null : Number(e.target.value))
        }
      );
    }
    if (row.type === "repos") {
      const reset = n in draft ? draft[n] === null : !row.set;
      const chosen = Array.isArray(v) ? v : row.default || [];
      return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" } }, (row.choices || []).map((r) => /* @__PURE__ */ React.createElement("label", { key: r, style: { display: "inline-flex", gap: 4, alignItems: "center" } }, /* @__PURE__ */ React.createElement(
        "input",
        {
          type: "checkbox",
          checked: chosen.includes(r),
          disabled: busy,
          onChange: (e) => set(n, cousinRepoToggle(row.choices, chosen, r, e.target.checked))
        }
      ), /* @__PURE__ */ React.createElement("span", { style: agentMono }, r))), reset ? /* @__PURE__ */ React.createElement("span", { style: agentHint }, "default") : /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: agentSmall, disabled: busy, onClick: () => set(n, null) }, "use the default"));
    }
    return /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "txt",
        value: v ?? "",
        disabled: busy,
        style: { width: "100%", fontFamily: "var(--mono)" },
        placeholder: n === "lifecycle.flip_at" ? `install default (${row.install || "never"})` : n === "memory.review_model" ? "the cousin's own model" : "",
        onChange: (e) => set(n, e.target.value === "" && n !== "cousin.name" ? null : e.target.value)
      }
    );
  };
  const ro = data.readonly || {};
  const roText = (v) => Array.isArray(v) ? v.length ? v.map((h) => typeof h === "string" ? h : h.name || h.cmd || JSON.stringify(h)).join("; ") : "none" : v == null || v === "" ? "-" : String(v);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "cousin settings"), /* @__PURE__ */ React.createElement("div", { "data-cousin-settings": true, style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("dl", { className: "kv", style: { gridTemplateColumns: "110px 1fr" } }, Object.keys(f).map((n) => /* @__PURE__ */ React.createElement(React.Fragment, { key: n }, /* @__PURE__ */ React.createElement("dt", { title: `cousin.toml [${n.split(".")[0]}] ${n.split(".")[1]}` }, COUSIN_SETTING_LABELS[n] || n), /* @__PURE__ */ React.createElement("dd", { style: { display: "flex", flexDirection: "column", gap: 3 } }, field(n), n === "lifecycle.flip_at" && /* @__PURE__ */ React.createElement("span", { style: agentHint }, "effective: ", f[n].effective || "never", flipBad ? ` · ${flipBad}` : ""), n === "agent.commit_attribution" && /* @__PURE__ */ React.createElement("span", { style: agentHint }, "effective: ", attribution.effective == null ? "unknown" : attribution.effective ? "on" : "off", " · install default ", install.value == null ? "unknown" : String(install.value), " from ", install.source), /* @__PURE__ */ React.createElement("span", { style: agentHint }, f[n].hint, f[n].restart ? "" : " · applies without a restart"), (errors[n] || (data.errors || {})[n]) && /* @__PURE__ */ React.createElement("span", { style: agentErr }, errors[n] || data.errors[n])))), Object.keys(ro).map((n) => /* @__PURE__ */ React.createElement(React.Fragment, { key: n }, /* @__PURE__ */ React.createElement("dt", { title: (data.readonly_why || {})[n] }, n.replace(".", " ").replace(/_/g, " ")), /* @__PURE__ */ React.createElement("dd", { style: { color: "var(--fg-2)" }, title: (data.readonly_why || {})[n] }, roText(ro[n]), " ", /* @__PURE__ */ React.createElement("span", { style: agentHint }, "read-only"))))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: agentSmall, disabled: !dirty || busy || !!flipBad, onClick: save }, busy ? "saving..." : "save"), dirty && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: agentSmall,
      disabled: busy,
      onClick: () => {
        setDraft({});
        setErrors({});
      }
    },
    "discard"
  ), msg && /* @__PURE__ */ React.createElement("span", { style: agentHint }, msg)), errors.changes && /* @__PURE__ */ React.createElement("span", { style: agentErr }, errors.changes), saved && saved.note && /* @__PURE__ */ React.createElement("span", { style: agentHint }, saved.note), saved && (saved.restart ? /* @__PURE__ */ React.createElement(AgentRestartOffer, { cousin: c }) : /* @__PURE__ */ React.createElement("span", { style: agentHint }, /* @__PURE__ */ React.createElement(Pill, { tone: "green" }, "saved"), " applies without a restart"))));
}
registerSlot("inspector.lane", {
  id: "agent",
  order: 10,
  render: ({ cousin }) => /* @__PURE__ */ React.createElement(AgentSettingsPanel, { cousin })
});
registerSlot("inspector.lane", {
  id: "cousin-settings",
  order: 11,
  render: ({ cousin }) => /* @__PURE__ */ React.createElement(CousinSettingsPanel, { cousin })
});
Object.assign(window, {
  AgentSettingsPanel,
  CousinSettingsPanel,
  AgentRestartOffer,
  AgentField,
  agentLaneReads,
  agentChanges,
  cousinSettingChanges,
  cousinRepoToggle,
  agentOpSettled,
  openKindSwitch
});
