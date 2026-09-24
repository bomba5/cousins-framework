// Main app shell + routing
const { useState: useStateApp, useEffect: useEffectApp } = React;

const NAV = [
  { id: "overview", label: "Overview", icon: I.host,    kbd: "1" },
  { id: "cousins",  label: "Cousins",  icon: I.cousins, kbd: "2" },
  { id: "jobs",     label: "Jobs",     icon: I.list,    kbd: "3" },
  { id: "memory",   label: "Memory",   icon: I.memory,  kbd: "4" },
  { id: "loops",    label: "Loops",    icon: I.loops,   kbd: "5" },
  { id: "tokens",   label: "Tokens",   icon: I.tokens,  kbd: "6" },
  { id: "tracker",  label: "Tracker",  icon: I.tracker, kbd: "7" },
  { id: "meetings", label: "Meetings", icon: I.meetings, kbd: "8" },
  { id: "settings", label: "Settings", icon: I.host,    kbd: "0" },
];

// Sidebar groups ({groups, assignments}) are a per-user preference kept on
// the server (/api/prefs/sidebar), so every browser and the phone's
// home-screen app show the same layout. Local storage under this key is
// only a cache for the first paint and the one-time migration of a layout
// made before the server kept it (docs/reference/console-api.md,
// "Preferences").
const SIDEBAR_KEY = "console_sidebar_v1";
const DEFAULT_GROUPS = [{ id: "sessions", name: "Sessions", collapsed: false }];

function loadSidebar() {
  try {
    const raw = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || "null");
    if (raw && Array.isArray(raw.groups) && raw.groups.length) return raw;
  } catch (_e) { /* private mode / bad JSON: start fresh */ }
  return { groups: DEFAULT_GROUPS.slice(), assignments: {} };
}

function SidebarGroups({ cousins, activeCousin, view, showHidden, chatUserFor, onPick }) {
  const [config, setConfig] = React.useState(loadSidebar);
  const [contextMenu, setContextMenu] = React.useState(null);
  const [groupMenu, setGroupMenu] = React.useState(null); // {gid, x, y}
  const [draggingSlug, setDraggingSlug] = React.useState(null);
  const [draggingGroup, setDraggingGroup] = React.useState(null);

  const cache = (value) => {
    try { localStorage.setItem(SIDEBAR_KEY, JSON.stringify(value)); } catch (_e) {}
  };
  const save = (value) => apiSend("POST", "/api/prefs/sidebar", { sidebar: value })
    .catch(e => console.error("sidebar save failed:", e));

  const persist = (next) => {
    const merged = { groups: next.groups || config.groups, assignments: next.assignments || config.assignments };
    setConfig(merged);
    cache(merged);
    save(merged);
  };

  // The server copy wins; refetched when the page comes back to the
  // foreground, so a change made on another device shows up. With no
  // server copy yet, a layout this browser already had is uploaded once.
  React.useEffect(() => {
    let alive = true;
    const pull = async () => {
      const d = await apiGet("/api/prefs/sidebar");
      if (!alive || !d) return;
      const remote = d.sidebar;
      if (remote && Array.isArray(remote.groups) && remote.groups.length) {
        setConfig(remote);
        cache(remote);
      } else {
        const local = loadSidebar();
        const custom = local.groups.length > 1 || Object.keys(local.assignments || {}).length
          || local.groups[0].name !== DEFAULT_GROUPS[0].name;
        if (custom) save(local);
      }
    };
    pull();
    const onVisible = () => { if (document.visibilityState === "visible") pull(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { alive = false; document.removeEventListener("visibilitychange", onVisible); };
  }, []);

  const groups = config.groups.length ? config.groups : DEFAULT_GROUPS;
  const assignments = config.assignments || {};

  // Workers (type=worker) have no chat server, so they get no row in the chat
  // sidebar. They are still listed in the Cousins view with a worker badge.
  const visible = (cousins || []).filter(c => (showHidden || !c.hidden) && c.type !== "worker");
  const groupOf = (slug) => assignments[slug] || "sessions";
  const cousinsByGroup = {};
  for (const g of groups) cousinsByGroup[g.id] = [];
  for (const c of visible) {
    const gid = cousinsByGroup[groupOf(c.slug)] !== undefined ? groupOf(c.slug) : groups[0].id;
    cousinsByGroup[gid].push(c);
  }

  const setCollapsed = (gid, collapsed) => {
    persist({ ...config, groups: groups.map(g => g.id === gid ? { ...g, collapsed } : g) });
  };

  const moveTo = (slug, gid) => {
    persist({ ...config, assignments: { ...assignments, [slug]: gid } });
    setContextMenu(null);
  };

  const newGroupAndMove = (slug) => {
    const name = window.prompt("new group name:");
    if (!name || !name.trim()) return;
    const id = name.trim().toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "").slice(0, 32) || `g${Date.now()}`;
    if (groups.some(g => g.id === id)) { moveTo(slug, id); return; }
    persist({
      groups: [...groups, { id, name: name.trim(), collapsed: false }],
      assignments: { ...assignments, [slug]: id },
    });
    setContextMenu(null);
  };

  const deleteGroup = (gid) => {
    if (gid === "sessions") return;
    if (!window.confirm("delete group? cousins move to Sessions.")) return;
    const newAssignments = { ...assignments };
    for (const slug of Object.keys(newAssignments)) {
      if (newAssignments[slug] === gid) newAssignments[slug] = "sessions";
    }
    persist({ groups: groups.filter(g => g.id !== gid), assignments: newAssignments });
  };

  const reorderGroup = (fromId, toId) => {
    if (fromId === toId) return;
    const fromIdx = groups.findIndex(g => g.id === fromId);
    const toIdx = groups.findIndex(g => g.id === toId);
    if (fromIdx < 0 || toIdx < 0) return;
    const next = groups.slice();
    const [moved] = next.splice(fromIdx, 1);
    next.splice(toIdx, 0, moved);
    persist({ ...config, groups: next });
  };

  const renameGroup = (gid) => {
    const cur = groups.find(g => g.id === gid);
    if (!cur) return;
    const name = window.prompt("rename group:", cur.name);
    if (!name || !name.trim() || name === cur.name) return;
    persist({ ...config, groups: groups.map(g => g.id === gid ? { ...g, name: name.trim() } : g) });
  };

  React.useEffect(() => {
    if (!contextMenu && !groupMenu) return;
    const close = () => { setContextMenu(null); setGroupMenu(null); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [contextMenu, groupMenu]);

  const renderCousin = (c) => {
    const stopped = c.status !== "running";
    let unread = false;
    try {
      const seen = parseInt(localStorage.getItem(chatSeenKey(c.slug, chatUserFor(c))) || "0", 10);
      const lastMsg = c.lastMsgTs || 0;
      unread = lastMsg > 0 && lastMsg > seen && !(activeCousin === c.slug && view === "chat");
    } catch (e) {}
    return (
      <div
        key={c.slug}
        className={`navitem ${activeCousin === c.slug && view === "chat" ? "active" : ""} ${stopped ? "disabled" : ""}`}
        draggable
        onDragStart={(e) => { setDraggingSlug(c.slug); e.dataTransfer.effectAllowed = "move"; }}
        onDragEnd={() => setDraggingSlug(null)}
        onContextMenu={(e) => { e.preventDefault(); setContextMenu({ slug: c.slug, x: e.clientX, y: e.clientY }); }}
        onClick={stopped ? undefined : () => onPick(c.slug)}
        title={stopped ? `@${c.slug} is stopped - start it from the Cousins view first` : `open chat with @${c.slug}` + (unread ? " (unread)" : "") + " (right-click to move)"}
        style={(stopped || c.hidden) ? { opacity: stopped ? 0.5 : 0.7, cursor: stopped ? "not-allowed" : "grab" } : { cursor: "grab" }}
      >
        <Led state={c.status === "running" ? "running" : "stopped"} pulse={c.status === "running"} />
        <span style={{ fontWeight: unread ? 600 : 400 }}>{(c.name || c.slug).toLowerCase()}</span>
        {unread && (
          <span title="unread messages"
                style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%",
                          background: "var(--accent)", marginLeft: 4 }} />
        )}
        {c.hidden && <span className="badge" title="hidden (revealed by Settings: show hidden)" style={{ background: "oklch(from var(--fg-3) l c h / 0.25)" }}>hidden</span>}
      </div>
    );
  };

  return (
    <>
      {groups.map(g => {
        const list = cousinsByGroup[g.id] || [];
        return (
          <React.Fragment key={g.id}>
            <div
              draggable
              onDragStart={(e) => { setDraggingGroup(g.id); e.dataTransfer.effectAllowed = "move"; }}
              onDragEnd={() => setDraggingGroup(null)}
              onDragOver={(e) => { if (draggingSlug || (draggingGroup && draggingGroup !== g.id)) e.preventDefault(); }}
              onDrop={(e) => {
                e.preventDefault();
                if (draggingSlug) moveTo(draggingSlug, g.id);
                else if (draggingGroup && draggingGroup !== g.id) reorderGroup(draggingGroup, g.id);
                setDraggingSlug(null);
                setDraggingGroup(null);
              }}
              className="section-label"
              style={{
                marginTop: 16, display: "flex", alignItems: "center", gap: 6,
                cursor: "grab", userSelect: "none",
                opacity: draggingGroup === g.id ? 0.4 : 1,
              }}
              onClick={() => setCollapsed(g.id, !g.collapsed)}
              onContextMenu={(e) => {
                e.preventDefault();
                if (g.id === "sessions") return;
                setGroupMenu({ gid: g.id, x: e.clientX, y: e.clientY });
              }}
              title={g.id === "sessions" ? "click to collapse, drag to reorder" : "click to collapse, drag to reorder, right-click for rename/delete, drop a cousin to move them here"}>
              <span style={{ fontFamily: "var(--mono)", fontSize: 9 }}>{g.collapsed ? "▸" : "▾"}</span>
              <span>{g.name.toLowerCase()}</span>
              {list.length > 0 && <span style={{ marginLeft: "auto", color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 10 }}>{list.length}</span>}
            </div>
            {!g.collapsed && list.map(renderCousin)}
          </React.Fragment>
        );
      })}
      {groupMenu && (
        <div
          onClick={(e) => e.stopPropagation()}
          style={{
            position: "fixed", left: groupMenu.x, top: groupMenu.y,
            background: "var(--bg-1)", border: "1px solid var(--line)",
            borderRadius: 4, padding: 6, zIndex: 9000,
            boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
            display: "flex", flexDirection: "column", gap: 2, minWidth: 160,
          }}>
          <div style={{ fontSize: 10, color: "var(--fg-3)", padding: "2px 8px", fontFamily: "var(--mono)" }}>
            group: {(groups.find(g => g.id === groupMenu.gid) || {}).name}
          </div>
          <button className="btn ghost"
                  onClick={() => { renameGroup(groupMenu.gid); setGroupMenu(null); }}
                  style={{ fontSize: 12, padding: "4px 10px", textAlign: "left" }}>
            rename...
          </button>
          <button className="btn ghost"
                  onClick={() => { deleteGroup(groupMenu.gid); setGroupMenu(null); }}
                  style={{ fontSize: 12, padding: "4px 10px", textAlign: "left", color: "var(--err, #ef4444)" }}>
            delete (cousins move to Sessions)
          </button>
        </div>
      )}
      {contextMenu && (
        <div
          onClick={(e) => e.stopPropagation()}
          style={{
            position: "fixed", left: contextMenu.x, top: contextMenu.y,
            background: "var(--bg-1)", border: "1px solid var(--line)",
            borderRadius: 4, padding: 6, zIndex: 9000,
            boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
            display: "flex", flexDirection: "column", gap: 2, minWidth: 180,
          }}>
          <div style={{ fontSize: 10, color: "var(--fg-3)", padding: "2px 8px", fontFamily: "var(--mono)" }}>
            move @{contextMenu.slug} to:
          </div>
          {groups.map(g => (
            <button key={g.id} className="btn ghost"
                    onClick={() => moveTo(contextMenu.slug, g.id)}
                    style={{
                      fontSize: 12, padding: "4px 10px", textAlign: "left",
                      background: groupOf(contextMenu.slug) === g.id ? "var(--bg-0)" : "transparent",
                    }}>
              {groupOf(contextMenu.slug) === g.id ? "● " : "  "}{g.name}
            </button>
          ))}
          <div style={{ borderTop: "1px solid var(--line)", margin: "4px 0" }} />
          <button className="btn ghost"
                  onClick={() => newGroupAndMove(contextMenu.slug)}
                  style={{ fontSize: 12, padding: "4px 10px", textAlign: "left", color: "var(--accent)" }}>
            + new group...
          </button>
        </div>
      )}
    </>
  );
}

// The login form, shown when the users file is configured and the
// browser holds no session (docs/reference/console-api.md, "The auth model").
function LoginScreen({ onLogin }) {
  const [user, setUser] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const submit = async (e) => {
    if (e) e.preventDefault();
    if (!user.trim() || !password || busy) return;
    setBusy(true); setErr(null);
    try {
      const { r, d } = await apiSend("POST", "/api/auth/login", { user: user.trim(), password });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      onLogin(d.user);
    } catch (e2) {
      setErr(String(e2.message || e2));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="login-screen" style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg-0)" }}>
      <form className="panel" onSubmit={submit} style={{ width: "min(360px, 92vw)" }}>
        <div className="panel-hdr"><span className="title">console login</span></div>
        <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10, padding: 18 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" }}>
            user
            <input className="txt" autoFocus value={user} onChange={e => setUser(e.target.value)} autoComplete="username" />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" }}>
            password
            <input className="txt" type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" />
          </label>
          {err && <div style={{ color: "var(--red)", fontSize: 11, fontFamily: "var(--mono)" }}>{err}</div>}
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button className="btn primary" type="submit" disabled={busy || !user.trim() || !password}>
              {busy ? "..." : "log in"}
            </button>
          </div>
          <div style={{ fontSize: 10, color: "var(--fg-3)", fontFamily: "var(--mono)" }}>
            users are provisioned with <code>cousin-console adduser &lt;name&gt;</code>
          </div>
        </div>
      </form>
    </div>
  );
}

function App() {
  // Query-param overrides for deep links and embedding:
  //   ?view=chat            land on the chat view, not overview
  //   ?cousin=<slug>        set the active cousin
  //   ?user=<name>          the chat view fetches this user's thread
  //   ?embed=1              hide the chrome (sidebar + topbar), chat fills the viewport
  //   ?meeting=<id>         with ?view=meetings, open that meeting's thread
  const urlParams = typeof window !== "undefined"
    ? new URLSearchParams(window.location.search) : new URLSearchParams();
  const initialView = urlParams.get("view") || "overview";
  const initialCousin = urlParams.get("cousin") || "";
  const embedUser = urlParams.get("user") || "";
  const embedMode = urlParams.get("embed") === "1";
  const initialMeeting = urlParams.get("meeting") || "";

  // The release this console runs (GET /api/version, public): shown dim
  // beside the brand. Read at console start, so a bumped or pulled
  // checkout that was not restarted still shows the old value.
  const [build, setBuild] = React.useState(null);
  React.useEffect(() => {
    apiGet("/api/version").then(d => { if (d && d.version) setBuild(d); });
  }, []);

  // Auth: null until /api/auth/me answers; then {user, configured, users}.
  const [auth, setAuth] = useStateApp(null);
  useEffectApp(() => {
    let cancelled = false;
    fetchAuthMe().then(me => { if (!cancelled) setAuth(me); });
    const onRequired = () => setAuth(a => a ? { ...a, user: null, configured: true } : a);
    window.addEventListener("console-auth-required", onRequired);
    return () => { cancelled = true; window.removeEventListener("console-auth-required", onRequired); };
  }, []);
  const needLogin = !!(auth && auth.configured && !auth.user);
  const sessionUser = (auth && auth.user) || "";

  // Embed mode: pin fullscreen chat class on body so the chrome is hidden
  // (reuses the body.chat-fullscreen CSS).
  useEffectApp(() => {
    if (!embedMode) return;
    document.body.classList.add("chat-fullscreen", "chat-embed");
    try { localStorage.setItem("console_chat_fullscreen", "1"); } catch (_e) {}
    return () => document.body.classList.remove("chat-fullscreen", "chat-embed");
  }, [embedMode]);

  // Every page load lands on Overview - no persistence. User navigates from there.
  const [view, setView] = useStateApp(initialView);
  const [cousins, setCousins] = useStateApp([]);
  const [loops, setLoops] = useStateApp([]);
  const [activeCousin, setActiveCousin] = useStateApp(initialCousin);
  const [clock, setClock] = useStateApp(() => new Date());
  // Reveal cousins/loops marked hidden=true in cousin.toml. Drives sidebar
  // and CousinsView filters; mirrors the Settings visibility toggle.
  const showHidden = (window.useSetting && window.useSetting("showHidden")) || false;
  // On mobile, sidebar starts collapsed. Desktop remembers last state.
  const [sidebarCollapsed, setSidebarCollapsed] = useStateApp(() => {
    if (typeof window !== "undefined" && window.innerWidth <= 820) return true;
    try { return localStorage.getItem("console_sidebar_collapsed") === "1"; }
    catch (e) { return false; }
  });

  // The default active cousin is the first one in the fleet, else none.
  useEffectApp(() => {
    if (activeCousin || !cousins.length) return;
    const first = cousins.find(c => c.type !== "worker" && (showHidden || !c.hidden)) || cousins[0];
    if (first) setActiveCousin(first.slug);
  }, [cousins, activeCousin, showHidden]);

  // The chat user for a cousin: ?user=, else the cousin's operator, else
  // the session user. Null when none of the three exists (the chat view
  // then disables its composer and says why).
  const chatUserFor = (c) => embedUser || (c && c.operator) || sessionUser || "";

  // Narrow-screen detection drives auto-close behaviour on nav selection.
  const [isNarrow, setIsNarrow] = useStateApp(() =>
    typeof window !== "undefined" ? window.innerWidth <= 820 : false);
  useEffectApp(() => {
    const onResize = () => setIsNarrow(window.innerWidth <= 820);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Picking a view on mobile should auto-close the sidebar overlay.
  const pickView = (v) => {
    setView(v);
    if (isNarrow) setSidebarCollapsed(true);
  };

  useEffectApp(() => {
    try { localStorage.setItem("console_sidebar_collapsed", sidebarCollapsed ? "1" : "0"); }
    catch (e) { /* private mode / quota / disabled - ignore */ }
  }, [sidebarCollapsed]);

  // The clock is pure client-side; live data arrives over SSE below.
  useEffectApp(() => {
    const id = setInterval(() => setClock(new Date()), 3000);
    return () => clearInterval(id);
  }, []);

  // SSE event stream: the server sends a full snapshot on connect, then
  // deltas (docs/reference/console-api.md, "/api/events"). Reconnect backs off from
  // 1 s to 15 s; a reconnect costs a fresh snapshot, never data.
  useEffectApp(() => {
    if (needLogin) return;
    let es = null;
    let cancelled = false;
    let backoffMs = 1000;
    function applyEvent(payload) {
      if (!payload || cancelled) return;
      const { kind, data } = payload;
      if (kind === "snapshot") {
        if (data.cousins) setCousins(data.cousins);
        if (data.loops) setLoops(data.loops);
      } else if (kind === "cousins-refresh") {
        // Periodic full refresh so derived state like lastMsgTs (drives
        // the unread dot) stays current.
        if (Array.isArray(data)) setCousins(data);
      } else if (kind === "loops-refresh") {
        if (Array.isArray(data)) setLoops(data);
      } else if (kind === "cousin-status") {
        // Optimistic patch; the next refresh confirms.
        setCousins(cs => cs.map(c => c.slug === data.slug ? { ...c, status: data.status } : c));
      } else if (kind === "job-add" || kind === "job-update" || kind === "job-delete") {
        // The jobs view polls its own route; nothing to do at the top level.
      } else if (kind === "cousin-flip") {
        // Re-dispatch as a DOM CustomEvent so FlipModal in cousins.jsx
        // can subscribe without taking a SSE handle of its own.
        window.dispatchEvent(new CustomEvent("fw-cousin-flip", { detail: data }));
      } else if (kind === "meeting-change") {
        // Re-dispatched like cousin-flip: the Meetings view refetches
        // its list and the open thread without a SSE handle of its own.
        window.dispatchEvent(new CustomEvent("fw-meeting-change", { detail: data }));
      } else if (kind === "tracker-change" || kind === "loop-fire") {
        // Views that care poll their own routes.
      }
    }
    function open() {
      if (cancelled) return;
      try {
        es = new EventSource("/api/events");
        es.onmessage = (ev) => {
          try {
            applyEvent(JSON.parse(ev.data));
            backoffMs = 1000;
          } catch (_) { /* ignore parse errors */ }
        };
        es.onerror = () => {
          try { es.close(); } catch (_) {}
          if (!cancelled) {
            setTimeout(open, Math.min(backoffMs, 15000));
            backoffMs = Math.min(backoffMs * 2, 15000);
          }
        };
      } catch (e) {
        console.error("EventSource failed, no live updates:", e);
      }
    }
    open();
    return () => {
      cancelled = true;
      try { if (es) es.close(); } catch (_) {}
    };
  }, [needLogin]);

  // Keyboard nav: Ctrl/Cmd + the digit on each NAV entry.
  useEffectApp(() => {
    const onKey = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (e.metaKey || e.ctrlKey) {
        const hit = NAV.find(n => n.kbd === e.key);
        if (hit) { e.preventDefault(); setView(hit.id); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Restart-in-flight banner: fires when the Settings process-control
  // panel dispatches a 'fw-restart' CustomEvent. We poll /api/cousins every
  // 800ms until the console is up, then hide.
  const [restartState, setRestartState] = useStateApp(null);
  useEffectApp(() => {
    const onRestart = (ev) => {
      const d = ev.detail || {};
      setRestartState({ target: d.target || "console", startedAt: Date.now(), ready: false });
      let tries = 0;
      // Bumps an internal counter every second so the banner's "Ns" elapsed
      // display actually updates during the poll.
      const tickId = setInterval(() => setRestartState(s => s && !s.ready && { ...s, _t: Date.now() }), 1000);
      const id = setInterval(async () => {
        tries += 1;
        try {
          const r = await fetch("/api/cousins", { cache: "no-store" });
          if (r.ok) {
            clearInterval(id); clearInterval(tickId);
            setRestartState(s => s && { ...s, ready: true });
            setTimeout(() => setRestartState(null), 2500);
            return;
          }
        } catch (_e) { /* ignore */ }
        if (tries > 60) { clearInterval(id); clearInterval(tickId); setRestartState(null); }
      }, 800);
    };
    window.addEventListener("fw-restart", onRestart);
    return () => window.removeEventListener("fw-restart", onRestart);
  }, []);

  // One reading of the fleet for the sidebar: how many cousins wait on a
  // person (the Overview entry's count) and when the next daily flip is
  // (the card at the sidebar's foot). Both from the rows the SSE keeps.
  const health = window.fleetHealth
    ? fleetHealth(cousins.filter(c => showHidden || !c.hidden), clock.getTime()) : null;
  const nextFlipAt = health && health.nextFlip;

  if (auth === null) {
    return <div style={{ padding: 20, fontFamily: "var(--mono)", color: "var(--fg-3)" }}>connecting...</div>;
  }
  if (needLogin) {
    return <LoginScreen onLogin={(user) => setAuth(a => ({ ...(a || {}), user, configured: true }))} />;
  }

  return (
    <div className={`shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <header className="topbar">
        <button
          className="btn ghost"
          onClick={() => setSidebarCollapsed(v => !v)}
          title={sidebarCollapsed ? "expand sidebar" : "collapse sidebar"}
          style={{ minHeight: 24, padding: "0 6px", fontSize: 12, marginRight: 4 }}
        >{sidebarCollapsed ? "›" : "‹"}</button>
        <img className="brand-icon" src="favicon.svg" alt="" aria-hidden="true" />
        <span className="brand">cousins<span className="dim">//</span>console</span>
        {build && <span className="build">{build.repo_url ? <a href={build.repo_url} title={build.repo_url} target="_blank" rel="noopener noreferrer">v{build.version}</a> : `v${build.version}`}{build.commit ? " " : ""}{build.commit ? (build.commit_url ? <a href={build.commit_url} title={build.commit_url} target="_blank" rel="noopener noreferrer">{build.commit}</a> : build.commit) : ""}</span>}
        <span className="spacer" />
        <button
          className="btn ghost"
          onClick={() => {
            const cur = (window.__fwSettings || {});
            const next = { ...cur, showHidden: !cur.showHidden };
            try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(next)); } catch (_e) {}
            if (typeof window.applySettings === "function") window.applySettings(next);
          }}
          title={showHidden ? "showing hidden cousins + loops (click to hide)" : "show hidden cousins + loops"}
          style={{ minHeight: 24, padding: "0 8px", marginRight: 8, color: showHidden ? "var(--accent)" : "var(--fg-3)" }}
        >{showHidden ? I.eye : I.eyeOff}</button>
        {sessionUser && <span className="stat" title="session user">{sessionUser}</span>}
        <span className="stat">{clock.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
      </header>

      <nav className="sidebar">
        <div className="section-label">framework</div>
        {NAV.map(n => (
          <div key={n.id} className={`navitem ${view === n.id ? "active" : ""}`} onClick={() => pickView(n.id)}
               title={`ctrl/cmd + ${n.kbd}`}>
            <span className="icon">{n.icon}</span>
            <span>{n.label}</span>
            {n.id === "overview" && health && health.needs > 0 && (
              <span className="nav-count" title={`${health.needs} cousin${health.needs === 1 ? "" : "s"} waiting on a person`}>{health.needs}</span>
            )}
          </div>
        ))}
        <SidebarGroups
          cousins={cousins}
          activeCousin={activeCousin}
          view={view}
          showHidden={showHidden}
          chatUserFor={chatUserFor}
          onPick={(slug) => { setActiveCousin(slug); pickView("chat"); }}
        />

        <div style={{ flex: 1 }} />
        {nextFlipAt && (
          <div className="flip-card" data-next-flip
               title="the earliest [lifecycle] flip_at among the running cousins, on this browser's clock">
            <div className="fc-label">next flip</div>
            <div className="fc-time">{nextFlipAt.at}<span className="fc-in">in {fleetIn(nextFlipAt.inSec)}</span></div>
            <div className="fc-sub">
              {nextFlipAt.slugs.map(s => "@" + s).join(", ")}
              {nextFlipAt.onDefault ? ` · ${nextFlipAt.onDefault} on the install default` : ""}
            </div>
          </div>
        )}
      </nav>

      <main className="main">
        <MainHeader view={view} cousins={cousins} activeCousin={activeCousin} />
        <div className="main-body">
          {view === "chat"     && window.ChatView && <ChatView activeCousin={activeCousin} cousins={cousins} embedUser={embedUser} sessionUser={sessionUser} embed={embedMode} />}
          {view === "cousins"  && <CousinsView cousins={cousins} setCousins={setCousins} setActiveCousin={(s) => { setActiveCousin(s); pickView("chat"); }} />}
          {view === "jobs"     && <JobsView />}
          {view === "memory"   && <MemoryView />}
          {view === "loops"    && <LoopsView loops={loops} />}
          {view === "tokens"   && <TokensView cousins={cousins} />}
          {view === "tracker"  && <TrackerView cousins={cousins} />}
          {view === "meetings" && window.MeetingsView && <MeetingsView cousins={cousins} sessionUser={sessionUser} initialMeeting={initialMeeting} />}
          {view === "settings" && <SettingsView auth={auth} setAuth={setAuth} />}
          {view === "overview" && <HostView onOpen={(slug) => { setActiveCousin(slug); pickView("chat"); }} />}
        </div>
      </main>

      {restartState && (
        <div style={{
          position: "fixed", left: 16, right: 16, bottom: 40, zIndex: 50,
          background: restartState.ready
            ? "oklch(from var(--green) l c h / 0.16)"
            : "oklch(from var(--amber) l c h / 0.16)",
          border: `1px solid ${restartState.ready ? "var(--green)" : "var(--amber)"}`,
          borderRadius: 4, padding: "10px 14px",
          fontFamily: "var(--mono)", fontSize: 12,
          color: restartState.ready ? "var(--green)" : "var(--amber)",
          display: "flex", gap: 10, alignItems: "center",
        }}>
          <span>{restartState.ready ? "✓" : "⟳"}</span>
          <span>
            {restartState.ready
              ? `${restartState.target} back up`
              : `restarting ${restartState.target}... ${Math.round((Date.now() - restartState.startedAt) / 1000)}s`}
          </span>
        </div>
      )}

    </div>
  );
}

function MainHeader({ view, cousins, activeCousin }) {
  const c = cousins.find(x => x.slug === activeCousin);
  // The cousin's state in words beside its dot (views.jsx fleetState).
  const st = c && window.fleetState ? fleetState(c) : null;
  const titles = {
    chat: c ? (c.name || c.slug) : "Chat",
    cousins: "Cousins",
    jobs: "Jobs",
    memory: "Memory",
    loops: "Loops",
    tokens: "Tokens",
    tracker: "Tracker",
    meetings: "Meetings",
    settings: "Settings",
    overview: "Overview",
  };
  const paths = {
    chat: "/console/chat/@" + (activeCousin || "-"),
    cousins: "/console/cousins",
    jobs: "/console/jobs",
    memory: "/console/memory",
    loops: "/console/loops",
    tokens: "/console/tokens",
    tracker: "/console/tracker",
    meetings: "/console/meetings",
    settings: "/console/settings",
    overview: "/console/host",
  };
  return (
    <div className="main-header">
      <h1>{titles[view] || view}</h1>
      <span className="path">{paths[view] || ""}</span>
      <span className="spacer" />
      {view === "chat" && c && (
        <span className="hdr-meta">
          <span className={"led " + (st ? st.tone : "gray") + (st && st.pulse ? " pulse" : "")} />
          <span className={"fleet-state tone-" + (st ? st.tone : "gray")}>{st ? st.word : c.status}</span> · {c.slug}{c.model ? ` · ${c.model}` : ""} · heartbeat {c.heartbeat}s{c.chat === "down" ? " · chat server down" : ""}
        </span>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
