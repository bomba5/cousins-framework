const MCP_HANDOFF_TOOL = "mcp__cousin__handoff";
function mcpNamesTool(entry, tool) {
  entry = String(entry || "");
  return entry === tool || entry.endsWith("*") && tool.startsWith(entry.slice(0, -1));
}
function policyRemovals(before, after) {
  const out = { any: false };
  for (const key of ["deny_tools", "deny_bash_patterns", "ask"]) {
    const next = new Set(after && after[key] || []);
    out[key] = (before && before[key] || []).filter((v) => !next.has(v));
    if (out[key].length) out.any = true;
  }
  out.outbound_filter = !!(before && before.outbound_filter !== false && after && after.outbound_filter === false);
  if (out.outbound_filter) out.any = true;
  return out;
}
function jsRegexProblem(pattern) {
  try {
    new RegExp(pattern, "u");
    return null;
  } catch (e) {
    return String(e.message || e);
  }
}
function mcpServerDraft(s) {
  const d = {
    _id: "file:" + s.name,
    name: s.name,
    type: s.type,
    ignored_keys: s.ignored_keys || [],
    masked: s.masked || [],
    account_vars: s.account_vars || [],
    unset_vars: s.unset_vars || []
  };
  if (s.type === "stdio") {
    d.command = s.command_masked ? null : s.command || "";
    d.args = (s.args || []).map((a) => a && typeof a === "object" ? a.value : a);
    d.env = (s.env || []).map((e) => ({ name: e.name, value: e.value }));
  } else {
    d.url = s.url_masked ? null : s.url || "";
    d.headers = (s.headers || []).map((h) => ({ name: h.name, value: h.value }));
  }
  return d;
}
function mcpServerBody(d) {
  const out = { name: String(d.name || "").trim(), type: d.type };
  if (d.type === "stdio") {
    out.command = d.command;
    out.args = (d.args || []).slice();
    out.env = (d.env || []).filter((e) => e.name !== "" || e.value !== "");
  } else {
    out.url = d.url;
    out.headers = (d.headers || []).filter((h) => h.name !== "" || h.value !== "");
  }
  return out;
}
function mcpApplySuggestion(d, p) {
  if (!p || !p.suggest || p.server !== d.name) return d;
  const next = JSON.parse(JSON.stringify(d));
  if ((p.field === "env" || p.field === "headers") && next[p.field]) {
    next[p.field] = next[p.field].map((e) => e.name === p.key ? { name: e.name, value: p.suggest } : e);
  } else if (p.field === "args" && typeof p.key === "number" && next.args) {
    next.args[p.key] = p.suggest;
  } else if (p.field === "url" || p.field === "command") {
    next[p.field] = p.suggest;
  } else {
    return d;
  }
  return next;
}
const mcpSmall = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const mcpMono = { fontFamily: "var(--mono)", fontSize: 11 };
const mcpHint = { fontSize: 10, color: "var(--fg-3)", lineHeight: 1.5 };
const mcpBox = {
  border: "1px solid var(--line)",
  borderRadius: 3,
  background: "var(--bg-0)",
  padding: 8,
  display: "flex",
  flexDirection: "column",
  gap: 6
};
function McpMsg({ msg }) {
  if (!msg) return null;
  return /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: msg.ok ? "var(--green)" : "var(--red)", whiteSpace: "pre-wrap" } }, msg.text);
}
function RestartOffer({ cousin, note }) {
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
  return /* @__PURE__ */ React.createElement("div", { "data-restart-offer": true, style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(Pill, { tone: "amber" }, "saved"), /* @__PURE__ */ React.createElement("span", { style: mcpHint }, note || "applies at the next start"), cousin && cousin.status === "running" && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: mcpSmall,
      disabled: busy,
      onClick: restart,
      title: "POST /api/cousins/<slug>/restart: stop, then start on the new settings"
    },
    busy ? "restarting..." : confirm ? "click again to restart" : "restart now"
  ), cousin && cousin.status !== "running" && /* @__PURE__ */ React.createElement("span", { style: mcpHint }, "the cousin is stopped: its next start uses it"), /* @__PURE__ */ React.createElement(McpMsg, { msg }));
}
function TypedConfirm({ word, label, busy, onConfirm, onCancel, children }) {
  const [typed, setTyped] = React.useState("");
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, children && /* @__PURE__ */ React.createElement("div", { style: { ...mcpHint, color: "var(--amber)", flexBasis: "100%" } }, children), /* @__PURE__ */ React.createElement("span", { style: mcpHint }, "type ", /* @__PURE__ */ React.createElement("code", null, word), " to confirm"), /* @__PURE__ */ React.createElement("input", { className: "txt", value: typed, onChange: (e) => setTyped(e.target.value), style: { width: 90 }, autoFocus: true }), /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: mcpSmall, disabled: busy || typed !== word, onClick: onConfirm }, label), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: mcpSmall, onClick: onCancel }, "cancel"));
}
function useMcpResource(url) {
  const [data, setData] = React.useState(null);
  const [loadErr, setLoadErr] = React.useState(null);
  const load = React.useCallback(async () => {
    try {
      const r = await fetch(url, { cache: "no-store" });
      const d = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
      if (!r.ok) {
        setLoadErr(d.error || `HTTP ${r.status}`);
        return null;
      }
      setData(d);
      setLoadErr(null);
      return d;
    } catch (e) {
      setLoadErr(String(e.message || e));
      return null;
    }
  }, [url]);
  React.useEffect(() => {
    setData(null);
    load();
  }, [load]);
  return [data, setData, load, loadErr];
}
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
    (data.tools || []).forEach((x) => {
      t[x.name] = x.enabled;
    });
    setTools(t);
  }, [data]);
  const copyPath = scope === "install" ? "/copy-example" : "/copy-default";
  const copyLabel = scope === "install" ? "copy from example" : "copy the install default here";
  const post = async (path, body) => {
    setBusy(true);
    setMsg(null);
    setStale(false);
    try {
      const { r, d } = await apiSend("POST", url + path, body);
      if (r.status === 409 && d.stale) setStale(true);
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setData(d);
      setSaved(true);
      return d;
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
      return null;
    } finally {
      setBusy(false);
    }
  };
  if (!data) {
    return /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-3)" } }, loadErr ? `registry unavailable: ${loadErr}` : "loading...");
  }
  const limits = data.limits || {};
  const changed = {};
  for (const k of ["ceiling", "timeout", "max_output"]) {
    if (numbers[k] !== void 0 && Number(numbers[k]) !== data[k]) changed[k] = Number(numbers[k]);
  }
  const toolChanges = {};
  (data.tools || []).forEach((t) => {
    if (tools[t.name] !== void 0 && tools[t.name] !== t.enabled) toolChanges[t.name] = tools[t.name];
  });
  const dirty = Object.keys(changed).length > 0 || Object.keys(toolChanges).length > 0;
  const enabledCount = Object.values(tools).filter(Boolean).length;
  const ceiling = Number(numbers.ceiling ?? data.ceiling);
  const badNumber = Object.entries(changed).find(([k, v]) => {
    const [lo, hi] = limits[k] || [0, Infinity];
    return !Number.isInteger(v) || v < lo || v > hi;
  });
  const save = () => post("", Object.assign({ etag: data.etag }, changed, Object.keys(toolChanges).length ? { tools: toolChanges } : {}));
  const sourceLabel = { own: "the cousin's own", install: "the install default", example: "the shipped example", shipped: "the shipped example" }[data.source] || data.source;
  return /* @__PURE__ */ React.createElement("div", { "data-mcp-registry": scope, style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)" } }, scope === "install" ? "config/" : "", data.file), data.exists ? /* @__PURE__ */ React.createElement(Pill, { tone: "green" }, "present") : /* @__PURE__ */ React.createElement(Pill, { tone: "gray" }, "absent"), !data.exists && data.shown && /* @__PURE__ */ React.createElement("span", { style: mcpHint }, "showing ", sourceLabel, ": ", data.shown), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: mcpSmall, disabled: busy, onClick: () => {
    setMsg(null);
    setStale(false);
    load();
  } }, "reload")), data.error && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--red)" } }, data.error), (data.skipped || []).map((s) => /* @__PURE__ */ React.createElement("div", { key: s.name, style: { ...mcpMono, fontSize: 10, color: "var(--amber)" } }, "cousin-mcp skips ", s.name, ": ", s.reason)), !data.exists && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn primary",
      style: mcpSmall,
      disabled: busy || scope === "install" && !data.example_exists,
      onClick: () => post(copyPath, {})
    },
    copyLabel
  ), /* @__PURE__ */ React.createElement("span", { style: mcpHint }, scope === "install" ? "The example is never edited in place: copy it to config/mcp-registry.toml, then edit the copy. Cousins without a registry of their own read it." : "This cousin has no registry of its own and reads the one shown. A copy here is its own to edit (the operator is filled in, as spawn does).")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" } }, ["ceiling", "timeout", "max_output"].map((k) => /* @__PURE__ */ React.createElement("label", { key: k, style: { ...mcpMono, display: "flex", gap: 4, alignItems: "center", color: "var(--fg-2)" } }, k, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      type: "number",
      disabled: !data.exists || busy,
      min: (limits[k] || [])[0],
      max: (limits[k] || [])[1],
      value: numbers[k] ?? "",
      style: { width: k === "max_output" ? 90 : 64 },
      onChange: (e) => setNumbers(Object.assign({}, numbers, { [k]: e.target.value === "" ? "" : Number(e.target.value) }))
    }
  )))), /* @__PURE__ */ React.createElement("div", { style: mcpHint }, "ceiling: most enabled tools allowed · timeout: seconds per call · max_output: characters returned per call"), /* @__PURE__ */ React.createElement("div", { style: mcpBox }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: enabledCount > ceiling ? "var(--red)" : "var(--fg-3)" } }, enabledCount, " of ", ceiling, " enabled"), (data.tools || []).length === 0 && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-3)" } }, "no tools"), (data.tools || []).map((t) => /* @__PURE__ */ React.createElement("label", { key: t.name, style: {
    ...mcpMono,
    display: "flex",
    gap: 8,
    alignItems: "baseline",
    flexWrap: "wrap",
    opacity: t.editable ? 1 : 0.6
  } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "checkbox",
      checked: !!tools[t.name],
      disabled: !data.exists || busy || !t.editable,
      onChange: (e) => setTools(Object.assign({}, tools, { [t.name]: e.target.checked }))
    }
  ), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)" } }, t.name), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, t.kind, t.commands.length ? ` · ${t.commands.join(", ")}` : ""), t.description && /* @__PURE__ */ React.createElement("span", { style: { ...mcpHint, flexBasis: "100%", paddingLeft: 22 } }, t.description)))), /* @__PURE__ */ React.createElement("div", { style: mcpHint }, "Adding or changing a tool's commands is a file edit: a registry tool runs a command, so it is not built here."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn primary",
      style: mcpSmall,
      disabled: !data.exists || busy || !dirty || !!badNumber || enabledCount > ceiling,
      onClick: save
    },
    "save"
  ), badNumber && /* @__PURE__ */ React.createElement("span", { style: { ...mcpHint, color: "var(--amber)" } }, badNumber[0], " must be ", (limits[badNumber[0]] || []).join(" to ")), enabledCount > ceiling && /* @__PURE__ */ React.createElement("span", { style: { ...mcpHint, color: "var(--amber)" } }, "more tools enabled than the ceiling"), data.exists && !replacing && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: mcpSmall,
      disabled: busy,
      onClick: () => setReplacing(true),
      title: scope === "install" ? "overwrite config/mcp-registry.toml with the example" : "overwrite this registry with the install default"
    },
    "replace with the ",
    scope === "install" ? "example" : "default"
  )), replacing && /* @__PURE__ */ React.createElement(
    TypedConfirm,
    {
      word: "replace",
      label: "replace",
      busy,
      onCancel: () => setReplacing(false),
      onConfirm: async () => {
        const d = await post(copyPath, { replace: true, etag: data.etag });
        if (d) setReplacing(false);
      }
    }
  ), stale && /* @__PURE__ */ React.createElement("div", { style: { ...mcpHint, color: "var(--amber)" } }, "the file changed on disk since it was loaded: reload, then make the change again"), /* @__PURE__ */ React.createElement(McpMsg, { msg }), saved && (scope === "install" ? /* @__PURE__ */ React.createElement("div", { style: mcpHint }, "saved: each cousin that reads the install default picks it up at its next start") : /* @__PURE__ */ React.createElement(RestartOffer, { cousin, note: data.restart_note })));
}
function McpRows({ rows, onChange, namePlaceholder, valuePlaceholder, disabled, problems, field }) {
  const set = (i, patch) => onChange(rows.map((r, j) => j === i ? Object.assign({}, r, patch) : r));
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, rows.map((r, i) => {
    const p = (problems || []).find((x) => x.field === field && x.key === r.name);
    return /* @__PURE__ */ React.createElement("div", { key: i, style: { display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "txt",
        value: r.name,
        placeholder: namePlaceholder,
        disabled,
        style: { width: 130 },
        onChange: (e) => set(i, { name: e.target.value })
      }
    ), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "txt",
        value: r.value ?? "",
        disabled,
        placeholder: r.value === null ? "a literal secret is in the file: write ${NAME}" : valuePlaceholder,
        style: { flex: "1 1 160px", minWidth: 0 },
        onChange: (e) => set(i, { value: e.target.value })
      }
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        style: mcpSmall,
        disabled,
        onClick: () => onChange(rows.filter((_, j) => j !== i))
      },
      "×"
    ), r.value === null && !p && /* @__PURE__ */ React.createElement("span", { style: { ...mcpHint, color: "var(--amber)", flexBasis: "100%" } }, "masked: the file holds a literal secret here; a save needs a $", "{VAR}", " reference instead"));
  }), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: { ...mcpSmall, alignSelf: "flex-start" },
      disabled,
      onClick: () => onChange(rows.concat([{ name: "", value: "" }]))
    },
    "+ ",
    field === "env" ? "variable" : "header"
  ));
}
function McpServerCard({ draft, onChange, onRemove, problems, busy }) {
  const mine = (problems || []).filter((p) => p.server === draft.name);
  const [removing, setRemoving] = React.useState(false);
  React.useEffect(() => {
    if (!removing) return;
    const t = setTimeout(() => setRemoving(false), 4e3);
    return () => clearTimeout(t);
  }, [removing]);
  const set = (patch) => onChange(Object.assign({}, draft, patch));
  const field = (label, children) => /* @__PURE__ */ React.createElement("label", { style: { ...mcpMono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", color: "var(--fg-2)" } }, /* @__PURE__ */ React.createElement("span", { style: { width: 60 } }, label), children);
  return /* @__PURE__ */ React.createElement("div", { "data-mcp-server": draft.name, style: { ...mcpBox, borderColor: mine.length ? "var(--red)" : "var(--line)" } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft.name,
      placeholder: "server name",
      disabled: busy,
      style: { width: 140 },
      onChange: (e) => set({ name: e.target.value })
    }
  ), /* @__PURE__ */ React.createElement(
    "select",
    {
      className: "sel",
      value: draft.type,
      disabled: busy,
      onChange: (e) => {
        const type = e.target.value;
        set(type === "stdio" ? { type, command: draft.command || "", args: draft.args || [], env: draft.env || [] } : { type, url: draft.url || "", headers: draft.headers || [] });
      }
    },
    /* @__PURE__ */ React.createElement("option", { value: "stdio" }, "stdio"),
    /* @__PURE__ */ React.createElement("option", { value: "http" }, "http"),
    /* @__PURE__ */ React.createElement("option", { value: "sse" }, "sse")
  ), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn danger",
      style: mcpSmall,
      disabled: busy,
      onClick: () => {
        if (removing) {
          setRemoving(false);
          onRemove();
        } else setRemoving(true);
      },
      title: "drops this server from the list; nothing is written until save"
    },
    removing ? "click again to remove" : "remove"
  )), draft.type === "stdio" ? /* @__PURE__ */ React.createElement(React.Fragment, null, field("command", /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft.command ?? "",
      disabled: busy,
      placeholder: draft.command === null ? "masked secret in the command: write ${NAME}" : "/path/to/server",
      style: { flex: "1 1 200px", minWidth: 0 },
      onChange: (e) => set({ command: e.target.value })
    }
  )), field("args", /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4, flexWrap: "wrap", flex: "1 1 200px" } }, (draft.args || []).map((a, i) => /* @__PURE__ */ React.createElement("span", { key: i, style: { display: "inline-flex", gap: 2 } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: a ?? "",
      disabled: busy,
      style: { width: 120 },
      placeholder: a === null ? "masked secret: write ${NAME}" : "arg",
      onChange: (e) => set({ args: draft.args.map((x, j) => j === i ? e.target.value : x) })
    }
  ), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: mcpSmall,
      disabled: busy,
      onClick: () => set({ args: draft.args.filter((_, j) => j !== i) })
    },
    "×"
  ))), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: mcpSmall, disabled: busy, onClick: () => set({ args: (draft.args || []).concat([""]) }) }, "+ arg"))), /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--fg-3)" } }, "env"), /* @__PURE__ */ React.createElement(
    McpRows,
    {
      rows: draft.env || [],
      field: "env",
      disabled: busy,
      problems: mine,
      namePlaceholder: "NAME",
      valuePlaceholder: "value or ${VAR}",
      onChange: (env) => set({ env })
    }
  )) : /* @__PURE__ */ React.createElement(React.Fragment, null, field("url", /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft.url ?? "",
      disabled: busy,
      placeholder: draft.url === null ? "masked secret in the url: write ${NAME}" : "https://host/mcp",
      style: { flex: "1 1 200px", minWidth: 0 },
      onChange: (e) => set({ url: e.target.value })
    }
  )), /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--fg-3)" } }, "headers"), /* @__PURE__ */ React.createElement(
    McpRows,
    {
      rows: draft.headers || [],
      field: "headers",
      disabled: busy,
      problems: mine,
      namePlaceholder: "Header",
      valuePlaceholder: "value or Bearer ${VAR}",
      onChange: (headers) => set({ headers })
    }
  )), draft.ignored_keys && draft.ignored_keys.length > 0 && /* @__PURE__ */ React.createElement("div", { style: mcpHint }, "kept as written, not edited here: ", draft.ignored_keys.join(", "), " (the runner ignores these keys)"), draft.account_vars && draft.account_vars.length > 0 && /* @__PURE__ */ React.createElement("div", { style: { ...mcpHint, color: "var(--red)" } }, "names an account variable (", draft.account_vars.join(", "), "): the runner skips this server"), draft.unset_vars && draft.unset_vars.length > 0 && /* @__PURE__ */ React.createElement("div", { style: { ...mcpHint, color: "var(--amber)" } }, "not set in the console's environment: ", draft.unset_vars.join(", "), ". The runner skips a server whose $", "{VAR}", " has no default and is unset in its own environment."), mine.map((p, i) => /* @__PURE__ */ React.createElement("div", { key: i, style: { ...mcpMono, fontSize: 10, color: "var(--red)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", null, p.field, p.key !== null && p.key !== void 0 ? ` ${p.key}` : "", ": ", p.reason), p.suggest && /* @__PURE__ */ React.createElement("button", { className: "btn", style: mcpSmall, disabled: busy, onClick: () => onChange(mcpApplySuggestion(draft, p)) }, "use ", p.suggest))));
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
    setDrop([]);
    setProblems([]);
  }, [data]);
  const save = async (replaceBroken) => {
    setBusy(true);
    setMsg(null);
    setStale(false);
    setProblems([]);
    try {
      const body = { etag: data.etag, servers: drafts.map(mcpServerBody), drop };
      if (replaceBroken) body.replace_broken = true;
      const { r, d } = await apiSend("POST", url, body);
      if (r.status === 409 && d.stale) setStale(true);
      if (!r.ok) {
        if (d.problems) setProblems(d.problems);
        throw new Error(d.error || `HTTP ${r.status}`);
      }
      setData(d);
      setSaved(true);
      setReplacing(false);
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };
  if (!data) return /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-3)" } }, loadErr ? `servers unavailable: ${loadErr}` : "loading...");
  const ev = data.last_event;
  const tmux = data.lane === "tmux-legacy";
  return /* @__PURE__ */ React.createElement("div", { "data-mcp-servers": true, style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)" } }, data.file), data.exists ? /* @__PURE__ */ React.createElement(Pill, { tone: "green" }, "present") : /* @__PURE__ */ React.createElement(Pill, { tone: "gray" }, "absent"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: mcpSmall, disabled: busy, onClick: () => {
    setMsg(null);
    setStale(false);
    load();
  } }, "reload")), /* @__PURE__ */ React.createElement("div", { style: { ...mcpHint, borderLeft: "2px solid var(--amber)", paddingLeft: 8 } }, "Secrets: the runner hands these servers to the agent CLI in a private file, never on its command line, but this file is plain text the model can read and edit, and a stdio server's args are on its own command line, which any user on this host can read. Write a secret as ", /* @__PURE__ */ React.createElement("code", null, "$", "{NAME}"), " and set NAME in the runner's environment: the CLI fills it in, and only the name is in the file. A value that looks like a secret is refused, with the reference to write instead. Account variables are never passed to a server."), tmux && /* @__PURE__ */ React.createElement("div", { style: mcpHint }, "This cousin is on the tmux lane: Claude Code reads this file itself at session start, and a new server also needs the harness's approval for this home."), ev && /* @__PURE__ */ React.createElement("div", { style: { ...mcpBox, gap: 2 } }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--fg-3)" } }, "last runner start", ev.ts ? ` · ${fmtAgo(Date.now() / 1e3 - ev.ts)}` : ""), ((ev.payload || {}).servers || []).map((s) => /* @__PURE__ */ React.createElement("div", { key: s.name, style: { ...mcpMono, color: "var(--green)" } }, "loaded ", s.name, " (", s.type, ")", s.ignored_keys ? `, ignored ${s.ignored_keys.join(", ")}` : "")), ((ev.payload || {}).skipped || []).map((s, i) => /* @__PURE__ */ React.createElement("div", { key: i, style: { ...mcpMono, color: "var(--amber)" } }, "skipped ", s.name ?? data.file, ": ", s.reason))), data.parse_error && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--red)" } }, data.parse_error), drafts.map((d, i) => /* @__PURE__ */ React.createElement(
    McpServerCard,
    {
      key: d._id,
      draft: d,
      busy,
      problems,
      onChange: (next) => setDrafts(drafts.map((x, j) => j === i ? next : x)),
      onRemove: () => setDrafts(drafts.filter((_, j) => j !== i))
    }
  )), (data.kept || []).length > 0 && /* @__PURE__ */ React.createElement("div", { style: mcpBox }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--fg-3)" } }, "kept as they are (not edited here)"), data.kept.map((k) => /* @__PURE__ */ React.createElement("div", { key: k.name, style: {
    ...mcpMono,
    display: "flex",
    gap: 6,
    alignItems: "center",
    flexWrap: "wrap",
    textDecoration: drop.includes(k.name) ? "line-through" : "none"
  } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)" } }, k.name), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, k.reason), k.name !== data.reserved && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: mcpSmall,
      disabled: busy,
      onClick: () => setDrop(drop.includes(k.name) ? drop.filter((n) => n !== k.name) : drop.concat([k.name]))
    },
    drop.includes(k.name) ? "keep" : "drop"
  )))), problems.filter((p) => !drafts.some((d) => d.name === p.server)).map((p, i) => /* @__PURE__ */ React.createElement("div", { key: i, style: { ...mcpMono, fontSize: 10, color: "var(--red)" } }, p.server || "server", " ", p.field, ": ", p.reason)), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: mcpSmall,
      disabled: busy,
      onClick: () => setDrafts(drafts.concat([{ _id: "new:" + Date.now() + ":" + drafts.length, name: "", type: "stdio", command: "", args: [], env: [] }]))
    },
    "+ server"
  ), !data.parse_error && /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: mcpSmall, disabled: busy, onClick: () => save(false) }, "save"), data.parse_error && !replacing && /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: mcpSmall, disabled: busy, onClick: () => setReplacing(true) }, "replace the broken file")), replacing && /* @__PURE__ */ React.createElement(TypedConfirm, { word: "replace", label: "replace", busy, onCancel: () => setReplacing(false), onConfirm: () => save(true) }), stale && /* @__PURE__ */ React.createElement("div", { style: { ...mcpHint, color: "var(--amber)" } }, "the file changed on disk since it was loaded: reload, then make the change again"), /* @__PURE__ */ React.createElement(McpMsg, { msg }), saved && /* @__PURE__ */ React.createElement(RestartOffer, { cousin, note: data.restart_note }));
}
function McpDiagnostics({ cousin }) {
  const [status, , loadStatus] = useMcpResource(`/api/cousins/${cousin.slug}/mcp/status`);
  const [selftest, setSelftest] = React.useState(null);
  const [last, setLast] = React.useState(void 0);
  const [busy, setBusy] = React.useState(null);
  const [approving, setApproving] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  React.useEffect(() => {
    setSelftest(null);
    setLast(void 0);
    setMsg(null);
    setApproving(false);
  }, [cousin.slug]);
  const run = async (what) => {
    setBusy(what);
    setMsg(null);
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
        const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/mcp/approve`, {});
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        setMsg({ ok: true, text: `approved in ${d.settings_file}: ${d.note}` });
        setApproving(false);
        loadStatus();
      }
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally {
      setBusy(null);
    }
  };
  const runner = status && status.lane && !["tmux-legacy", "tmux"].includes(status.lane);
  const stateTone = { connected: "green", failed: "red", unrecorded: "amber" };
  return /* @__PURE__ */ React.createElement("div", { "data-mcp-diagnostics": true, style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: mcpSmall,
      disabled: !!busy,
      onClick: () => run("selftest"),
      title: "load the registry, build every schema, resolve every command"
    },
    busy === "selftest" ? "testing..." : "selftest"
  ), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: mcpSmall,
      disabled: !!busy,
      onClick: () => run("last"),
      title: "what the harness recorded the last time it connected this cousin's MCP server"
    },
    "last connection"
  ), !runner && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: mcpSmall,
      disabled: !!busy || approving || !status || !status.mcp_json || !status.settings_file,
      onClick: () => setApproving(true),
      title: "trust the home and enable `cousin` in the harness settings file config/harness.toml names"
    },
    "approve"
  ), status && status.approved === true && /* @__PURE__ */ React.createElement(Pill, { tone: "green" }, "approved"), status && status.approved === false && /* @__PURE__ */ React.createElement(Pill, { tone: "amber" }, "not approved")), status && /* @__PURE__ */ React.createElement("div", { style: mcpHint }, runner ? "A runner cousin serves `cousin` in-process: approval matters only to the tmux lane's harness." : status.settings_file ? `approve rewrites the whole harness settings file ${status.settings_file} to trust this home and enable \`cousin\`; the harness reads it at the next session start` : status.reason || "no harness settings file configured"), approving && status && /* @__PURE__ */ React.createElement(
    TypedConfirm,
    {
      word: "approve",
      label: "approve",
      busy: !!busy,
      onCancel: () => setApproving(false),
      onConfirm: () => run("approve")
    },
    "This rewrites the whole harness settings file, ",
    status.settings_file,
    ", not only this home's entry. A live harness session keeps its own copy and writes it back: it can overwrite this change, or this write can overwrite what the session wrote. Approve while ",
    cousin.slug,
    "'s session is stopped."
  ), /* @__PURE__ */ React.createElement(McpMsg, { msg }), selftest && /* @__PURE__ */ React.createElement("div", { style: mcpBox, "data-mcp-selftest": true }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(Pill, { tone: selftest.ok ? "green" : "red" }, selftest.ok ? "selftest ok" : "selftest failed"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", wordBreak: "break-all" } }, selftest.registry || "no registry")), selftest.error && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--red)" } }, selftest.error), selftest.ceiling !== void 0 && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-2)" } }, selftest.tools.length, " tools · ceiling ", selftest.ceiling, " · timeout ", selftest.timeout, "s · output cap ", selftest.max_output, " chars"), (selftest.tools || []).map((t) => /* @__PURE__ */ React.createElement("div", { key: t.name, style: { ...mcpMono, color: "var(--fg-1)" } }, t.name, " ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, t.commands.join(", "), t.operators ? ` · operators: ${t.operators.join(", ") || "none"}` : ""))), (selftest.commands || []).map((c) => /* @__PURE__ */ React.createElement("div", { key: c.command, style: { ...mcpMono, color: c.found ? "var(--fg-2)" : "var(--red)" } }, c.command, " -> ", c.where)), selftest.sdk && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-3)" } }, "mcp sdk: ", selftest.sdk.present ? `present (${selftest.sdk.versions.join(", ")})` : "absent"), (selftest.skipped || []).map((s) => /* @__PURE__ */ React.createElement("div", { key: s.name, style: { ...mcpMono, color: "var(--amber)" } }, "skipped ", s.name, ": ", s.reason)), (selftest.missing_handlers || []).length > 0 && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--red)" } }, "no in-process handler: ", selftest.missing_handlers.join(", "))), last !== void 0 && /* @__PURE__ */ React.createElement("div", { style: mcpBox, "data-mcp-last": true }, last === null ? /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-3)" } }, "no MCP connection recorded for this home") : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(Pill, { tone: stateTone[last.state] || "gray" }, last.state), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, last.when || "?", " · session ", last.session_id || "?")), last.earlier && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-3)" } }, "an earlier attempt in this session: ", last.earlier), last.detail && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-1)", whiteSpace: "pre-wrap" } }, last.detail), last.stderr && /* @__PURE__ */ React.createElement("pre", { style: { ...mcpMono, fontSize: 10, color: "var(--fg-2)", whiteSpace: "pre-wrap", margin: 0 } }, last.stderr), /* @__PURE__ */ React.createElement("div", { style: { ...mcpHint, wordBreak: "break-all" } }, last.path))));
}
function McpPanel({ cousin }) {
  const [tab, setTab] = React.useState("tools");
  const tabs = [["tools", "tool registry"], ["servers", "servers (.mcp.json)"], ["diag", "cousin-mcp"]];
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "mcp"), /* @__PURE__ */ React.createElement("div", { "data-mcp-panel": true, style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4, flexWrap: "wrap" } }, tabs.map(([id, label]) => /* @__PURE__ */ React.createElement("button", { key: id, className: `btn ${tab === id ? "active" : "ghost"}`, style: mcpSmall, onClick: () => setTab(id) }, label))), tab === "tools" && /* @__PURE__ */ React.createElement(RegistryEditor, { url: `/api/cousins/${cousin.slug}/mcp/registry`, scope: "cousin", cousin }), tab === "servers" && /* @__PURE__ */ React.createElement(McpServersEditor, { cousin }), tab === "diag" && /* @__PURE__ */ React.createElement(McpDiagnostics, { cousin })));
}
function PolicyChips({ label, items, onChange, disabled, hint, check }) {
  const [draft, setDraft] = React.useState("");
  const [err, setErr] = React.useState(null);
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    const why = check ? check(v) : null;
    if (why) {
      setErr(why);
      return;
    }
    if (!items.includes(v)) onChange(items.concat([v]));
    setDraft("");
    setErr(null);
  };
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" } }, label), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4, flexWrap: "wrap" } }, items.length === 0 && /* @__PURE__ */ React.createElement("span", { style: { ...mcpMono, color: "var(--fg-3)" } }, "none"), items.map((v) => /* @__PURE__ */ React.createElement("span", { key: v, className: "pill gray", style: { display: "inline-flex", gap: 4, alignItems: "center" } }, v, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: { ...mcpSmall, padding: "0 4px" },
      disabled,
      onClick: () => onChange(items.filter((x) => x !== v)),
      title: `remove ${v}`
    },
    "×"
  )))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft,
      disabled,
      placeholder: "tool name, or a prefix*",
      style: { width: 200 },
      onChange: (e) => {
        setDraft(e.target.value);
        setErr(null);
      },
      onKeyDown: (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          add();
        }
      }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: mcpSmall, disabled: disabled || !draft.trim(), onClick: add }, "add"), hint && /* @__PURE__ */ React.createElement("span", { style: mcpHint }, hint)), err && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--red)" } }, err));
}
function PolicyPatterns({ items, onChange, disabled, lane, problems }) {
  const [draft, setDraft] = React.useState("");
  const add = () => {
    if (!draft) return;
    if (!items.includes(draft)) onChange(items.concat([draft]));
    setDraft("");
  };
  const opencode = lane === "opencode";
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" } }, "deny_bash_patterns"), items.length === 0 && /* @__PURE__ */ React.createElement("span", { style: { ...mcpMono, color: "var(--fg-3)" } }, "none"), items.map((p, i) => {
    const js = jsRegexProblem(p);
    const py = (problems || []).find((x) => x.key === "deny_bash_patterns" && x.entry === p);
    return /* @__PURE__ */ React.createElement("div", { key: i, style: { display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "txt",
        value: p,
        disabled,
        style: { flex: "1 1 220px", minWidth: 0, fontFamily: "var(--mono)" },
        onChange: (e) => onChange(items.map((x, j) => j === i ? e.target.value : x))
      }
    ), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: mcpSmall, disabled, onClick: () => onChange(items.filter((_, j) => j !== i)) }, "×"), py && /* @__PURE__ */ React.createElement("span", { style: { ...mcpHint, color: "var(--red)", flexBasis: "100%" } }, "does not compile: ", py.reason), js && /* @__PURE__ */ React.createElement("span", { style: { ...mcpHint, color: opencode ? "var(--red)" : "var(--amber)", flexBasis: "100%" } }, "not a JavaScript RegExp (", js, ")", opencode ? ": on opencode every command is denied until it is rewritten" : ": it would fail on the opencode lane"));
  }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft,
      disabled,
      placeholder: "\\bgit\\s+push\\s+--force",
      style: { flex: "1 1 220px", minWidth: 0, fontFamily: "var(--mono)" },
      onChange: (e) => setDraft(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          add();
        }
      }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: mcpSmall, disabled: disabled || !draft, onClick: add }, "add")), /* @__PURE__ */ React.createElement("div", { style: mcpHint }, "A regex (Python syntax, checked on save) searched in the `command` string of any tool call except the cousin's own mcp__cousin__* tools. ", opencode ? "On opencode the plugin runs it as a JavaScript RegExp, so it must compile in both." : ""));
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
  const [loosen, setLoosen] = React.useState(null);
  const [replacing, setReplacing] = React.useState(false);
  React.useEffect(() => {
    if (!data) return;
    setDraft({
      deny_tools: data.deny_tools || [],
      deny_bash_patterns: data.deny_bash_patterns || [],
      ask: data.ask || [],
      outbound_filter: data.outbound_filter !== false
    });
    setLoosen(null);
    setProblems([]);
  }, [data]);
  if (!data || !draft) {
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "policy"), /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-3)" } }, loadErr ? `policy unavailable: ${loadErr}` : "loading..."));
  }
  const protectedTool = data.protected || MCP_HANDOFF_TOOL;
  const noHandoff = (v) => mcpNamesTool(v, protectedTool) ? `never deny ${protectedTool}: every generation ends through it` : null;
  const set = (patch) => {
    setDraft(Object.assign({}, draft, patch));
    setLoosen(null);
  };
  const before = {
    deny_tools: data.deny_tools,
    deny_bash_patterns: data.deny_bash_patterns,
    ask: data.ask,
    outbound_filter: data.outbound_filter
  };
  const removal = policyRemovals(data.error ? {} : before, draft);
  const dirty = JSON.stringify(before) !== JSON.stringify(draft) || !data.exists;
  const save = async (opts) => {
    setBusy(true);
    setMsg(null);
    setStale(false);
    setProblems([]);
    try {
      const body = Object.assign({ etag: data.etag }, draft, opts || {});
      const { r, d } = await apiSend("POST", url, body);
      if (r.status === 409 && d.needs_confirm) {
        setLoosen(d.removed);
        return;
      }
      if (r.status === 409 && d.stale) setStale(true);
      if (!r.ok) {
        if (d.problems) setProblems(d.problems);
        throw new Error(d.error || `HTTP ${r.status}`);
      }
      setData(d);
      setSaved(true);
      setReplacing(false);
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };
  const ev = data.last_event;
  const tmux = data.lane === "tmux-legacy";
  const shownRemoval = loosen || (removal.any ? removal : null);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "policy"), /* @__PURE__ */ React.createElement("div", { "data-policy-panel": true, style: { display: "flex", flexDirection: "column", gap: 10 } }, /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)" } }, data.file), data.exists ? /* @__PURE__ */ React.createElement(Pill, { tone: "green" }, "present") : /* @__PURE__ */ React.createElement(Pill, { tone: "gray" }, "absent: every tool allowed"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: mcpSmall, disabled: busy, onClick: () => {
    setMsg(null);
    setStale(false);
    load();
  } }, "reload"), !data.exists && data.template && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: mcpSmall,
      disabled: busy,
      onClick: () => set({
        deny_tools: (data.template.deny_tools || []).filter((v) => !noHandoff(v)),
        deny_bash_patterns: data.template.deny_bash_patterns || [],
        ask: data.template.ask || [],
        outbound_filter: data.template.outbound_filter !== false
      })
    },
    "fill from the template"
  )), /* @__PURE__ */ React.createElement("div", { style: mcpHint }, "A guardrail, not a sandbox: a pattern stops the command line it names, not every way to the same effect, and the model can rewrite this file. It is read once, at the runner's start.", tmux ? " This cousin is on the tmux lane: the runner enforces policy.toml, so it has no effect here until the cousin moves to a runner." : ""), data.error && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--red)" } }, data.error, " (the runner refuses to start with it)"), ev && ev.payload && ev.payload.describe && /* @__PURE__ */ React.createElement("div", { style: { ...mcpMono, fontSize: 10, color: "var(--fg-3)" } }, "in force since the last start: ", ev.payload.describe), /* @__PURE__ */ React.createElement(
    PolicyChips,
    {
      label: "deny_tools",
      items: draft.deny_tools,
      disabled: busy,
      check: noHandoff,
      hint: "exact names or prefix*: mcp__ha__* denies a whole server",
      onChange: (v) => set({ deny_tools: v })
    }
  ), /* @__PURE__ */ React.createElement(
    PolicyPatterns,
    {
      items: draft.deny_bash_patterns,
      disabled: busy,
      lane: data.lane,
      problems,
      onChange: (v) => set({ deny_bash_patterns: v })
    }
  ), /* @__PURE__ */ React.createElement(
    PolicyChips,
    {
      label: "ask",
      items: draft.ask,
      disabled: busy,
      check: noHandoff,
      hint: "no approval surface yet: enforced as a deny",
      onChange: (v) => set({ ask: v })
    }
  ), /* @__PURE__ */ React.createElement("label", { style: { ...mcpMono, display: "flex", gap: 6, alignItems: "center" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "checkbox",
      checked: draft.outbound_filter,
      disabled: busy,
      onChange: (e) => set({ outbound_filter: e.target.checked })
    }
  ), "outbound_filter", /* @__PURE__ */ React.createElement("span", { style: mcpHint }, "reply and send cross config/outbound-filter.json")), /* @__PURE__ */ React.createElement("div", { style: mcpHint }, protectedTool, " can never be denied or asked for: every generation ends through it."), problems.filter((p) => p.key !== "deny_bash_patterns").map((p, i) => /* @__PURE__ */ React.createElement("div", { key: i, style: { ...mcpMono, fontSize: 10, color: "var(--red)" } }, p.key, " ", p.entry, ": ", p.reason)), shownRemoval && /* @__PURE__ */ React.createElement("div", { "data-policy-loosen": true, style: { ...mcpHint, color: "var(--amber)", borderLeft: "2px solid var(--amber)", paddingLeft: 8 } }, "This change loosens the policy:", ["deny_tools", "deny_bash_patterns", "ask"].filter((k) => (shownRemoval[k] || []).length).map((k) => /* @__PURE__ */ React.createElement("div", { key: k }, "removes from ", k, ": ", shownRemoval[k].join(", "))), shownRemoval.outbound_filter && /* @__PURE__ */ React.createElement("div", null, "turns the outbound filter off")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, !data.error && (loosen ? /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: mcpSmall, disabled: busy, onClick: () => save({ confirm_loosening: true }) }, "save and loosen") : /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: mcpSmall, disabled: busy || !dirty, onClick: () => save() }, "save")), data.error && !replacing && /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: mcpSmall, disabled: busy, onClick: () => setReplacing(true) }, "replace the broken file")), replacing && /* @__PURE__ */ React.createElement(
    TypedConfirm,
    {
      word: "replace",
      label: "replace",
      busy,
      onCancel: () => setReplacing(false),
      onConfirm: () => save({ replace_broken: true })
    }
  ), stale && /* @__PURE__ */ React.createElement("div", { style: { ...mcpHint, color: "var(--amber)" } }, "the file changed on disk since it was loaded: reload, then make the change again"), /* @__PURE__ */ React.createElement(McpMsg, { msg }), saved && /* @__PURE__ */ React.createElement(RestartOffer, { cousin, note: data.restart_note })));
}
function McpInstallPanel() {
  return /* @__PURE__ */ React.createElement("div", { className: "panel", style: { marginTop: 14 }, "data-mcp-install": true }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "mcp tool registry"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", fontSize: 11, marginLeft: 8 } }, "the install default: what a cousin without its own mcp-registry.toml reads, and what spawn copies into a new home")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { padding: 14 } }, /* @__PURE__ */ React.createElement(RegistryEditor, { url: "/api/mcp/registry", scope: "install" })));
}
registerSlot("inspector.panels", { id: "mcp", order: 30, render: ({ cousin }) => /* @__PURE__ */ React.createElement(McpPanel, { cousin }) });
registerSlot("inspector.panels", { id: "policy", order: 31, render: ({ cousin }) => /* @__PURE__ */ React.createElement(PolicyPanel, { cousin }) });
registerSlot("settings.panels", { id: "mcp", order: 30, render: () => /* @__PURE__ */ React.createElement(McpInstallPanel, null) });
Object.assign(window, {
  McpPanel,
  PolicyPanel,
  McpInstallPanel,
  RegistryEditor,
  McpServersEditor,
  McpDiagnostics,
  RestartOffer,
  policyRemovals,
  jsRegexProblem,
  mcpNamesTool,
  mcpServerDraft,
  mcpServerBody,
  mcpApplySuggestion
});
