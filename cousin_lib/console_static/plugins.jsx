// Plugins (docs/plugins.md): this package's own file (index.html's package block).
// Its routes are cousin_lib/console/routes_plugins.py (docs/reference/console-api.md,
// "Plugins"). The framework ships no plugin: on an install without one, nothing
// here renders, so the console looks exactly as it does without this file.
//
//   pluginTabs(cousin)   the cousin's console pages from its fleet row (`plugins`):
//                        only an enabled, valid plugin with a [console] page has one,
//                        each placed "pane" (the default) or "chat".
//   PluginPaneTabs       the tab strip at the top of the pane column (chat.jsx),
//                        shown only when a "pane" page exists.
//   PluginFrame          one tab: an iframe of /plugins/<name><page>, same origin.
//   PluginChatStrips     the "chat" pages: one strip each over the chat's messages
//                        (chat.jsx), like a video call; none without such a page.
//   PluginsPanel         the inspector's "plugins" checkbox list (inspector.lane),
//                        writing cousin.toml [plugins] enabled; absent (null) when
//                        the install has no plugins.

// ---- pure helpers (the tests run this block under node) -------------------
// The pages a cousin row offers: [{name, title, url, placement}], in the row's
// order; placement is "chat" or "pane" (the default, and anything else). A row
// without `plugins` (a remote row, an older console) has none.
function pluginTabs(cousin) {
  const rows = (cousin && Array.isArray(cousin.plugins)) ? cousin.plugins : [];
  return rows.filter(p => p && p.tab && typeof p.tab.url === "string"
                          && p.tab.url.indexOf("/plugins/") === 0)
             .map(p => ({ name: p.name, title: p.tab.title || p.name, url: p.tab.url,
                          placement: p.tab.placement === "chat" ? "chat" : "pane" }));
}

// A chat strip's height and collapsed state, kept in localStorage per cousin and plugin.
function pluginStripKeys(slug, name) {
  return { height: `fw_plugin_strip_h:${slug}:${name}`,
           collapsed: `fw_plugin_strip_collapsed:${slug}:${name}` };
}

// A strip's height in px: at least 120, at most 70% of the chat column when its
// height is known (colH > 0), 240 when unset or not a number.
const PLUGIN_STRIP_MIN = 120;
const PLUGIN_STRIP_DEFAULT = 240;
function pluginStripClamp(h, colH) {
  const v = (h === null || h === undefined || h === "") ? NaN : Number(h);
  const max = colH > 0 ? Math.max(PLUGIN_STRIP_MIN, Math.floor(colH * 0.7)) : Infinity;
  return Math.round(Math.min(max, Math.max(PLUGIN_STRIP_MIN,
                                           Number.isFinite(v) ? v : PLUGIN_STRIP_DEFAULT)));
}

// The names a draft enables, sorted, and whether it differs from what is written.
function pluginDraftChanged(enabled, draft) {
  const a = (enabled || []).slice().sort();
  const b = (draft || []).slice().sort();
  return JSON.stringify(a) !== JSON.stringify(b);
}
// ---- end pure helpers --------------------------------------------------------

function PluginPaneTabs({ tabs, active, onPick, paneLabel }) {
  if (!tabs || !tabs.length) return null;
  return (
    <div className="pane-tabs" role="tablist" data-plugin-tabs>
      <button className={"btn " + (active === "pane" ? "active" : "ghost")} role="tab"
              aria-selected={active === "pane"} onClick={() => onPick("pane")}>{paneLabel}</button>
      {tabs.map(t => (
        <button key={t.name} className={"btn " + (active === t.name ? "active" : "ghost")} role="tab"
                aria-selected={active === t.name} onClick={() => onPick(t.name)}
                title={`plugin ${t.name}: ${t.url}`}>{t.title}</button>
      ))}
    </div>
  );
}

function PluginFrame({ tab }) {
  if (!tab) return null;
  return <iframe className="plugin-frame" src={tab.url} title={tab.title} data-plugin={tab.name} />;
}

// One "chat" page: a header bar (title, pop out, collapse), the iframe, and a
// grip on the bottom edge to drag its height. The chat's messages and composer
// stay below it (the strips' box is capped at 70% of the column in styles.css).
function PluginChatStrip({ slug, tab }) {
  const keys = pluginStripKeys(slug, tab.name);
  const [h, setH] = React.useState(() => {
    try { return pluginStripClamp(localStorage.getItem(keys.height), 0); }
    catch (e) { return PLUGIN_STRIP_DEFAULT; }
  });
  const [collapsed, setCollapsed] = React.useState(() => {
    try { return localStorage.getItem(keys.collapsed) === "1"; }
    catch (e) { return false; }
  });
  React.useEffect(() => {
    try { localStorage.setItem(keys.height, String(h)); } catch (e) { /* ignore */ }
  }, [keys.height, h]);
  React.useEffect(() => {
    try { localStorage.setItem(keys.collapsed, collapsed ? "1" : "0"); } catch (e) { /* ignore */ }
  }, [keys.collapsed, collapsed]);
  const ref = React.useRef(null);
  const onGripDown = (e) => {
    const el = ref.current;
    if (!el) return;
    e.preventDefault();
    // the pointer stays the grip's over any iframe (this strip's, or one below it)
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
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
  return (
    <div ref={ref} className={"plugin-strip" + (collapsed ? " collapsed" : "")}
         style={collapsed ? undefined : { height: h }} data-plugin-strip={tab.name}>
      <div className="plugin-strip-bar">
        <span className="plugin-strip-title" title={`plugin ${tab.name}: ${tab.url}`}>{tab.title}</span>
        <span style={{ flex: 1 }} />
        <button className="btn ghost" onClick={popOut} title="open the page in its own window">pop out</button>
        <button className="btn ghost" onClick={() => setCollapsed(v => !v)} aria-expanded={!collapsed}
                title={collapsed ? "show the page" : "collapse to this bar"}>{collapsed ? "expand" : "collapse"}</button>
      </div>
      <iframe className="plugin-frame" src={tab.url} title={tab.title} data-plugin={tab.name} />
      {!collapsed && (
        <div className="plugin-strip-grip" onPointerDown={onGripDown}
             onDoubleClick={() => setH(PLUGIN_STRIP_DEFAULT)} role="separator" aria-orientation="horizontal"
             title="drag to resize, double-click to reset" />
      )}
    </div>
  );
}

function PluginChatStrips({ slug, tabs }) {
  if (!tabs || !tabs.length) return null;
  return (
    <div className="plugin-strips" data-plugin-strips>
      {tabs.map(t => <PluginChatStrip key={slug + "|" + t.name} slug={slug} tab={t} />)}
    </div>
  );
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
    if (d) { setData(d); setDraft(null); }
  }, [c.slug]);
  React.useEffect(() => {
    setData(null); setDraft(null); setSaved(null); setErr(null);
    if (!c.remote) load();
  }, [c.slug, load]);
  // No plugin installed (or none valid and enabled in config/plugins.toml): no section.
  if (c.remote || !data || !(data.available || []).length) return null;
  const enabled = data.enabled || [];
  const want = draft || enabled;
  const dirty = pluginDraftChanged(enabled, want);
  const toggle = (name, on) => {
    const next = want.filter(n => n !== name);
    if (on) next.push(name);
    setDraft(next); setSaved(null); setErr(null);
  };
  const save = async () => {
    if (!dirty || busy) return;
    setBusy(true); setErr(null);
    try {
      // a name the install no longer has is dropped with the write
      const known = new Set((data.available || []).map(p => p.name));
      const { r, d } = await apiSend("POST", `/api/cousins/${c.slug}/plugins`,
                                     { enabled: want.filter(n => known.has(n)) });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setData(d); setDraft(null);
      setSaved(d.changed ? { restart: d.restart_required } : null);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>plugins</SectionLabel>
      <div data-plugins-settings style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {(data.available || []).map(p => (
          <label key={p.name} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}
                 title={`cousin.toml [plugins] enabled: ${p.name}`}>
            <input type="checkbox" checked={want.includes(p.name)} disabled={busy}
                   onChange={e => toggle(p.name, e.target.checked)} />
            <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11 }}>{p.name}{p.version ? ` ${p.version}` : ""}</span>
              {p.description && <span style={pluginHint}>{p.description}</span>}
              <span style={pluginHint}>{[p.mcp && "tools", p.service && "service", p.console && `tab "${p.title}"`].filter(Boolean).join(" · ")}</span>
            </span>
          </label>
        ))}
        {(data.skipped || []).map(s => (
          <span key={s.name || "table"} style={pluginErr}>{s.name ? `${s.name}: ` : ""}{s.reason}</span>
        ))}
        {data.note && <span style={pluginHint}>{data.note}</span>}
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn primary" style={pluginSmall} disabled={!dirty || busy} onClick={save}>
            {busy ? "saving..." : "save"}
          </button>
          {dirty && <button className="btn ghost" style={pluginSmall} disabled={busy}
                            onClick={() => { setDraft(null); setErr(null); }}>discard</button>}
          <span style={pluginHint}>read by the runner at start</span>
        </div>
        {err && <span style={pluginErr}>{err}</span>}
        {saved && saved.restart && window.AgentRestartOffer && <AgentRestartOffer cousin={c} />}
      </div>
    </>
  );
}

registerSlot("inspector.lane", { id: "plugins", order: 12,
                                 render: ({ cousin }) => <PluginsPanel cousin={cousin} /> });

Object.assign(window, { pluginTabs, pluginDraftChanged, pluginStripKeys, pluginStripClamp,
                        PluginPaneTabs, PluginFrame, PluginChatStrip, PluginChatStrips,
                        PluginsPanel });
