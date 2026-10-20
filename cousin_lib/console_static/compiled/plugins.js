function pluginTabs(cousin) {
  const rows = cousin && Array.isArray(cousin.plugins) ? cousin.plugins : [];
  return rows.filter((p) => p && p.tab && typeof p.tab.url === "string" && p.tab.url.indexOf("/plugins/") === 0).map((p) => ({
    name: p.name,
    title: p.tab.title || p.name,
    url: p.tab.url,
    placement: p.tab.placement === "chat" ? "chat" : "pane"
  }));
}
function pluginStripKeys(slug, name) {
  return {
    height: `fw_plugin_strip_h:${slug}:${name}`,
    collapsed: `fw_plugin_strip_collapsed:${slug}:${name}`
  };
}
const PLUGIN_STRIP_MIN = 120;
const PLUGIN_STRIP_DEFAULT = 240;
function pluginStripClamp(h, colH) {
  const v = h === null || h === void 0 || h === "" ? NaN : Number(h);
  const max = colH > 0 ? Math.max(PLUGIN_STRIP_MIN, Math.floor(colH * 0.7)) : Infinity;
  return Math.round(Math.min(max, Math.max(
    PLUGIN_STRIP_MIN,
    Number.isFinite(v) ? v : PLUGIN_STRIP_DEFAULT
  )));
}
function pluginDraftChanged(enabled, draft) {
  const a = (enabled || []).slice().sort();
  const b = (draft || []).slice().sort();
  return JSON.stringify(a) !== JSON.stringify(b);
}
function PluginPaneTabs({ tabs, active, onPick, paneLabel }) {
  if (!tabs || !tabs.length) return null;
  return /* @__PURE__ */ React.createElement("div", { className: "pane-tabs", role: "tablist", "data-plugin-tabs": true }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn " + (active === "pane" ? "active" : "ghost"),
      role: "tab",
      "aria-selected": active === "pane",
      onClick: () => onPick("pane")
    },
    paneLabel
  ), tabs.map((t) => /* @__PURE__ */ React.createElement(
    "button",
    {
      key: t.name,
      className: "btn " + (active === t.name ? "active" : "ghost"),
      role: "tab",
      "aria-selected": active === t.name,
      onClick: () => onPick(t.name),
      title: `plugin ${t.name}: ${t.url}`
    },
    t.title
  )));
}
function PluginFrame({ tab }) {
  if (!tab) return null;
  return /* @__PURE__ */ React.createElement("iframe", { className: "plugin-frame", src: tab.url, title: tab.title, "data-plugin": tab.name });
}
function PluginChatStrip({ slug, tab }) {
  const keys = pluginStripKeys(slug, tab.name);
  const [h, setH] = React.useState(() => {
    try {
      return pluginStripClamp(localStorage.getItem(keys.height), 0);
    } catch (e) {
      return PLUGIN_STRIP_DEFAULT;
    }
  });
  const [collapsed, setCollapsed] = React.useState(() => {
    try {
      return localStorage.getItem(keys.collapsed) === "1";
    } catch (e) {
      return false;
    }
  });
  React.useEffect(() => {
    try {
      localStorage.setItem(keys.height, String(h));
    } catch (e) {
    }
  }, [keys.height, h]);
  React.useEffect(() => {
    try {
      localStorage.setItem(keys.collapsed, collapsed ? "1" : "0");
    } catch (e) {
    }
  }, [keys.collapsed, collapsed]);
  const ref = React.useRef(null);
  const onGripDown = (e) => {
    const el = ref.current;
    if (!el) return;
    e.preventDefault();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch (err) {
    }
    const col = el.closest(".chat-col");
    const colH = col ? col.getBoundingClientRect().height : 0;
    const startY = e.clientY;
    const startH = el.getBoundingClientRect().height;
    el.classList.add("dragging");
    const move = (m) => setH(pluginStripClamp(startH + m.clientY - startY, colH));
    const up = () => {
      el.classList.remove("dragging");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const popOut = () => window.open(tab.url, `plugin-${slug}-${tab.name}`, "popup,width=760,height=600");
  return /* @__PURE__ */ React.createElement(
    "div",
    {
      ref,
      className: "plugin-strip" + (collapsed ? " collapsed" : ""),
      style: collapsed ? void 0 : { height: h },
      "data-plugin-strip": tab.name
    },
    /* @__PURE__ */ React.createElement("div", { className: "plugin-strip-bar" }, /* @__PURE__ */ React.createElement("span", { className: "plugin-strip-title", title: `plugin ${tab.name}: ${tab.url}` }, tab.title), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: popOut, title: "open the page in its own window" }, "pop out"), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: () => setCollapsed((v) => !v),
        "aria-expanded": !collapsed,
        title: collapsed ? "show the page" : "collapse to this bar"
      },
      collapsed ? "expand" : "collapse"
    )),
    /* @__PURE__ */ React.createElement("iframe", { className: "plugin-frame", src: tab.url, title: tab.title, "data-plugin": tab.name }),
    !collapsed && /* @__PURE__ */ React.createElement(
      "div",
      {
        className: "plugin-strip-grip",
        onPointerDown: onGripDown,
        onDoubleClick: () => setH(PLUGIN_STRIP_DEFAULT),
        role: "separator",
        "aria-orientation": "horizontal",
        title: "drag to resize, double-click to reset"
      }
    )
  );
}
function PluginChatStrips({ slug, tabs }) {
  if (!tabs || !tabs.length) return null;
  return /* @__PURE__ */ React.createElement("div", { className: "plugin-strips", "data-plugin-strips": true }, tabs.map((t) => /* @__PURE__ */ React.createElement(PluginChatStrip, { key: slug + "|" + t.name, slug, tab: t })));
}
const pluginSmall = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const pluginHint = { fontSize: 10, color: "var(--fg-3)", lineHeight: 1.5 };
const pluginErr = { fontFamily: "var(--mono)", fontSize: 10, color: "var(--red)", whiteSpace: "pre-wrap" };
function PluginsPanel({ cousin }) {
  const c = cousin;
  const [data, setData] = React.useState(null);
  const [draft, setDraft] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [saved, setSaved] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const load = React.useCallback(async () => {
    const d = await apiGet(`/api/cousins/${c.slug}/plugins`);
    if (d) {
      setData(d);
      setDraft(null);
    }
  }, [c.slug]);
  React.useEffect(() => {
    setData(null);
    setDraft(null);
    setSaved(null);
    setErr(null);
    if (!c.remote) load();
  }, [c.slug, load]);
  if (c.remote || !data || !(data.available || []).length) return null;
  const enabled = data.enabled || [];
  const want = draft || enabled;
  const dirty = pluginDraftChanged(enabled, want);
  const toggle = (name, on) => {
    const next = want.filter((n) => n !== name);
    if (on) next.push(name);
    setDraft(next);
    setSaved(null);
    setErr(null);
  };
  const save = async () => {
    if (!dirty || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const known = new Set((data.available || []).map((p) => p.name));
      const { r, d } = await apiSend(
        "POST",
        `/api/cousins/${c.slug}/plugins`,
        { enabled: want.filter((n) => known.has(n)) }
      );
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setData(d);
      setDraft(null);
      setSaved(d.changed ? { restart: d.restart_required } : null);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "plugins"), /* @__PURE__ */ React.createElement("div", { "data-plugins-settings": true, style: { display: "flex", flexDirection: "column", gap: 6 } }, (data.available || []).map((p) => /* @__PURE__ */ React.createElement(
    "label",
    {
      key: p.name,
      style: { display: "flex", gap: 8, alignItems: "flex-start" },
      title: `cousin.toml [plugins] enabled: ${p.name}`
    },
    /* @__PURE__ */ React.createElement(
      "input",
      {
        type: "checkbox",
        checked: want.includes(p.name),
        disabled: busy,
        onChange: (e) => toggle(p.name, e.target.checked)
      }
    ),
    /* @__PURE__ */ React.createElement("span", { style: { display: "flex", flexDirection: "column", gap: 2 } }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 11 } }, p.name, p.version ? ` ${p.version}` : ""), p.description && /* @__PURE__ */ React.createElement("span", { style: pluginHint }, p.description), /* @__PURE__ */ React.createElement("span", { style: pluginHint }, [p.mcp && "tools", p.service && "service", p.console && `tab "${p.title}"`].filter(Boolean).join(" · ")))
  )), (data.skipped || []).map((s) => /* @__PURE__ */ React.createElement("span", { key: s.name || "table", style: pluginErr }, s.name ? `${s.name}: ` : "", s.reason)), data.note && /* @__PURE__ */ React.createElement("span", { style: pluginHint }, data.note), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: pluginSmall, disabled: !dirty || busy, onClick: save }, busy ? "saving..." : "save"), dirty && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: pluginSmall,
      disabled: busy,
      onClick: () => {
        setDraft(null);
        setErr(null);
      }
    },
    "discard"
  ), /* @__PURE__ */ React.createElement("span", { style: pluginHint }, "read by the runner at start")), err && /* @__PURE__ */ React.createElement("span", { style: pluginErr }, err), saved && saved.restart && window.AgentRestartOffer && /* @__PURE__ */ React.createElement(AgentRestartOffer, { cousin: c })));
}
registerSlot("inspector.lane", {
  id: "plugins",
  order: 12,
  render: ({ cousin }) => /* @__PURE__ */ React.createElement(PluginsPanel, { cousin })
});
Object.assign(window, {
  pluginTabs,
  pluginDraftChanged,
  pluginStripKeys,
  pluginStripClamp,
  PluginPaneTabs,
  PluginFrame,
  PluginChatStrip,
  PluginChatStrips,
  PluginsPanel
});
