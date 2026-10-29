const { useState: useStateApp, useEffect: useEffectApp } = React;
const NAV = [
  { id: "overview", label: "Overview", icon: I.host, kbd: "1" },
  { id: "cousins", label: "Cousins", icon: I.cousins, kbd: "2" },
  { id: "jobs", label: "Jobs", icon: I.list, kbd: "3" },
  { id: "memory", label: "Memory", icon: I.memory, kbd: "4" },
  { id: "tokens", label: "Tokens", icon: I.tokens, kbd: "6" },
  { id: "tracker", label: "Tracker", icon: I.tracker, kbd: "7" },
  { id: "meetings", label: "Meetings", icon: I.meetings, kbd: "8" }
];
const SYSTEM_TAB_OF = { loops: "loops", settings: "settings", accounts: "accounts" };
const SYSTEM_TAB_KEYS = { "5": "loops", "0": "settings" };
function startView() {
  const p = new URLSearchParams(window.location.search);
  const asked = p.get("view") || "overview";
  const tab = SYSTEM_TAB_OF[asked];
  if (!tab) return asked;
  p.set("view", "system");
  p.set("tab", tab);
  window.history.replaceState(window.history.state, "", window.location.pathname + "?" + p + window.location.hash);
  return "system";
}
function openSystemTab(tab, setView) {
  const p = new URLSearchParams(window.location.search);
  p.set("tab", tab);
  window.history.replaceState(window.history.state, "", window.location.pathname + "?" + p + window.location.hash);
  window.dispatchEvent(new CustomEvent("fw-system-tab", { detail: tab }));
  setView("system");
}
function navEntries() {
  const extra = (window.registeredViews ? registeredViews() : []).map((v) => ({ id: v.id, label: v.label, icon: v.icon || I.list, kbd: v.kbd || "" }));
  return NAV.concat(extra);
}
const SIDEBAR_KEY = "console_sidebar_v1";
const READ_MOVED_KEY = "console_chat_read_moved";
const DEFAULT_GROUPS = [{ id: "sessions", name: "Sessions", collapsed: false }];
function loadSidebar() {
  try {
    const raw = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || "null");
    if (raw && Array.isArray(raw.groups) && raw.groups.length) return raw;
  } catch (_e) {
  }
  return { groups: DEFAULT_GROUPS.slice(), assignments: {} };
}
function SidebarGroups({ cousins, activeCousin, view, showHidden, unread, onPick }) {
  const [config, setConfig] = React.useState(loadSidebar);
  const [contextMenu, setContextMenu] = React.useState(null);
  const [groupMenu, setGroupMenu] = React.useState(null);
  const [draggingSlug, setDraggingSlug] = React.useState(null);
  const [draggingGroup, setDraggingGroup] = React.useState(null);
  const cache = (value) => {
    try {
      localStorage.setItem(SIDEBAR_KEY, JSON.stringify(value));
    } catch (_e) {
    }
  };
  const save = (value) => apiSend("POST", "/api/prefs/sidebar", { sidebar: value }).catch((e) => console.error("sidebar save failed:", e));
  const persist = (next) => {
    const merged = { groups: next.groups || config.groups, assignments: next.assignments || config.assignments };
    setConfig(merged);
    cache(merged);
    save(merged);
  };
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
        const custom = local.groups.length > 1 || Object.keys(local.assignments || {}).length || local.groups[0].name !== DEFAULT_GROUPS[0].name;
        if (custom) save(local);
      }
    };
    pull();
    const onVisible = () => {
      if (document.visibilityState === "visible") pull();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  const groups = config.groups.length ? config.groups : DEFAULT_GROUPS;
  const assignments = config.assignments || {};
  const visible = (cousins || []).filter((c) => (showHidden || !c.hidden) && c.type !== "worker");
  const groupOf = (slug) => assignments[slug] || "sessions";
  const cousinsByGroup = {};
  for (const g of groups) cousinsByGroup[g.id] = [];
  for (const c of visible) {
    const gid = cousinsByGroup[groupOf(c.slug)] !== void 0 ? groupOf(c.slug) : groups[0].id;
    cousinsByGroup[gid].push(c);
  }
  const setCollapsed = (gid, collapsed) => {
    persist({ ...config, groups: groups.map((g) => g.id === gid ? { ...g, collapsed } : g) });
  };
  const moveTo = (slug, gid) => {
    persist({ ...config, assignments: { ...assignments, [slug]: gid } });
    setContextMenu(null);
  };
  const newGroupAndMove = (slug) => {
    const name = window.prompt("new group name:");
    if (!name || !name.trim()) return;
    const id = name.trim().toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "").slice(0, 32) || `g${Date.now()}`;
    if (groups.some((g) => g.id === id)) {
      moveTo(slug, id);
      return;
    }
    persist({
      groups: [...groups, { id, name: name.trim(), collapsed: false }],
      assignments: { ...assignments, [slug]: id }
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
    persist({ groups: groups.filter((g) => g.id !== gid), assignments: newAssignments });
  };
  const reorderGroup = (fromId, toId) => {
    if (fromId === toId) return;
    const fromIdx = groups.findIndex((g) => g.id === fromId);
    const toIdx = groups.findIndex((g) => g.id === toId);
    if (fromIdx < 0 || toIdx < 0) return;
    const next = groups.slice();
    const [moved] = next.splice(fromIdx, 1);
    next.splice(toIdx, 0, moved);
    persist({ ...config, groups: next });
  };
  const renameGroup = (gid) => {
    const cur = groups.find((g) => g.id === gid);
    if (!cur) return;
    const name = window.prompt("rename group:", cur.name);
    if (!name || !name.trim() || name === cur.name) return;
    persist({ ...config, groups: groups.map((g) => g.id === gid ? { ...g, name: name.trim() } : g) });
  };
  React.useEffect(() => {
    if (!contextMenu && !groupMenu) return;
    const close = () => {
      setContextMenu(null);
      setGroupMenu(null);
    };
    const onKey = (e) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [contextMenu, groupMenu]);
  const renderCousin = (c) => {
    const stopped = c.status !== "running";
    const viewing = activeCousin === c.slug && view === "chat";
    const n = viewing ? 0 : (unread || {})[c.slug] || 0;
    return /* @__PURE__ */ React.createElement(
      "div",
      {
        key: c.slug,
        className: `navitem ${activeCousin === c.slug && view === "chat" ? "active" : ""} ${stopped ? "disabled" : ""}`,
        draggable: true,
        onDragStart: (e) => {
          setDraggingSlug(c.slug);
          e.dataTransfer.effectAllowed = "move";
        },
        onDragEnd: () => setDraggingSlug(null),
        onContextMenu: (e) => {
          e.preventDefault();
          setContextMenu({ slug: c.slug, x: e.clientX, y: e.clientY });
        },
        onClick: stopped ? void 0 : () => onPick(c.slug),
        title: stopped ? `@${c.slug} is stopped - start it from the Cousins view first` : `open chat with @${c.slug}` + (n ? ` (${n} unread)` : "") + " (right-click to move)",
        style: stopped || c.hidden ? { opacity: stopped ? 0.5 : 0.7, cursor: stopped ? "not-allowed" : "grab" } : { cursor: "grab" }
      },
      /* @__PURE__ */ React.createElement(CousinLed, { c }),
      /* @__PURE__ */ React.createElement("span", { style: { fontWeight: n ? 600 : 400 } }, (c.name || c.slug).toLowerCase()),
      n > 0 && /* @__PURE__ */ React.createElement("span", { className: "unread-count", title: `${n} unread repl${n === 1 ? "y" : "ies"}` }, n > 9 ? "9+" : n),
      c.hidden && /* @__PURE__ */ React.createElement("span", { className: "badge", title: "hidden (revealed by Settings: show hidden)", style: { background: "oklch(from var(--fg-3) l c h / 0.25)" } }, "hidden")
    );
  };
  return /* @__PURE__ */ React.createElement(React.Fragment, null, groups.map((g) => {
    const list = cousinsByGroup[g.id] || [];
    return /* @__PURE__ */ React.createElement(React.Fragment, { key: g.id }, /* @__PURE__ */ React.createElement(
      "div",
      {
        draggable: true,
        onDragStart: (e) => {
          setDraggingGroup(g.id);
          e.dataTransfer.effectAllowed = "move";
        },
        onDragEnd: () => setDraggingGroup(null),
        onDragOver: (e) => {
          if (draggingSlug || draggingGroup && draggingGroup !== g.id) e.preventDefault();
        },
        onDrop: (e) => {
          e.preventDefault();
          if (draggingSlug) moveTo(draggingSlug, g.id);
          else if (draggingGroup && draggingGroup !== g.id) reorderGroup(draggingGroup, g.id);
          setDraggingSlug(null);
          setDraggingGroup(null);
        },
        className: "section-label",
        style: {
          marginTop: 16,
          display: "flex",
          alignItems: "center",
          gap: 6,
          cursor: "grab",
          userSelect: "none",
          opacity: draggingGroup === g.id ? 0.4 : 1
        },
        onClick: () => setCollapsed(g.id, !g.collapsed),
        onContextMenu: (e) => {
          e.preventDefault();
          if (g.id === "sessions") return;
          setGroupMenu({ gid: g.id, x: e.clientX, y: e.clientY });
        },
        title: g.id === "sessions" ? "click to collapse, drag to reorder" : "click to collapse, drag to reorder, right-click for rename/delete, drop a cousin to move them here"
      },
      /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 9 } }, g.collapsed ? "▸" : "▾"),
      /* @__PURE__ */ React.createElement("span", null, g.name.toLowerCase()),
      list.length > 0 && /* @__PURE__ */ React.createElement("span", { style: { marginLeft: "auto", color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 10 } }, list.length)
    ), !g.collapsed && list.map(renderCousin));
  }), groupMenu && /* @__PURE__ */ React.createElement(
    "div",
    {
      onClick: (e) => e.stopPropagation(),
      style: {
        position: "fixed",
        left: groupMenu.x,
        top: groupMenu.y,
        background: "var(--bg-1)",
        border: "1px solid var(--line)",
        borderRadius: 10,
        padding: 6,
        zIndex: 9e3,
        boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
        display: "flex",
        flexDirection: "column",
        gap: 2,
        minWidth: 160
      }
    },
    /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, color: "var(--fg-3)", padding: "2px 8px", fontFamily: "var(--mono)" } }, "group: ", (groups.find((g) => g.id === groupMenu.gid) || {}).name),
    /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: () => {
          renameGroup(groupMenu.gid);
          setGroupMenu(null);
        },
        style: { fontSize: 12, padding: "4px 10px", textAlign: "left" }
      },
      "rename..."
    ),
    /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: () => {
          deleteGroup(groupMenu.gid);
          setGroupMenu(null);
        },
        style: { fontSize: 12, padding: "4px 10px", textAlign: "left", color: "var(--err, #ef4444)" }
      },
      "delete (cousins move to Sessions)"
    )
  ), contextMenu && /* @__PURE__ */ React.createElement(
    "div",
    {
      onClick: (e) => e.stopPropagation(),
      style: {
        position: "fixed",
        left: contextMenu.x,
        top: contextMenu.y,
        background: "var(--bg-1)",
        border: "1px solid var(--line)",
        borderRadius: 10,
        padding: 6,
        zIndex: 9e3,
        boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
        display: "flex",
        flexDirection: "column",
        gap: 2,
        minWidth: 180
      }
    },
    /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, color: "var(--fg-3)", padding: "2px 8px", fontFamily: "var(--mono)" } }, "move @", contextMenu.slug, " to:"),
    groups.map((g) => /* @__PURE__ */ React.createElement(
      "button",
      {
        key: g.id,
        className: "btn ghost",
        onClick: () => moveTo(contextMenu.slug, g.id),
        style: {
          fontSize: 12,
          padding: "4px 10px",
          textAlign: "left",
          background: groupOf(contextMenu.slug) === g.id ? "var(--bg-0)" : "transparent"
        }
      },
      groupOf(contextMenu.slug) === g.id ? "● " : "  ",
      g.name
    )),
    /* @__PURE__ */ React.createElement("div", { style: { borderTop: "1px solid var(--line)", margin: "4px 0" } }),
    /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: () => newGroupAndMove(contextMenu.slug),
        style: { fontSize: 12, padding: "4px 10px", textAlign: "left", color: "var(--accent)" }
      },
      "+ new group..."
    )
  ));
}
function LoginScreen({ onLogin }) {
  const [user, setUser] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const submit = async (e) => {
    if (e) e.preventDefault();
    if (!user.trim() || !password || busy) return;
    setBusy(true);
    setErr(null);
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
  return /* @__PURE__ */ React.createElement("div", { className: "login-screen", style: { minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg-0)" } }, /* @__PURE__ */ React.createElement("form", { className: "panel", onSubmit: submit, style: { width: "min(360px, 92vw)", paddingTop: 22 } }, /* @__PURE__ */ React.createElement("img", { className: "login-mark", src: "favicon.svg", alt: "", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "console login")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 10, padding: 18 } }, /* @__PURE__ */ React.createElement("label", { style: { display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" } }, "user", /* @__PURE__ */ React.createElement("input", { className: "txt", autoFocus: true, value: user, onChange: (e) => setUser(e.target.value), autoComplete: "username" })), /* @__PURE__ */ React.createElement("label", { style: { display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" } }, "password", /* @__PURE__ */ React.createElement("input", { className: "txt", type: "password", value: password, onChange: (e) => setPassword(e.target.value), autoComplete: "current-password" })), err && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--red)", fontSize: 11, fontFamily: "var(--mono)" } }, err), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", justifyContent: "flex-end" } }, /* @__PURE__ */ React.createElement("button", { className: "btn primary", type: "submit", disabled: busy || !user.trim() || !password }, busy ? "..." : "log in")), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, color: "var(--fg-3)", fontFamily: "var(--mono)" } }, "users are provisioned with ", /* @__PURE__ */ React.createElement("code", null, "cousin-console adduser <name>")))));
}
const HEALTH_POLL_MS = 3e4;
function healthWhen(ts) {
  if (!ts) return "-";
  return new Date(ts * 1e3).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}
function HealthBadge() {
  const [h, setH] = useStateApp(null);
  const [open, setOpen] = useStateApp(false);
  useEffectApp(() => {
    let cancelled = false;
    const load = () => apiGet("/api/health").then((d) => {
      if (!cancelled && d) setH(d);
    });
    load();
    const id = setInterval(load, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);
  if (!h || !h.failing_count) return null;
  const children = h.supervisor && h.supervisor.failing || [];
  return /* @__PURE__ */ React.createElement("span", { className: "health-badge", "data-health-badge": true }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost health-count",
      onClick: () => setOpen((v) => !v),
      title: `${h.failing_count} failing (cousin-health)`
    },
    /* @__PURE__ */ React.createElement("span", { className: "led red" }),
    " ",
    h.failing_count,
    " failing"
  ), open && /* @__PURE__ */ React.createElement("div", { className: "health-list", "data-health-list": true }, h.failing.map((r) => /* @__PURE__ */ React.createElement("div", { key: r.key, className: "health-row" }, /* @__PURE__ */ React.createElement("b", null, r.key), " ", r.fails, "x since ", healthWhen(r.since), r.quiet ? ` (not seen since ${healthWhen(r.seen)})` : "", r.error && /* @__PURE__ */ React.createElement("div", { className: "health-error" }, r.error))), children.map((c) => /* @__PURE__ */ React.createElement("div", { key: c.name, className: "health-row" }, /* @__PURE__ */ React.createElement("b", null, c.name), " ", c.state, " since ", c.since || "-", c.reason && /* @__PURE__ */ React.createElement("div", { className: "health-error" }, c.reason)))));
}
function App() {
  const urlParams = typeof window !== "undefined" ? new URLSearchParams(window.location.search) : new URLSearchParams();
  const initialCousin = urlParams.get("cousin") || "";
  const embedUser = urlParams.get("user") || "";
  const embedMode = urlParams.get("embed") === "1";
  const initialMeeting = urlParams.get("meeting") || "";
  const [build, setBuild] = React.useState(null);
  React.useEffect(() => {
    apiGet("/api/version").then((d) => {
      if (d && d.version) setBuild(d);
    });
  }, []);
  const [auth, setAuth] = useStateApp(null);
  useEffectApp(() => {
    let cancelled = false;
    fetchAuthMe().then((me) => {
      if (!cancelled) setAuth(me);
    });
    const onRequired = () => setAuth((a) => a ? { ...a, user: null, configured: true } : a);
    window.addEventListener("console-auth-required", onRequired);
    return () => {
      cancelled = true;
      window.removeEventListener("console-auth-required", onRequired);
    };
  }, []);
  const needLogin = !!(auth && auth.configured && !auth.user);
  const sessionUser = auth && auth.user || "";
  useEffectApp(() => {
    if (!embedMode) return;
    document.body.classList.add("chat-embed");
    return () => document.body.classList.remove("chat-embed");
  }, [embedMode]);
  const [view, setView] = useStateApp(startView);
  const [cousins, setCousins] = useStateApp([]);
  const [loops, setLoops] = useStateApp([]);
  const [activeCousin, setActiveCousin] = useStateApp(initialCousin);
  const [unread, setUnread] = useStateApp(null);
  useEffectApp(() => {
    const p = new URLSearchParams(window.location.search);
    p.set("view", view);
    if (activeCousin) p.set("cousin", activeCousin);
    else p.delete("cousin");
    if (view !== "meetings") p.delete("meeting");
    if (view !== "system") p.delete("tab");
    const next = "?" + p.toString();
    if (next !== window.location.search) {
      window.history.replaceState(
        window.history.state,
        "",
        window.location.pathname + next + window.location.hash
      );
    }
  }, [view, activeCousin]);
  const [clock, setClock] = useStateApp(() => /* @__PURE__ */ new Date());
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  const [sidebarCollapsed, setSidebarCollapsed] = useStateApp(() => {
    if (typeof window !== "undefined" && window.innerWidth <= 820) return true;
    try {
      return localStorage.getItem("console_sidebar_collapsed") === "1";
    } catch (e) {
      return false;
    }
  });
  useEffectApp(() => {
    if (activeCousin || !cousins.length) return;
    const first = cousins.find((c) => c.type !== "worker" && (showHidden || !c.hidden)) || cousins[0];
    if (first) setActiveCousin(first.slug);
  }, [cousins, activeCousin, showHidden]);
  const chatUserFor = (c) => embedUser || c && c.operator || sessionUser || "";
  const [isNarrow, setIsNarrow] = useStateApp(() => typeof window !== "undefined" ? window.innerWidth <= 820 : false);
  useEffectApp(() => {
    const onResize = () => setIsNarrow(window.innerWidth <= 820);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  const pickView = (v) => {
    setView(v);
    if (isNarrow) setSidebarCollapsed(true);
  };
  useEffectApp(() => {
    try {
      localStorage.setItem("console_sidebar_collapsed", sidebarCollapsed ? "1" : "0");
    } catch (e) {
    }
  }, [sidebarCollapsed]);
  useEffectApp(() => {
    const id = setInterval(() => setClock(/* @__PURE__ */ new Date()), 3e3);
    return () => clearInterval(id);
  }, []);
  const refreshUnread = () => {
    apiGet("/api/chat/unread").then((d) => {
      if (d && d.unread) setUnread(d.unread);
    });
  };
  useEffectApp(() => {
    const onCounts = (e) => {
      if (e.detail) setUnread(e.detail);
    };
    window.addEventListener("fw-chat-unread", onCounts);
    return () => window.removeEventListener("fw-chat-unread", onCounts);
  }, []);
  useEffectApp(() => {
    if (!unread || !cousins.length || embedMode) return;
    let moved = false;
    try {
      moved = localStorage.getItem(READ_MOVED_KEY) === "1";
    } catch (_e) {
      return;
    }
    if (moved) return;
    for (const c of cousins) {
      if (!unread[c.slug] || !c.lastMsgId) continue;
      const user = chatUserFor(c);
      let seen = 0;
      try {
        seen = parseInt(localStorage.getItem(chatSeenKey(c.slug, user)) || "0", 10);
      } catch (_e) {
      }
      if (seen && seen >= (c.lastMsgTs || 0)) {
        apiSend("POST", "/api/chat/read", { cousin: c.slug, user, id: c.lastMsgId }).then(({ r, d }) => {
          if (r.ok && d && d.unread) setUnread(d.unread);
        }).catch(() => {
        });
      }
    }
    try {
      Object.keys(localStorage).filter((k) => k.startsWith("console_chat_seen_")).forEach((k) => localStorage.removeItem(k));
      localStorage.setItem(READ_MOVED_KEY, "1");
    } catch (_e) {
    }
  }, [unread, cousins]);
  useEffectApp(() => {
    if (needLogin) return;
    let es = null;
    let cancelled = false;
    let backoffMs = 1e3;
    function applyEvent(payload) {
      if (!payload || cancelled) return;
      const { kind, data } = payload;
      if (kind === "snapshot") {
        if (data.cousins) setCousins(data.cousins);
        if (data.loops) setLoops(data.loops);
        refreshUnread();
      } else if (kind === "cousins-refresh") {
        if (Array.isArray(data)) setCousins(data);
        refreshUnread();
      } else if (kind === "chat-read") {
        refreshUnread();
      } else if (kind === "loops-refresh") {
        if (Array.isArray(data)) setLoops(data);
      } else if (kind === "cousin-status") {
        setCousins((cs) => cs.map((c) => c.slug === data.slug ? { ...c, status: data.status } : c));
      } else if (kind === "job-add" || kind === "job-update" || kind === "job-delete") {
      } else if (kind === "cousin-flip") {
        window.dispatchEvent(new CustomEvent("fw-cousin-flip", { detail: data }));
      } else if (kind === "cousin-op") {
        window.dispatchEvent(new CustomEvent("fw-cousin-op", { detail: data }));
      } else if (kind === "accounts-change") {
        window.dispatchEvent(new CustomEvent("fw-accounts-change", { detail: data }));
      } else if (kind === "meeting-change") {
        window.dispatchEvent(new CustomEvent("fw-meeting-change", { detail: data }));
      } else if (kind === "tracker-change" || kind === "loop-fire") {
      }
    }
    function open() {
      if (cancelled) return;
      try {
        es = new EventSource("/api/events");
        es.onmessage = (ev) => {
          try {
            applyEvent(JSON.parse(ev.data));
            backoffMs = 1e3;
          } catch (_) {
          }
        };
        es.onerror = () => {
          try {
            es.close();
          } catch (_) {
          }
          if (!cancelled) {
            setTimeout(open, Math.min(backoffMs, 15e3));
            backoffMs = Math.min(backoffMs * 2, 15e3);
          }
        };
      } catch (e) {
        console.error("EventSource failed, no live updates:", e);
      }
    }
    open();
    return () => {
      cancelled = true;
      try {
        if (es) es.close();
      } catch (_) {
      }
    };
  }, [needLogin]);
  useEffectApp(() => {
    const onKey = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (e.metaKey || e.ctrlKey) {
        const tab = SYSTEM_TAB_KEYS[e.key];
        if (tab) {
          e.preventDefault();
          openSystemTab(tab, setView);
          return;
        }
        const hit = navEntries().find((n) => n.kbd && n.kbd === e.key);
        if (hit) {
          e.preventDefault();
          setView(hit.id);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const [restartState, setRestartState] = useStateApp(null);
  useEffectApp(() => {
    const onRestart = (ev) => {
      const d = ev.detail || {};
      setRestartState({ target: d.target || "console", startedAt: Date.now(), ready: false });
      let tries = 0;
      const tickId = setInterval(() => setRestartState((s) => s && !s.ready && { ...s, _t: Date.now() }), 1e3);
      const id = setInterval(async () => {
        tries += 1;
        try {
          const r = await fetch("/api/cousins", { cache: "no-store" });
          if (r.ok) {
            clearInterval(id);
            clearInterval(tickId);
            setRestartState((s) => s && { ...s, ready: true });
            setTimeout(() => setRestartState(null), 2500);
            return;
          }
        } catch (_e) {
        }
        if (tries > 60) {
          clearInterval(id);
          clearInterval(tickId);
          setRestartState(null);
        }
      }, 800);
    };
    window.addEventListener("fw-restart", onRestart);
    return () => window.removeEventListener("fw-restart", onRestart);
  }, []);
  const health = window.fleetHealth ? fleetHealth(cousins.filter((c) => showHidden || !c.hidden), clock.getTime()) : null;
  if (auth === null) {
    return /* @__PURE__ */ React.createElement("div", { style: { padding: 20, fontFamily: "var(--mono)", color: "var(--fg-3)" } }, "connecting...");
  }
  if (needLogin) {
    return /* @__PURE__ */ React.createElement(LoginScreen, { onLogin: (user) => setAuth((a) => ({ ...a || {}, user, configured: true })) });
  }
  return /* @__PURE__ */ React.createElement("div", { className: `shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}` }, /* @__PURE__ */ React.createElement("header", { className: "topbar" }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      onClick: () => setSidebarCollapsed((v) => !v),
      title: sidebarCollapsed ? "expand sidebar" : "collapse sidebar",
      style: { minHeight: 24, padding: "0 6px", fontSize: 12, marginRight: 4 }
    },
    sidebarCollapsed ? "›" : "‹"
  ), /* @__PURE__ */ React.createElement("img", { className: "brand-icon", src: "favicon.svg", alt: "", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement("span", { className: "brand" }, "cousins", /* @__PURE__ */ React.createElement("span", { className: "dim" }, "//"), "console"), build && /* @__PURE__ */ React.createElement("span", { className: "build" }, build.repo_url ? /* @__PURE__ */ React.createElement("a", { href: build.repo_url, title: build.repo_url, target: "_blank", rel: "noopener noreferrer" }, "v", build.version) : `v${build.version}`, build.commit ? " " : "", build.commit ? build.commit_url ? /* @__PURE__ */ React.createElement("a", { href: build.commit_url, title: build.commit_url, target: "_blank", rel: "noopener noreferrer" }, build.commit) : build.commit : ""), /* @__PURE__ */ React.createElement("span", { className: "spacer" }), /* @__PURE__ */ React.createElement(HealthBadge, null), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      onClick: () => {
        const cur = window.__fwSettings || {};
        const next = { ...cur, showHidden: !cur.showHidden };
        try {
          localStorage.setItem(SETTINGS_KEY, JSON.stringify(next));
        } catch (_e) {
        }
        if (typeof window.applySettings === "function") window.applySettings(next);
      },
      title: showHidden ? "showing hidden cousins + loops (click to hide)" : "show hidden cousins + loops",
      style: { minHeight: 24, padding: "0 8px", marginRight: 8, color: showHidden ? "var(--accent)" : "var(--fg-3)" }
    },
    showHidden ? I.eye : I.eyeOff
  ), sessionUser && /* @__PURE__ */ React.createElement("span", { className: "stat", title: "session user" }, sessionUser), /* @__PURE__ */ React.createElement("span", { className: "stat" }, clock.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }))), /* @__PURE__ */ React.createElement("nav", { className: "sidebar" }, /* @__PURE__ */ React.createElement("div", { className: "section-label" }, "framework"), navEntries().map((n) => /* @__PURE__ */ React.createElement(
    "div",
    {
      key: n.id,
      className: `navitem ${view === n.id ? "active" : ""}`,
      onClick: () => pickView(n.id),
      title: n.kbd ? `ctrl/cmd + ${n.kbd}` : n.label
    },
    /* @__PURE__ */ React.createElement("span", { className: "icon" }, n.icon),
    /* @__PURE__ */ React.createElement("span", null, n.label),
    n.id === "overview" && health && health.needs > 0 && /* @__PURE__ */ React.createElement("span", { className: "nav-count", title: `${health.needs} cousin${health.needs === 1 ? "" : "s"} waiting on a person` }, health.needs)
  )), /* @__PURE__ */ React.createElement(
    SidebarGroups,
    {
      cousins,
      activeCousin,
      view,
      showHidden,
      unread,
      onPick: (slug) => {
        setActiveCousin(slug);
        pickView("chat");
      }
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } })), /* @__PURE__ */ React.createElement("main", { className: "main" }, /* @__PURE__ */ React.createElement(MainHeader, { view, cousins, activeCousin }), /* @__PURE__ */ React.createElement("div", { className: "main-body" }, view === "chat" && window.ChatView && /* @__PURE__ */ React.createElement(ChatView, { activeCousin, cousins, embedUser, sessionUser, embed: embedMode }), view === "cousins" && /* @__PURE__ */ React.createElement(CousinsView, { cousins, setCousins, setActiveCousin: (s) => {
    setActiveCousin(s);
    pickView("chat");
  } }), view === "jobs" && /* @__PURE__ */ React.createElement(JobsView, null), view === "memory" && /* @__PURE__ */ React.createElement(MemoryView, null), view === "tokens" && /* @__PURE__ */ React.createElement(TokensView, { cousins }), view === "tracker" && /* @__PURE__ */ React.createElement(TrackerView, { cousins }), view === "meetings" && window.MeetingsView && /* @__PURE__ */ React.createElement(MeetingsView, { cousins, sessionUser, initialMeeting }), view === "overview" && /* @__PURE__ */ React.createElement(HostView, { onOpen: (slug) => {
    setActiveCousin(slug);
    pickView("chat");
  } }), registeredView(view) && /* @__PURE__ */ React.createElement(SlotBoundary, { label: "view/" + view }, /* @__PURE__ */ React.createElement(
    SlotEntry,
    {
      entry: registeredView(view),
      props: {
        cousins,
        sessionUser,
        loops,
        auth,
        setAuth,
        onOpen: (slug) => {
          setActiveCousin(slug);
          pickView("chat");
        }
      }
    }
  )))), restartState && /* @__PURE__ */ React.createElement("div", { style: {
    position: "fixed",
    left: 16,
    right: 16,
    bottom: 40,
    zIndex: 50,
    background: restartState.ready ? "oklch(from var(--green) l c h / 0.16)" : "oklch(from var(--amber) l c h / 0.16)",
    border: `1px solid ${restartState.ready ? "var(--green)" : "var(--amber)"}`,
    borderRadius: 4,
    padding: "10px 14px",
    fontFamily: "var(--mono)",
    fontSize: 12,
    color: restartState.ready ? "var(--green)" : "var(--amber)",
    display: "flex",
    gap: 10,
    alignItems: "center"
  } }, /* @__PURE__ */ React.createElement("span", null, restartState.ready ? "✓" : "⟳"), /* @__PURE__ */ React.createElement("span", null, restartState.ready ? `${restartState.target} back up` : `restarting ${restartState.target}... ${Math.round((Date.now() - restartState.startedAt) / 1e3)}s`)));
}
function MainHeader({ view, cousins, activeCousin }) {
  const c = cousins.find((x) => x.slug === activeCousin);
  const st = c && window.fleetState ? fleetState(c) : null;
  const titles = {
    chat: c ? c.name || c.slug : "Chat",
    cousins: "Cousins",
    jobs: "Jobs",
    memory: "Memory",
    tokens: "Tokens",
    tracker: "Tracker",
    meetings: "Meetings",
    overview: "Overview"
  };
  const paths = {
    chat: "/console/chat/@" + (activeCousin || "-"),
    cousins: "/console/cousins",
    jobs: "/console/jobs",
    memory: "/console/memory",
    tokens: "/console/tokens",
    tracker: "/console/tracker",
    meetings: "/console/meetings",
    overview: "/console/host"
  };
  const extra = window.registeredView ? registeredView(view) : null;
  const mobile = window.useMobileLayout ? useMobileLayout() : false;
  return /* @__PURE__ */ React.createElement("div", { className: "main-header" }, /* @__PURE__ */ React.createElement("h1", null, titles[view] || extra && extra.label || view), /* @__PURE__ */ React.createElement("span", { className: "path" }, paths[view] || (extra ? "/console/" + view : "")), /* @__PURE__ */ React.createElement("span", { className: "spacer" }), view === "chat" && c && mobile && /* @__PURE__ */ React.createElement("span", { className: "hdr-meta hdr-meta-m" }, /* @__PURE__ */ React.createElement("span", { className: "led " + (st ? st.tone : "gray") + (st && st.pulse ? " pulse" : "") }), /* @__PURE__ */ React.createElement("span", { className: "fleet-state tone-" + (st ? st.tone : "gray") }, st ? st.word : c.status), c.model && /* @__PURE__ */ React.createElement("span", { className: "hdr-model" }, "· ", c.model), c.chat === "down" && /* @__PURE__ */ React.createElement("span", { className: "hdr-down" }, "· chat server down")), view === "chat" && c && !mobile && /* @__PURE__ */ React.createElement("span", { className: "hdr-meta" }, /* @__PURE__ */ React.createElement("span", { className: "led " + (st ? st.tone : "gray") + (st && st.pulse ? " pulse" : "") }), /* @__PURE__ */ React.createElement("span", { className: "fleet-state tone-" + (st ? st.tone : "gray") }, st ? st.word : c.status), " · ", c.slug, c.model ? ` · ${c.model}` : "", " · heartbeat ", c.heartbeat, "s", c.chat === "down" ? " · chat server down" : ""));
}
ReactDOM.createRoot(document.getElementById("root")).render(/* @__PURE__ */ React.createElement(App, null));
