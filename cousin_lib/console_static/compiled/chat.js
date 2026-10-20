const FRAME_PREFIX = "\x1B[0m\x1B[r\x1B[?1000l\x1B[?1002l\x1B[?1003l\x1B[?1006l\x1B[?25h\x1B[H\x1B[2J\x1B[3J";
function resolveChatUser(embedUser, cousin, sessionUser) {
  return embedUser || cousin && cousin.operator || sessionUser || "";
}
const CHAT_COMPACT_PX = 740;
const SPLIT_SNAP_PX = 40;
function splitSnap(x, left, right, zone = SPLIT_SNAP_PX) {
  if (!(right > left)) return null;
  if (x <= left + zone) return "chat";
  if (x >= right - zone) return "pane";
  return null;
}
async function postChatMessage(body) {
  const r = await fetch("/api/chat/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) return { ok: false, error: d.error || `HTTP ${r.status}` };
  return { ok: true, data: d };
}
function ChatView({ activeCousin, cousins, embedUser, embed, sessionUser }) {
  const c = (cousins || []).find((x) => x.slug === activeCousin);
  const [me, setMe] = React.useState(sessionUser || "");
  React.useEffect(() => {
    if (sessionUser !== void 0) {
      setMe(sessionUser || "");
      return;
    }
    let cancelled = false;
    fetch("/api/auth/me", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((d) => {
      if (!cancelled && d) setMe(d.user || "");
    }).catch(() => {
    });
    return () => {
      cancelled = true;
    };
  }, [sessionUser]);
  const chatUser = resolveChatUser(embedUser, c, me);
  const [paneOpen, setPaneOpen] = React.useState(() => {
    try {
      const v = localStorage.getItem("fw_pane_side");
      if (v === "1" || v === "0") return v === "1";
    } catch (e) {
    }
    return typeof window !== "undefined" && window.innerWidth > 1100;
  });
  const [search, setSearch] = React.useState("");
  const [toast, setToast] = React.useState(null);
  const toastTimerRef = React.useRef(null);
  React.useEffect(() => () => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
  }, []);
  const scheduleToastClear = (ms) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(null), ms);
  };
  const [archiveArmed, setArchiveArmed] = React.useState(false);
  const [showArchived, setShowArchived] = React.useState(false);
  const [fullscreen, setFullscreen] = React.useState(() => {
    try {
      return localStorage.getItem("fw_chat_fullscreen") === "1";
    } catch (e) {
      return false;
    }
  });
  const [mediaShown, setMediaShown] = React.useState(() => readMediaShown());
  React.useEffect(() => {
    writeMediaShown(mediaShown);
  }, [mediaShown]);
  const [paneTab, setPaneTab] = React.useState("pane");
  React.useEffect(() => {
    if (!activeCousin || !chatUser) return;
    const key = chatSeenKey(activeCousin, chatUser);
    const stamp = () => {
      if (document.visibilityState === "visible") {
        try {
          localStorage.setItem(key, String(Math.floor(Date.now() / 1e3)));
        } catch (e) {
        }
      }
    };
    stamp();
    document.addEventListener("visibilitychange", stamp);
    return () => document.removeEventListener("visibilitychange", stamp);
  }, [activeCousin, chatUser]);
  React.useEffect(() => {
    try {
      localStorage.setItem("fw_pane_side", paneOpen ? "1" : "0");
    } catch (e) {
    }
  }, [paneOpen]);
  const [paneW, setPaneW] = React.useState(() => {
    try {
      const v = Number(localStorage.getItem("fw_pane_w"));
      if (v >= 20 && v <= 80) return v;
    } catch (e) {
    }
    return 50;
  });
  React.useEffect(() => {
    try {
      localStorage.setItem("fw_pane_w", String(Math.round(paneW * 10) / 10));
    } catch (e) {
    }
  }, [paneW]);
  const [chatCollapsed, setChatCollapsed] = React.useState(() => {
    try {
      return localStorage.getItem("fw_chat_collapsed") === "1";
    } catch (e) {
      return false;
    }
  });
  React.useEffect(() => {
    try {
      localStorage.setItem("fw_chat_collapsed", chatCollapsed ? "1" : "0");
    } catch (e) {
    }
  }, [chatCollapsed]);
  const splitRef = React.useRef(null);
  const onDividerDown = (e) => {
    const el = splitRef.current;
    if (!el) return;
    e.preventDefault();
    el.classList.add("dragging");
    const startW = paneW;
    let snap = null;
    const move = (m) => {
      const r = el.getBoundingClientRect();
      if (r.width > 0) setPaneW(Math.min(80, Math.max(20, (r.right - m.clientX) / r.width * 100)));
      snap = splitSnap(m.clientX, r.left, r.right);
      el.classList.toggle("snap-chat", snap === "chat");
      el.classList.toggle("snap-pane", snap === "pane");
    };
    const up = () => {
      el.classList.remove("dragging", "snap-chat", "snap-pane");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (snap) setPaneW(startW);
      if (snap === "chat") setChatCollapsed(true);
      if (snap === "pane") setPaneOpen(false);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const [chatColRef, narrowCol] = window.useNarrowerThan ? useNarrowerThan(CHAT_COMPACT_PX) : [null, false];
  React.useEffect(() => {
    try {
      localStorage.setItem("fw_chat_fullscreen", fullscreen ? "1" : "0");
    } catch (e) {
    }
    document.body.classList.toggle("chat-fullscreen", fullscreen);
    return () => document.body.classList.remove("chat-fullscreen");
  }, [fullscreen]);
  React.useEffect(() => {
    if (!archiveArmed) return;
    const t = setTimeout(() => setArchiveArmed(false), 3500);
    return () => clearTimeout(t);
  }, [archiveArmed]);
  const onArchive = async () => {
    if (!c || !chatUser) return;
    if (!archiveArmed) {
      setArchiveArmed(true);
      setToast("click archive again within 3.5s to confirm");
      scheduleToastClear(3500);
      return;
    }
    setArchiveArmed(false);
    try {
      const r = await fetch("/api/chat/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cousin: c.slug, user: chatUser, keep: 0 })
      });
      const d = await r.json();
      if (d.ok) {
        setToast(`archived ${d.archived}`);
      } else {
        setToast("archive failed: " + (d.error || "unknown"));
      }
    } catch (e) {
      setToast("archive error: " + (e.message || e));
    }
    scheduleToastClear(3e3);
  };
  if (!c) {
    return /* @__PURE__ */ React.createElement("div", { style: { padding: 24, color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 12 } }, "no cousin selected");
  }
  const paneShown = paneOpen && !c.remote;
  const chatHidden = paneShown && chatCollapsed;
  const closePane = () => {
    setPaneOpen(false);
    setChatCollapsed(false);
  };
  const pluginTabAll = window.pluginTabs ? window.pluginTabs(c) : [];
  const pluginTabList = pluginTabAll.filter((t) => t.placement !== "chat");
  const chatStrips = embed || c.remote ? [] : pluginTabAll.filter((t) => t.placement === "chat");
  const activeTab = pluginTabList.find((t) => t.name === paneTab) || null;
  return /* @__PURE__ */ React.createElement("div", { style: { position: "relative", height: "100%", minHeight: 0 } }, /* @__PURE__ */ React.createElement("div", { ref: splitRef, className: "chat-split" + (paneShown ? " pane-open" : "") + (chatHidden ? " chat-collapsed" : ""), style: { "--pane-w": paneW + "%" } }, /* @__PURE__ */ React.createElement("div", { className: "chat-col", ref: chatColRef, "aria-hidden": chatHidden || void 0 }, /* @__PURE__ */ React.createElement(ChatHeader, { cousin: c, chatUser, paneOpen: paneShown, setPaneOpen, search, setSearch, onArchive, fullscreen, setFullscreen: embed ? null : setFullscreen, embed, showArchived, setShowArchived, mediaShown, setMediaShown, narrow: narrowCol }), fullscreen && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "chat-fullscreen-exit",
      onClick: () => setFullscreen(false),
      title: "exit fullscreen"
    },
    "x exit"
  ), chatStrips.length > 0 && window.PluginChatStrips && /* @__PURE__ */ React.createElement(PluginChatStrips, { slug: c.slug, tabs: chatStrips }), /* @__PURE__ */ React.createElement(ChatBody, { key: c.slug + "|" + chatUser, cousin: c, search, setSearch, chatUser, showArchived, mediaShown }), toast && /* @__PURE__ */ React.createElement("div", { className: "chat-toast" }, toast)), paneShown && !chatHidden && /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "split-div",
      onPointerDown: onDividerDown,
      onDoubleClick: () => setPaneW(50),
      title: "drag to resize, double-click to reset; drag to an edge to collapse",
      role: "separator",
      "aria-orientation": "vertical"
    }
  ), chatHidden && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "chat-restore",
      onClick: () => setChatCollapsed(false),
      title: "show the chat",
      "aria-label": "show the chat"
    },
    "›"
  ), /* @__PURE__ */ React.createElement("div", { className: `pane-col ${paneShown ? "open" : ""}` }, paneShown && pluginTabList.length > 0 && window.PluginPaneTabs && /* @__PURE__ */ React.createElement(
    PluginPaneTabs,
    {
      tabs: pluginTabList,
      active: activeTab ? activeTab.name : "pane",
      onPick: setPaneTab,
      paneLabel: c.runner ? "reasoning" : "terminal"
    }
  ), paneShown && activeTab && window.PluginFrame && /* @__PURE__ */ React.createElement(PluginFrame, { key: c.slug + "|" + activeTab.name, tab: activeTab }), paneShown && !activeTab && (c.runner ? /* @__PURE__ */ React.createElement(RunnerPaneView, { key: c.slug, cousin: c, onClose: closePane, chatUser, chatHidden }) : /* @__PURE__ */ React.createElement(PaneView, { cousin: c, onClose: closePane })))));
}
function ChatHeader({ cousin, chatUser, paneOpen, setPaneOpen, search, setSearch, onArchive, fullscreen, setFullscreen, embed, showArchived, setShowArchived, mediaShown, setMediaShown, narrow }) {
  const btnH = 28;
  const remote = !!cousin.remote;
  const [efforts, setEfforts] = React.useState([]);
  const [laneKeys, setLaneKeys] = React.useState(null);
  const [effort, setEffort] = React.useState(cousin.effort || "");
  const [effortHint, setEffortHint] = React.useState(null);
  React.useEffect(() => {
    setEffort(cousin.effort || "");
    setEffortHint(null);
  }, [cousin.effort, cousin.slug]);
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      const d = await apiGet("/api/spawn/options");
      if (cancelled || !d) return;
      setEfforts(d.efforts || []);
      setLaneKeys(d.lane_keys || {});
      setEffort((e) => e || d.default_effort || "");
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  const applyEffort = async (level) => {
    const before = effort;
    setEffort(level);
    const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/effort`, { effort: level });
    if (r.ok && d.ok) {
      setEffortHint(d.restart_required ? "restart to apply" : null);
    } else {
      setEffort(before);
      setEffortHint("failed: " + (d.error || `HTTP ${r.status}`));
    }
  };
  const readsEffort = !laneKeys || !window.agentLaneReads || window.agentLaneReads(cousin.lane, "effort", laneKeys);
  const compact = ((window.useMobileLayout ? useMobileLayout() : false) || !!narrow) && !embed;
  const [searchOpen, setSearchOpen] = React.useState(() => !!search);
  const [menuOpen, setMenuOpen] = React.useState(false);
  const menuRef = React.useRef(null);
  React.useEffect(() => {
    if (!menuOpen) return void 0;
    const away = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false);
    };
    const esc = (e) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [menuOpen]);
  React.useEffect(() => {
    setMenuOpen(false);
  }, [cousin.slug, compact]);
  const closeSearch = () => {
    setSearch("");
    setSearchOpen(false);
  };
  if (compact && searchOpen && !remote) {
    return /* @__PURE__ */ React.createElement("div", { className: "chat-header ch-mobile ch-searching" }, /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "chat-search",
        value: search,
        onChange: (e) => setSearch(e.target.value),
        onKeyDown: (e) => {
          if (e.key === "Escape") closeSearch();
        },
        placeholder: "search...",
        "aria-label": "search this chat",
        autoFocus: true
      }
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost ch-icon ch-search-close",
        onClick: closeSearch,
        title: "close the search",
        "aria-label": "close the search"
      },
      "×"
    ));
  }
  if (compact) {
    const menuItems = !remote || !!setMediaShown;
    return /* @__PURE__ */ React.createElement("div", { className: "chat-header ch-mobile" }, remote && /* @__PURE__ */ React.createElement("span", { className: "ch-sub", title: `a hive node at ${cousin.host}:${cousin.port}` }, "remote"), !remote && readsEffort && /* @__PURE__ */ React.createElement("label", { className: "ch-effort" }, /* @__PURE__ */ React.createElement(
      "select",
      {
        value: effort,
        onChange: (e) => applyEffort(e.target.value),
        disabled: !efforts.length,
        title: cousin.runner ? "effort: cousin.toml [agent] effort, read by the runner at the next start" : "effort: cousin.toml [runtime] effort, rendered into the agent command at the next start",
        "aria-label": "effort",
        className: "sel-inline"
      },
      !efforts.length && /* @__PURE__ */ React.createElement("option", { value: effort }, effort || "..."),
      efforts.map((l) => /* @__PURE__ */ React.createElement("option", { key: l, value: l }, l))
    ), effortHint && /* @__PURE__ */ React.createElement(
      "span",
      {
        className: "ch-effort-hint",
        title: effortHint,
        style: { color: effortHint.startsWith("failed") ? "var(--red)" : "var(--fg-2)" }
      },
      effortHint
    )), /* @__PURE__ */ React.createElement("span", { className: "ch-fill" }), !remote && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost ch-icon ch-search-open" + (search ? " active" : ""),
        onClick: () => setSearchOpen(true),
        title: "search this chat",
        "aria-label": "search this chat"
      },
      /* @__PURE__ */ React.createElement("svg", { viewBox: "0 0 16 16", width: "16", height: "16", fill: "none", stroke: "currentColor", strokeWidth: "1.6", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("circle", { cx: "7", cy: "7", r: "4.5" }), /* @__PURE__ */ React.createElement("path", { d: "M10.5 10.5L14 14" }))
    ), menuItems && /* @__PURE__ */ React.createElement("span", { className: "ch-more-wrap", ref: menuRef }, /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost ch-icon ch-more" + (showArchived || !mediaShown ? " active" : ""),
        onClick: () => setMenuOpen((v) => !v),
        "aria-haspopup": "menu",
        "aria-expanded": menuOpen,
        title: "more: archive, archived, media",
        "aria-label": "more"
      },
      /* @__PURE__ */ React.createElement("svg", { viewBox: "0 0 16 16", width: "16", height: "16", fill: "currentColor", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("circle", { cx: "3", cy: "8", r: "1.4" }), /* @__PURE__ */ React.createElement("circle", { cx: "8", cy: "8", r: "1.4" }), /* @__PURE__ */ React.createElement("circle", { cx: "13", cy: "8", r: "1.4" }))
    ), menuOpen && /* @__PURE__ */ React.createElement("div", { className: "ch-menu", role: "menu" }, /* @__PURE__ */ React.createElement("div", { className: "ch-menu-who" }, "chat · @", cousin.slug, chatUser ? ` as ${chatUser}` : ""), !remote && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(
      "button",
      {
        role: "menuitem",
        className: "btn ghost",
        onClick: onArchive,
        disabled: !chatUser,
        title: "archive ALL active messages"
      },
      "archive"
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        role: "menuitem",
        className: "btn ghost" + (showArchived ? " active" : ""),
        onClick: () => {
          setShowArchived((v) => !v);
          setMenuOpen(false);
        },
        title: showArchived ? "showing archived messages, click to return to live" : "browse archived messages"
      },
      showArchived ? "live" : "archived"
    )), setMediaShown && /* @__PURE__ */ React.createElement(
      "button",
      {
        role: "menuitem",
        className: "btn ghost chat-media-toggle" + (mediaShown ? "" : " active"),
        onClick: () => {
          setMediaShown((v) => !v);
          setMenuOpen(false);
        },
        "aria-pressed": !mediaShown,
        title: mediaShown ? "media shown: click to hide images, videos and audio" : "media hidden: click to show"
      },
      mediaShown ? "media on" : "media off"
    ))), !paneOpen && !remote && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost ch-icon ch-pane-open",
        onClick: () => setPaneOpen(true),
        title: cousin.runner ? "show the reasoning stream" : "show the terminal pane",
        "aria-label": cousin.runner ? "reasoning" : "terminal"
      },
      /* @__PURE__ */ React.createElement("svg", { viewBox: "0 0 16 16", width: "16", height: "16", fill: "none", stroke: "currentColor", strokeWidth: "1.4", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("rect", { x: "2", y: "3", width: "12", height: "10", rx: "1" }), /* @__PURE__ */ React.createElement("path", { d: "M9.5 3v10M11 6h1.5M11 8.5h1.5" }))
    ), setFullscreen && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost ch-icon chat-fullscreen-toggle",
        onClick: () => setFullscreen((v) => !v),
        title: fullscreen ? "exit fullscreen" : "fullscreen chat",
        "aria-label": fullscreen ? "exit fullscreen" : "fullscreen chat"
      },
      /* @__PURE__ */ React.createElement("svg", { viewBox: "0 0 16 16", width: "16", height: "16", fill: "none", stroke: "currentColor", strokeWidth: "1.5", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: "M9.5 2.5h4v4M13.5 2.5L9 7M6.5 13.5h-4v-4M2.5 13.5L7 9" }))
    ));
  }
  return /* @__PURE__ */ React.createElement("div", { className: "chat-header" }, /* @__PURE__ */ React.createElement("span", { className: "ch-title" }, /* @__PURE__ */ React.createElement("span", { className: "ch-sub" }, "chat · @", cousin.slug, chatUser ? ` as ${chatUser}` : ""), remote && /* @__PURE__ */ React.createElement("span", { className: "ch-sub", title: `a hive node at ${cousin.host}:${cousin.port}` }, "· remote")), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), !embed && !remote && readsEffort && /* @__PURE__ */ React.createElement("label", { className: "ch-effort" }, /* @__PURE__ */ React.createElement("span", null, "effort"), /* @__PURE__ */ React.createElement(
    "select",
    {
      value: effort,
      onChange: (e) => applyEffort(e.target.value),
      disabled: !efforts.length,
      title: cousin.runner ? "cousin.toml [agent] effort: read by the runner at the next start" : "cousin.toml [runtime] effort: rendered into the agent command at the next start",
      className: "sel-inline",
      style: { height: btnH }
    },
    !efforts.length && /* @__PURE__ */ React.createElement("option", { value: effort }, effort || "..."),
    efforts.map((l) => /* @__PURE__ */ React.createElement("option", { key: l, value: l }, l))
  ), effortHint && /* @__PURE__ */ React.createElement("span", { style: { color: effortHint.startsWith("failed") ? "var(--red)" : "var(--fg-2)" } }, effortHint)), !remote && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      onClick: onArchive,
      disabled: !chatUser,
      title: "archive ALL active messages",
      style: { height: btnH }
    },
    "archive"
  ), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost" + (showArchived ? " active" : ""),
      onClick: () => setShowArchived((v) => !v),
      title: showArchived ? "showing archived messages, click to return to live" : "browse archived messages",
      style: { height: btnH }
    },
    showArchived ? "live" : "archived"
  )), setMediaShown && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost chat-media-toggle" + (mediaShown ? "" : " active"),
      onClick: () => setMediaShown((v) => !v),
      "aria-pressed": !mediaShown,
      title: mediaShown ? "media shown: click to hide images, videos and audio" : "media hidden: click to show",
      style: { height: btnH }
    },
    mediaShown ? "media on" : "media off"
  ), !remote && /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "chat-search",
      value: search,
      onChange: (e) => setSearch(e.target.value),
      placeholder: "search...",
      style: { height: btnH }
    }
  ), !paneOpen && !embed && !remote && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      onClick: () => setPaneOpen(true),
      title: cousin.runner ? "show the reasoning stream beside the chat" : "show the terminal pane beside the chat",
      style: { height: btnH }
    },
    cousin.runner ? "reasoning" : "terminal",
    " ›"
  ), setFullscreen && !embed && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost chat-fullscreen-toggle",
      onClick: () => setFullscreen((v) => !v),
      title: fullscreen ? "exit fullscreen" : "fullscreen chat",
      style: { height: btnH }
    },
    fullscreen ? "⤢ exit" : "⤢"
  ));
}
function mergePolled(prev, list) {
  const first = list.length ? list[0].id : null;
  if (first == null) return list;
  const older = prev.filter((m) => m.id != null && m.id < first);
  return older.length ? [...older, ...list] : list;
}
function mergeChanged(prev, rows, gone, showArchived) {
  if (!rows.length && !gone.length) return prev;
  const want = showArchived ? 1 : 0;
  const out = /* @__PURE__ */ new Set([...gone, ...rows.map((m) => m.id)]);
  const kept = prev.filter((m) => !out.has(m.id));
  const first = prev.length ? prev[0].id : null;
  const back = rows.filter((m) => (m.archived ? 1 : 0) === want && (first == null || m.id >= first));
  return [...kept, ...back].sort((a, b) => a.id - b.id);
}
function ChatBody({ cousin, search, setSearch, chatUser, showArchived, mediaShown }) {
  const c = cousin;
  const [messages, setMessages] = React.useState([]);
  const [draft, setDraft] = React.useState("");
  const [sending, setSending] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [loaded, setLoaded] = React.useState(false);
  const [totalCount, setTotalCount] = React.useState(0);
  const scrollerRef = React.useRef(null);
  const atBottomRef = React.useRef(true);
  const searchActive = (search || "").trim().length >= 2;
  const [matchIndex, setMatchIndex] = React.useState(0);
  const matchElsRef = React.useRef([]);
  React.useEffect(() => {
    if (!scrollerRef.current) {
      matchElsRef.current = [];
      return;
    }
    if (!searchActive) {
      matchElsRef.current = [];
      setMatchIndex(0);
      return;
    }
    const marks = Array.from(scrollerRef.current.querySelectorAll(".hl-match"));
    matchElsRef.current = marks;
    setMatchIndex((i) => marks.length === 0 ? 0 : Math.min(i, marks.length - 1));
  }, [messages, search, searchActive]);
  React.useEffect(() => {
    const marks = matchElsRef.current;
    if (!marks || marks.length === 0) return;
    marks.forEach((el2, i) => el2.classList.toggle("current", i === matchIndex));
    const el = marks[matchIndex];
    if (el && el.scrollIntoView) {
      atBottomRef.current = false;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [matchIndex, messages, search]);
  const matchCount = matchElsRef.current.length;
  const goPrev = () => {
    if (matchCount === 0) return;
    setMatchIndex((i) => (i - 1 + matchCount) % matchCount);
  };
  const goNext = () => {
    if (matchCount === 0) return;
    setMatchIndex((i) => (i + 1) % matchCount);
  };
  React.useEffect(() => {
    if (!c) return;
    if (!chatUser) {
      setLoaded(true);
      return;
    }
    let cancelled = false;
    let fresh = true;
    let rev = null;
    async function pull() {
      try {
        const base = `/api/messages?cousin=${c.slug}&limit=500&user=${encodeURIComponent(chatUser)}${showArchived ? "&archived=1" : ""}`;
        const url = searchActive ? `/api/search?cousin=${c.slug}&q=${encodeURIComponent(search.trim())}&user=${encodeURIComponent(chatUser)}${showArchived ? "&archived=all" : ""}` : rev != null ? `${base}&changed=${rev}` : base;
        const asked = !searchActive && rev != null;
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        if (cancelled) return;
        setError(d.error || null);
        rev = !searchActive && typeof d.rev === "number" ? d.rev : null;
        if (asked && d.reset) {
          rev = null;
          fresh = true;
          return pull();
        }
        const list = searchActive ? (d.messages || []).slice().reverse() : d.messages || [];
        if (asked) setMessages((prev) => mergeChanged(prev, list, d.gone || [], showArchived));
        else if (searchActive || fresh) setMessages(list);
        else setMessages((prev) => mergePolled(prev, list));
        fresh = false;
        setTotalCount(typeof d.total === "number" ? d.total : list.length);
        setLoaded(true);
      } catch (e) {
        if (!cancelled) {
          setError(String(e.message || e));
          setLoaded(true);
        }
      }
    }
    pull();
    const id = !searchActive ? setInterval(pull, 3500) : null;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [c.slug, chatUser, search, searchActive, showArchived]);
  const chatAnchorRef = React.useRef(null);
  const lastTailIdRef = React.useRef(null);
  React.useEffect(() => {
    lastTailIdRef.current = null;
  }, [cousin?.slug, chatUser]);
  React.useEffect(() => {
    if (!messages.length) return;
    const last = messages[messages.length - 1];
    const tailId = last?.id ?? last?.timestamp ?? null;
    const prev = lastTailIdRef.current;
    lastTailIdRef.current = tailId;
    if (prev === tailId) return;
    const el = scrollerRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (prev !== null && distFromBottom > 600) return;
    const snap = () => {
      const a = chatAnchorRef.current;
      if (a && a.scrollIntoView) a.scrollIntoView({ block: "end", inline: "nearest" });
    };
    snap();
    const id = requestAnimationFrame(snap);
    return () => cancelAnimationFrame(id);
  }, [messages]);
  const [atBottom, setAtBottom] = React.useState(true);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const loadingMoreRef = React.useRef(false);
  const loadMore = async () => {
    if (loadingMoreRef.current) return;
    if (!c || !chatUser || !messages.length || searchActive) return;
    const earliest = messages[0]?.id;
    if (!earliest) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
      const url = `/api/messages?cousin=${c.slug}&limit=200&user=${encodeURIComponent(chatUser)}${showArchived ? "&archived=1" : ""}&before=${earliest}`;
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) return;
      const d = await r.json();
      const more = (d.messages || []).filter((m) => m.id < earliest);
      if (more.length) {
        const el = scrollerRef.current;
        const prevH = el ? el.scrollHeight : 0;
        setMessages((prev) => [...more, ...prev]);
        requestAnimationFrame(() => {
          if (el) el.scrollTop = el.scrollHeight - prevH;
        });
      }
    } catch (_) {
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  };
  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    atBottomRef.current = near;
    setAtBottom(near);
    if (el.scrollTop < 60 && !loadingMoreRef.current) loadMore();
  };
  React.useEffect(() => {
    const onTick = () => {
      const el = scrollerRef.current;
      if (!el) return;
      const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (dist > 120) return;
      const a = chatAnchorRef.current;
      if (a && a.scrollIntoView) a.scrollIntoView({ block: "end", inline: "nearest" });
    };
    window.addEventListener("chat-stream-tick", onTick);
    return () => window.removeEventListener("chat-stream-tick", onTick);
  }, []);
  React.useEffect(() => {
    const el = scrollerRef.current;
    if (!el || !window.ResizeObserver) return;
    const ro = new window.ResizeObserver(() => {
      if (atBottomRef.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const jumpToBottom = () => {
    const el = scrollerRef.current;
    if (!el) return;
    atBottomRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setAtBottom(true);
  };
  const [viewer, setViewer] = React.useState(null);
  const openMedia = React.useCallback((msg) => {
    const items = threadMedia(messages);
    const at = items.findIndex((x) => x.id === msg.id);
    if (at < 0) return;
    setViewer({ items, start: at });
  }, [messages]);
  const closeViewer = React.useCallback(() => setViewer(null), []);
  React.useEffect(() => {
    if (!mediaShown) setViewer(null);
  }, [mediaShown]);
  const [attachment, setAttachment] = React.useState(null);
  const [replyingTo, setReplyingTo] = React.useState(null);
  const fileInputRef = React.useRef(null);
  const pickImage = () => fileInputRef.current && fileInputRef.current.click();
  const acceptImageFile = (f, label) => {
    if (!f) return;
    if (!f.type || !f.type.startsWith("image/")) {
      setError("only image files supported here");
      return;
    }
    if (f.size > 8 * 1024 * 1024) {
      setError("image too large (>8 MB)");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setAttachment({ dataUrl: reader.result, name: f.name || label || "image" });
    reader.readAsDataURL(f);
  };
  const onFile = (e) => {
    acceptImageFile(e.target.files && e.target.files[0]);
    e.target.value = "";
  };
  const onPaste = (e) => {
    const items = e.clipboardData && e.clipboardData.items || [];
    for (const item of items) {
      if (item && item.type && item.type.startsWith("image/")) {
        const f = item.getAsFile();
        if (f) {
          e.preventDefault();
          acceptImageFile(f, "pasted-image");
          return;
        }
      }
    }
  };
  const [dragOver, setDragOver] = React.useState(false);
  const onDragOver = (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.items || []).some((i) => (i.type || "").startsWith("image/"))) {
      e.preventDefault();
      setDragOver(true);
    }
  };
  const onDragLeave = () => setDragOver(false);
  const onDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    acceptImageFile(f, "dropped-image");
  };
  const textareaRef = React.useRef(null);
  const send = async (typed) => {
    const text = (typeof typed === "string" ? typed : draft).trim();
    if (!text && !attachment || sending || !c || !chatUser) return;
    setSending(true);
    try {
      const body = { cousin: c.slug, user: chatUser, message: text };
      if (attachment) body.image = attachment.dataUrl;
      if (replyingTo) body.reply_to = replyingTo;
      const res = await postChatMessage(body);
      if (!res.ok) {
        setError(res.error);
        return;
      }
      setError(null);
      setDraft("");
      setAttachment(null);
      setReplyingTo(null);
      atBottomRef.current = true;
      if (textareaRef.current) textareaRef.current.style.height = "36px";
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setSending(false);
    }
  };
  const shiftEnterRef = React.useRef(false);
  const onKey = (e) => {
    shiftEnterRef.current = e.key === "Enter" && e.shiftKey;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };
  const sendRef = React.useRef(send);
  sendRef.current = send;
  React.useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const onBeforeInput = (e) => {
      if (e.inputType !== "insertLineBreak" && e.inputType !== "insertParagraph") return;
      if (shiftEnterRef.current) {
        shiftEnterRef.current = false;
        return;
      }
      e.preventDefault();
      sendRef.current(el.value);
    };
    el.addEventListener("beforeinput", onBeforeInput);
    return () => el.removeEventListener("beforeinput", onBeforeInput);
  }, []);
  const noUser = !chatUser;
  const showSecondaryBar = searchActive || !loaded || !!error || noUser;
  return /* @__PURE__ */ React.createElement(React.Fragment, null, showSecondaryBar && /* @__PURE__ */ React.createElement("div", { style: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "6px 24px",
    borderBottom: "1px solid var(--hair)",
    fontFamily: "var(--mono)",
    fontSize: 11,
    color: "var(--fg-3)",
    background: searchActive ? "oklch(from var(--accent) 0.25 0.06 h)" : "var(--bg-1)"
  } }, searchActive ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent, var(--cyan))", fontWeight: 600 } }, 'search: "', search, '" · ', matchCount > 0 ? `${matchIndex + 1}/${matchCount} hits in ${messages.length} msg${messages.length !== 1 ? "s" : ""}` : `no hits in ${messages.length} msg${messages.length !== 1 ? "s" : ""}`), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: goPrev, disabled: matchCount === 0, style: { padding: "2px 8px", minHeight: 20, fontSize: 12 }, title: "previous match" }, "↑"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: goNext, disabled: matchCount === 0, style: { padding: "2px 8px", minHeight: 20, fontSize: 12 }, title: "next match" }, "↓"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => setSearch && setSearch(""), style: { padding: "2px 8px", minHeight: 20, fontSize: 10 }, title: "clear search" }, "clear x")) : noUser ? /* @__PURE__ */ React.createElement("span", null, "no chat user: pass ?user=, set [operator] name in this cousin's cousin.toml, or log in") : /* @__PURE__ */ React.createElement("span", null, loaded ? error : "loading...")), /* @__PURE__ */ React.createElement(
    "div",
    {
      ref: scrollerRef,
      className: "chat-scroller",
      onScroll,
      style: {
        flex: 1,
        overflowY: "auto",
        overflowX: "hidden",
        padding: "18px 24px 10px",
        background: "var(--bg-0)",
        fontFamily: "var(--mono)",
        fontSize: 13,
        lineHeight: 1.55,
        minHeight: 0,
        minWidth: 0
      }
    },
    !loaded && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)", fontSize: 12 } }, "loading messages from @", c.slug, "..."),
    loaded && messages.length === 0 && !error && !noUser && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)", fontSize: 12 } }, searchActive ? `no matches for "${search}"` : "no messages yet. send one below."),
    messages.map((m, i) => /* @__PURE__ */ React.createElement(
      ChatBubble,
      {
        key: m.id || m.timestamp,
        msg: m,
        cousin: c,
        search: searchActive ? search : "",
        isLast: i === messages.length - 1,
        chatUser,
        mediaShown: mediaShown !== false,
        onOpenMedia: openMedia,
        onReply: () => setReplyingTo({
          id: m.id,
          user: m.user,
          snippet: (m.message || "").replace(/\s+/g, " ").slice(0, 120)
        })
      }
    )),
    /* @__PURE__ */ React.createElement("div", { ref: chatAnchorRef, style: { height: 1 } })
  ), viewer && /* @__PURE__ */ React.createElement(MediaViewer, { items: viewer.items, start: viewer.start, onClose: closeViewer }), !atBottom && /* @__PURE__ */ React.createElement(
    "button",
    {
      onClick: jumpToBottom,
      title: "jump to latest",
      style: {
        position: "absolute",
        bottom: 76,
        right: 18,
        width: 36,
        height: 36,
        borderRadius: "50%",
        background: "var(--bg-2)",
        color: "var(--fg-0)",
        border: "1px solid var(--line)",
        cursor: "pointer",
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 16,
        lineHeight: 1,
        boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
        zIndex: 5
      }
    },
    "↓"
  ), /* @__PURE__ */ React.createElement(
    "div",
    {
      onDragOver,
      onDragLeave,
      onDrop,
      className: "chat-composer",
      style: {
        background: dragOver ? "oklch(from var(--accent) l c h / 0.18)" : "var(--bg-0)",
        outline: dragOver ? "2px dashed var(--accent)" : "none",
        outlineOffset: -4
      }
    },
    replyingTo && /* @__PURE__ */ React.createElement("div", { style: {
      display: "flex",
      alignItems: "stretch",
      gap: 8,
      padding: 6,
      background: "var(--bg-1)",
      border: "1px solid var(--hair)",
      borderRadius: 8,
      borderLeft: "3px solid var(--accent)"
    } }, /* @__PURE__ */ React.createElement(
      "div",
      {
        style: {
          flex: 1,
          fontSize: 11,
          fontFamily: "var(--mono)",
          color: "var(--fg-2)",
          cursor: "pointer",
          overflow: "hidden"
        },
        title: "click to scroll to original",
        onClick: () => {
          const el = document.querySelector(`[data-msg-id="${replyingTo.id}"]`);
          if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      },
      /* @__PURE__ */ React.createElement("div", { style: { color: "var(--accent)", fontWeight: 500 } }, "replying to ", replyingTo.user || "msg"),
      /* @__PURE__ */ React.createElement("div", { style: {
        whiteSpace: "nowrap",
        textOverflow: "ellipsis",
        overflow: "hidden",
        color: "var(--fg-3)"
      } }, replyingTo.snippet || "(empty)")
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn danger",
        style: { fontSize: 10, padding: "2px 6px" },
        onClick: () => setReplyingTo(null)
      },
      "x"
    )),
    attachment && /* @__PURE__ */ React.createElement("div", { style: {
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: 6,
      background: "var(--bg-1)",
      border: "1px solid var(--hair)",
      borderRadius: 8
    } }, /* @__PURE__ */ React.createElement("img", { src: attachment.dataUrl, alt: "", style: { height: 40, borderRadius: 2 } }), /* @__PURE__ */ React.createElement("span", { style: { fontSize: 11, fontFamily: "var(--mono)", color: "var(--fg-2)", flex: 1 } }, attachment.name || "image"), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn danger",
        style: { fontSize: 10, padding: "2px 6px" },
        onClick: () => setAttachment(null)
      },
      "x"
    )),
    /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, alignItems: "flex-end" } }, /* @__PURE__ */ React.createElement("input", { ref: fileInputRef, type: "file", accept: "image/*", onChange: onFile, style: { display: "none" } }), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        title: "attach image",
        onClick: pickImage,
        disabled: sending || noUser,
        style: {
          height: 36,
          width: 36,
          padding: 0,
          boxSizing: "border-box",
          flexShrink: 0,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          lineHeight: 1,
          fontSize: 16
        }
      },
      "+"
    ), /* @__PURE__ */ React.createElement(
      "textarea",
      {
        ref: textareaRef,
        className: "chat-input",
        value: draft,
        onChange: (e) => setDraft(e.target.value),
        onKeyDown: onKey,
        onPaste,
        enterKeyHint: "send",
        placeholder: noUser ? "composer disabled: no chat user" : `message @${c.slug}...${dragOver ? " (drop image to attach)" : ""}`,
        disabled: sending || noUser,
        rows: 1,
        style: {
          flex: 1,
          resize: "none",
          height: 36,
          maxHeight: 180,
          /* one-line height matches button exactly */
          padding: "7px 12px",
          lineHeight: "20px",
          /* 20 + 7 + 7 + 1 + 1 = 36px */
          fontFamily: "var(--sans)",
          fontSize: 14,
          background: "var(--bg-1)",
          color: "var(--fg-0)",
          border: "1px solid var(--line)",
          borderRadius: 10,
          outline: "none",
          boxSizing: "border-box",
          overflowY: "auto"
        },
        onInput: (e) => {
          const el = e.target;
          if (window.innerWidth <= 820) {
            el.style.height = "36px";
            return;
          }
          el.style.height = "36px";
          const overflows = el.scrollHeight > 40;
          el.style.height = overflows ? "120px" : "36px";
        }
      }
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn primary",
        onClick: send,
        disabled: sending || noUser || !draft.trim() && !attachment,
        style: { height: 36, padding: "0 14px", boxSizing: "border-box", flexShrink: 0 }
      },
      sending ? "sending" : "send"
    ))
  ));
}
function PaneView({ cousin, onClose, serial }) {
  const slug = cousin && cousin.slug;
  const serialRef = React.useRef(!!serial);
  serialRef.current = !!serial;
  const sendingRef = React.useRef(false);
  const tmuxSession = cousin && cousin.tmuxSession || "?";
  const [status, setStatus] = React.useState("connecting");
  const [lastPoll, setLastPoll] = React.useState(null);
  const [lastChange, setLastChange] = React.useState(null);
  const [sendError, setSendError] = React.useState(null);
  const hostRef = React.useRef(null);
  const termRef = React.useRef(null);
  const fitRef = React.useRef(null);
  const lastTextRef = React.useRef("");
  const inputBufRef = React.useRef("");
  const flushTimerRef = React.useRef(null);
  const followRef = React.useRef(true);
  const pendingRef = React.useRef(null);
  const applyingRef = React.useRef(false);
  const paneStateRef = React.useRef(null);
  const pendingStateRef = React.useRef(null);
  const [held, setHeld] = React.useState(false);
  const [xtermReady, setXtermReady] = React.useState(!!window.Terminal);
  React.useEffect(() => {
    if (xtermReady) return;
    let live = true;
    window.loadLib("xterm").then(
      () => {
        if (live) setXtermReady(true);
      },
      () => {
        if (live) setStatus("error");
      }
    );
    return () => {
      live = false;
    };
  }, [xtermReady]);
  const applyFrame = (text, state) => {
    const term = termRef.current;
    if (!term) return;
    if (text === lastTextRef.current) return;
    applyingRef.current = true;
    paneStateRef.current = state || null;
    term.write(FRAME_PREFIX + text, () => {
      try {
        term.scrollToBottom();
      } catch (_e) {
      }
      applyingRef.current = false;
    });
    lastTextRef.current = text;
  };
  React.useEffect(() => {
    if (!xtermReady || !slug || !hostRef.current) return;
    const term = new window.Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: "var(--mono), monospace",
      fontSize: 12,
      theme: { background: "#0a0a0a", foreground: "#dcdcdc" },
      scrollback: 4e3,
      // Wheel events scroll N lines per tick instead of the default 1.
      // On mobile, momentum-scroll fires lots of tiny wheel events; without
      // this, the panel feels like it barely moves per gesture.
      scrollSensitivity: 5,
      fastScrollSensitivity: 8
    });
    const fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fit) term.loadAddon(fit);
    term.open(hostRef.current);
    termRef.current = term;
    fitRef.current = fit;
    lastTextRef.current = "";
    let resizeTimer = null;
    const pushResize = (cols, rows) => {
      fetch("/api/pane/resize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cousin: slug, cols, rows })
      }).catch(() => {
      });
    };
    const debouncedResize = () => {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        try {
          fit && fit.fit();
        } catch (_e) {
        }
        if (term.cols && term.rows) pushResize(term.cols, term.rows);
      }, 120);
    };
    requestAnimationFrame(() => debouncedResize());
    term.onResize(({ cols, rows }) => pushResize(cols, rows));
    const toPaneRows = (data) => {
      const st = paneStateRef.current;
      if (!st || data.indexOf("\x1B[<") < 0) return data;
      const b = term.buffer.active;
      const maxRow = Math.max(1, st.rows || term.rows || 1);
      return data.replace(/\x1b\[<(\d+);(\d+);(\d+)([Mm])/g, (_m, btn, col, row, fin) => {
        const r = Math.max(1, Math.min(maxRow, b.viewportY + Number(row) - (st.top || 0)));
        return "\x1B[<" + btn + ";" + col + ";" + r + fin;
      });
    };
    const sendInput = async () => {
      flushTimerRef.current = null;
      if (serialRef.current && sendingRef.current) return;
      const buf = toPaneRows(inputBufRef.current);
      inputBufRef.current = "";
      if (!buf) return;
      sendingRef.current = true;
      try {
        const r = await fetch("/api/pane/input", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cousin: slug, data: buf })
        });
        const d = await r.json();
        if (!r.ok || !d.ok) setSendError(d.error || `HTTP ${r.status}`);
        else setSendError(null);
      } catch (e) {
        setSendError(String(e.message || e));
      } finally {
        sendingRef.current = false;
        if (serialRef.current && inputBufRef.current && !flushTimerRef.current) {
          flushTimerRef.current = setTimeout(sendInput, 40);
        }
      }
    };
    term.onData((data) => {
      inputBufRef.current += data;
      if (!flushTimerRef.current) {
        flushTimerRef.current = setTimeout(sendInput, 40);
      }
    });
    const host = hostRef.current;
    let touchLastY = null;
    let touchAccPx = 0;
    const TOUCH_PX_PER_TICK = 24;
    const appTracksMouse = () => {
      const st = paneStateRef.current;
      if (st && st.mouse && st.sgr) return true;
      try {
        const m = term.modes && term.modes.mouseTrackingMode;
        return !!m && m !== "none";
      } catch (_e) {
        return false;
      }
    };
    const queueWheel = (up, touch) => {
      const rect = host.getBoundingClientRect();
      const cols = Math.max(1, term.cols || 1);
      const rows = Math.max(1, term.rows || 1);
      const col = Math.max(1, Math.min(
        cols,
        Math.ceil((touch.clientX - rect.left) / (rect.width / cols))
      ));
      const row = Math.max(1, Math.min(
        rows,
        Math.ceil((touch.clientY - rect.top) / (rect.height / rows))
      ));
      inputBufRef.current += "\x1B[<" + (up ? 64 : 65) + ";" + col + ";" + row + "M";
      if (!flushTimerRef.current) {
        flushTimerRef.current = setTimeout(sendInput, 40);
      }
    };
    const onTouchStart = (e) => {
      if (e.touches.length !== 1) {
        touchLastY = null;
        return;
      }
      touchLastY = e.touches[0].clientY;
      touchAccPx = 0;
    };
    const onTouchMove = (e) => {
      if (touchLastY == null || e.touches.length !== 1) return;
      const t = e.touches[0];
      const dy = t.clientY - touchLastY;
      touchLastY = t.clientY;
      if (!appTracksMouse()) return;
      e.preventDefault();
      touchAccPx += dy;
      while (touchAccPx >= TOUCH_PX_PER_TICK) {
        queueWheel(true, t);
        touchAccPx -= TOUCH_PX_PER_TICK;
      }
      while (touchAccPx <= -TOUCH_PX_PER_TICK) {
        queueWheel(false, t);
        touchAccPx += TOUCH_PX_PER_TICK;
      }
    };
    const onTouchEnd = () => {
      touchLastY = null;
      touchAccPx = 0;
    };
    const onWheelCapture = (e) => {
      if (appTracksMouse()) return;
      let alt = false;
      try {
        alt = term.buffer.active.type === "alternate";
      } catch (_e) {
      }
      if (alt) {
        e.preventDefault();
        e.stopPropagation();
      }
    };
    host.addEventListener("wheel", onWheelCapture, { capture: true, passive: false });
    host.addEventListener("touchstart", onTouchStart, { passive: true });
    host.addEventListener("touchmove", onTouchMove, { passive: false });
    host.addEventListener("touchend", onTouchEnd, { passive: true });
    host.addEventListener("touchcancel", onTouchEnd, { passive: true });
    const syncFollow = () => {
      if (applyingRef.current) return;
      const b = term.buffer.active;
      const atBottom = b.viewportY >= b.baseY;
      if (atBottom && !followRef.current) {
        followRef.current = true;
        setHeld(false);
        const next = pendingRef.current;
        const nextState = pendingStateRef.current;
        pendingRef.current = null;
        pendingStateRef.current = null;
        if (next !== null && next !== lastTextRef.current) applyFrame(next, nextState);
      } else if (!atBottom && followRef.current) {
        followRef.current = false;
        setHeld(true);
      }
    };
    const scrollSub = term.onScroll(syncFollow);
    const viewportEl = host.querySelector(".xterm-viewport");
    if (viewportEl) viewportEl.addEventListener("scroll", syncFollow, { passive: true });
    followRef.current = true;
    pendingRef.current = null;
    setHeld(false);
    const onResize = () => debouncedResize();
    let observer = null;
    if (window.ResizeObserver) {
      observer = new window.ResizeObserver(onResize);
      observer.observe(host);
    }
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      if (observer) observer.disconnect();
      try {
        scrollSub.dispose();
      } catch (_e) {
      }
      if (viewportEl) viewportEl.removeEventListener("scroll", syncFollow);
      host.removeEventListener("touchstart", onTouchStart);
      host.removeEventListener("touchmove", onTouchMove);
      host.removeEventListener("touchend", onTouchEnd);
      host.removeEventListener("touchcancel", onTouchEnd);
      host.removeEventListener("wheel", onWheelCapture, { capture: true });
      if (resizeTimer) clearTimeout(resizeTimer);
      if (flushTimerRef.current) clearTimeout(flushTimerRef.current);
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
      lastTextRef.current = "";
    };
  }, [slug, xtermReady]);
  React.useEffect(() => {
    if (!slug || !xtermReady) return;
    let es;
    let reconnectTimer;
    let attempt = 0;
    let cancelled = false;
    const open = () => {
      if (cancelled) return;
      try {
        es = new EventSource("/api/pane/stream?cousin=" + encodeURIComponent(slug) + "&lines=2000");
        es.addEventListener("pane", function(ev) {
          try {
            const payload = JSON.parse(ev.data);
            const text = payload.text || "";
            if (followRef.current) {
              applyFrame(text, payload.state);
            } else {
              pendingRef.current = text;
              pendingStateRef.current = payload.state || null;
            }
            setStatus("live");
            const ts = payload.ts ? new Date(payload.ts) : /* @__PURE__ */ new Date();
            setLastPoll(ts);
            setLastChange(ts);
          } catch (_e) {
          }
        });
        es.addEventListener("delta", function(ev) {
          try {
            const payload = JSON.parse(ev.data);
            const text = payload.text || "";
            const term = termRef.current;
            if (term && text) term.write(text);
            setStatus("live");
            const ts = payload.ts ? new Date(payload.ts) : /* @__PURE__ */ new Date();
            setLastPoll(ts);
            setLastChange(ts);
          } catch (_e) {
          }
        });
        es.addEventListener("heartbeat", function(ev) {
          try {
            const payload = JSON.parse(ev.data);
            setLastPoll(payload.ts ? new Date(payload.ts) : /* @__PURE__ */ new Date());
            setStatus("live");
          } catch (_e) {
          }
        });
        es.addEventListener("geom", function(ev) {
          try {
            const payload = JSON.parse(ev.data);
            const term = termRef.current;
            if (term && payload.cols && term.cols !== payload.cols) {
              term.resize(payload.cols, term.rows);
            }
          } catch (_e) {
          }
        });
        es.onopen = function() {
          attempt = 0;
          setStatus("live");
        };
        es.onerror = function() {
          if (cancelled) return;
          setStatus(attempt === 0 ? "reconnecting" : "reconnecting...");
          try {
            es.close();
          } catch (_e) {
          }
          attempt += 1;
          const wait = Math.min(15e3, 1e3 * Math.pow(2, Math.min(4, attempt - 1)));
          reconnectTimer = setTimeout(open, wait);
        };
      } catch (_e) {
        setStatus("error");
        reconnectTimer = setTimeout(open, 3e3);
      }
    };
    open();
    return function() {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      try {
        if (es) es.close();
      } catch (_e) {
      }
    };
  }, [slug, xtermReady]);
  const [, setNow] = React.useState(Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1e3);
    return () => clearInterval(id);
  }, []);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "pane-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "led " + (status === "live" ? "green" : status === "connecting" ? "amber" : "red") }), /* @__PURE__ */ React.createElement("span", { className: "pane-title" }, "Terminal"), /* @__PURE__ */ React.createElement("span", { className: "pane-chip" }, "tmux ", tmuxSession), /* @__PURE__ */ React.createElement("span", { style: { color: status === "live" ? "var(--green)" : status === "connecting" ? "var(--amber)" : "var(--red)" } }, status), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-2)" } }, "interactive"), lastPoll && /* @__PURE__ */ React.createElement("span", null, "polled ", fmtAgoShort((Date.now() - lastPoll.getTime()) / 1e3)), lastChange && /* @__PURE__ */ React.createElement("span", null, "changed ", fmtAgoShort((Date.now() - lastChange.getTime()) / 1e3)), sendError && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--red)" } }, "input err: ", sendError), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), held && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      onClick: () => {
        const t = termRef.current;
        if (t) t.scrollToBottom();
      },
      title: "scrolled back: live updates are held until you return to the bottom",
      style: { padding: "0 8px", minHeight: 22, color: "var(--amber)" }
    },
    "scrolled back · jump to live"
  ), onClose && /* @__PURE__ */ React.createElement("button", { className: "btn ghost pane-x", onClick: onClose, title: "collapse the terminal pane" }, "x")), /* @__PURE__ */ React.createElement("div", { style: {
    flex: 1,
    minHeight: 0,
    display: "flex",
    padding: "6px 10px",
    background: "#0a0a0a"
  } }, /* @__PURE__ */ React.createElement(
    "div",
    {
      ref: hostRef,
      style: {
        flex: 1,
        minWidth: 0,
        minHeight: 0,
        overflow: "hidden"
      }
    }
  )));
}
const RUNNER_PANE_KEEP = 500;
function runnerEventLine(ev) {
  const p = ev.payload || {};
  const cut = (s, n) => {
    s = String(s == null ? "" : s);
    return s.length > n ? s.slice(0, n - 3) + "..." : s;
  };
  switch (ev.kind) {
    case "state":
      return (p.from ? p.from + " -> " : "") + p.to + (p.detail ? " (" + p.detail + ")" : "");
    case "turn_start":
      return (p.thread_id || "") + ": " + cut((p.bodies || [""])[0], 200);
    case "text":
    case "user":
      return String(p.text || "");
    case "thinking":
      return p.text ? p.text + (p.truncated ? " [truncated]" : "") : "(" + p.length + " chars, not recorded)";
    case "tool":
      return (p.name || "") + " " + cut(JSON.stringify(p.input), 300);
    case "tool_result":
      return (p.is_error ? "error: " : "") + cut(p.text, 600);
    case "tool_call":
      return (p.tool || "") + " " + (p.command || "") + (p.is_error ? " error" : "") + " (" + p.ms + " ms)";
    case "result":
      return "rows " + JSON.stringify(p.inbox_ids || []) + (p.interrupted ? " interrupted" : "") + (p.is_error ? " is_error" : "");
    case "error":
      return String(p.error || "");
    case "session":
      return String(p.session || "");
    default:
      return cut(JSON.stringify(p), 300);
  }
}
const RP_TAGS = {
  span: 1,
  strong: 1,
  em: 1,
  code: 1,
  pre: 1,
  div: 1,
  a: 1,
  table: 1,
  thead: 1,
  tbody: 1,
  tr: 1,
  th: 1,
  td: 1
};
const rpSafeHref = (u) => /^https?:\/\//i.test(String(u || "")) ? String(u) : null;
const rpCut = (s, n) => {
  s = String(s == null ? "" : s);
  return s.length > n ? s.slice(0, n - 3) + "..." : s;
};
const rpSpan = (cls, text) => ({ tag: "span", cls, children: [String(text)] });
function runnerKindClass(kind) {
  switch (kind) {
    case "text":
    case "user":
    case "result":
      return "text";
    case "thinking":
      return "thinking";
    case "tool":
    case "tool_call":
    case "tool_result":
    case "output":
      return "tool";
    case "error":
    case "auth":
      return "error";
    default:
      return "meta";
  }
}
function looksLikeDiff(text) {
  const lines = String(text == null ? "" : text).split("\n");
  let changed = 0, header = false;
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/^@@ -\d+(,\d+)? \+\d+(,\d+)? @@/.test(l) || /^diff --git /.test(l)) header = true;
    else if (/^--- /.test(l) && /^\+\+\+ /.test(lines[i + 1] || "")) header = true;
    else if (/^(\+\+\+|---)( |$)/.test(l)) continue;
    else if (/^[+-]/.test(l)) changed++;
  }
  return header && changed >= 2;
}
function renderDiff(text) {
  const out = [];
  String(text == null ? "" : text).split("\n").forEach((l, i) => {
    if (i) out.push("\n");
    let cls = "rp-diff-ctx";
    if (/^(diff --git |index |--- |\+\+\+ )/.test(l)) cls = "rp-diff-file";
    else if (/^@@/.test(l)) cls = "rp-diff-hunk";
    else if (/^\+/.test(l)) cls = "rp-diff-add";
    else if (/^-/.test(l)) cls = "rp-diff-del";
    out.push(rpSpan(cls, l));
  });
  return out;
}
function highlightJson(text) {
  const s = String(text == null ? "" : text);
  const re = /("(?:[^"\\\n]|\\.)*"?)(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false)\b|\b(null)\b|([{}\[\],:])/g;
  const out = [];
  let last = 0, m;
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) out.push(s.slice(last, m.index));
    if (m[1] !== void 0) {
      out.push(rpSpan(m[2] !== void 0 ? "rp-json-key" : "rp-json-str", m[1]));
      if (m[2] !== void 0) out.push(rpSpan("rp-json-punc", m[2]));
    } else if (m[3] !== void 0) out.push(rpSpan("rp-json-num", m[3]));
    else if (m[4] !== void 0) out.push(rpSpan("rp-json-bool", m[4]));
    else if (m[5] !== void 0) out.push(rpSpan("rp-json-null", m[5]));
    else out.push(rpSpan("rp-json-punc", m[6]));
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
function rpJsonBlock(value, limit) {
  const compact = JSON.stringify(value);
  if (compact === void 0) return [];
  if (compact.length <= limit) {
    const pretty = JSON.stringify(value, null, 2);
    if (pretty.length <= limit * 3) return highlightJson(pretty);
  }
  return highlightJson(rpCut(compact, limit));
}
function mdInline(text) {
  const s = String(text == null ? "" : text);
  const re = /`([^`\n]{1,2000})`|\*\*([^*\n]{1,2000}?)\*\*|\*([^*\s](?:[^*\n]{0,2000}?[^*\s])?)\*|\[([^\]\n]{1,300})\]\(([^)\s]{1,500})\)/g;
  const out = [];
  let last = 0, m;
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) out.push(s.slice(last, m.index));
    if (m[1] !== void 0) out.push({ tag: "code", cls: "rp-md-code", children: [m[1]] });
    else if (m[2] !== void 0) out.push({ tag: "strong", children: [m[2]] });
    else if (m[3] !== void 0) out.push({ tag: "em", children: [m[3]] });
    else if (rpSafeHref(m[5])) out.push({ tag: "a", cls: "rp-md-a", href: m[5], children: [m[4]] });
    else out.push(m[0]);
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
const RP_MD_MAX = 2e4;
const RP_MD_TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
function rpMdCells(line) {
  const t = line.trim().replace(/^\|/, "").replace(/(^|[^\\])\|$/, "$1");
  const cells = [];
  let cur = "";
  for (let i = 0; i < t.length; i++) {
    if (t[i] === "\\" && t[i + 1] === "|") {
      cur += "|";
      i++;
    } else if (t[i] === "|") {
      cells.push(cur.trim());
      cur = "";
    } else cur += t[i];
  }
  cells.push(cur.trim());
  return cells;
}
function renderMarkdownLite(text) {
  const all = String(text == null ? "" : text);
  if (!all) return [];
  const src = all.slice(0, RP_MD_MAX);
  const lines = src.split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^\s*```\s*([\w+-]*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1].toLowerCase(), body = [];
      for (i++; i < lines.length && !/^\s*```\s*$/.test(lines[i]); i++) body.push(lines[i]);
      i++;
      const code = body.join("\n");
      const kids = lang === "diff" || looksLikeDiff(code) ? renderDiff(code) : lang === "json" ? highlightJson(code) : [code];
      out.push({ tag: "pre", cls: "rp-md-pre", children: kids });
      continue;
    }
    if (/\|/.test(line) && i + 1 < lines.length && /\|/.test(lines[i + 1]) && RP_MD_TABLE_SEP.test(lines[i + 1]) && rpMdCells(lines[i + 1]).length === rpMdCells(line).length) {
      const head = rpMdCells(line), body = [];
      for (i += 2; i < lines.length && /\|/.test(lines[i]) && lines[i].trim(); i++) body.push(rpMdCells(lines[i]));
      const row = (cells, tag) => ({ tag: "tr", children: cells.map((c) => ({ tag, children: mdInline(c) })) });
      out.push({ tag: "div", cls: "rp-md-table", children: [{ tag: "table", children: [
        { tag: "thead", children: [row(head, "th")] },
        { tag: "tbody", children: body.map((r) => row(r, "td")) }
      ] }] });
      continue;
    }
    let m;
    if (m = /^(#{1,6})\s+(.*)$/.exec(line)) {
      out.push({ tag: "div", cls: "rp-md-h rp-md-h" + m[1].length, children: mdInline(m[2]) });
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push({ tag: "div", cls: "rp-md-hr", children: [] });
    } else if (m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line)) {
      out.push({ tag: "div", cls: "rp-md-li", children: [m[1], rpSpan("rp-md-bullet", m[2]), " "].concat(mdInline(m[3])) });
    } else if (m = /^\s*>\s?(.*)$/.exec(line)) {
      out.push({ tag: "div", cls: "rp-md-quote", children: mdInline(m[1]) });
    } else if (!line.trim()) {
      out.push({ tag: "div", cls: "rp-md-gap", children: [] });
    } else {
      out.push({ tag: "div", cls: "rp-md-p", children: mdInline(line) });
    }
    i++;
  }
  if (all.length > RP_MD_MAX) out.push({ tag: "div", cls: "rp-md-rest", children: [all.slice(RP_MD_MAX)] });
  return out;
}
function rpToolInput(input) {
  if (input && typeof input.old_string === "string" && typeof input.new_string === "string") {
    const f = String(input.file_path || "");
    const diff = ["--- " + f, "+++ " + f].concat(input.old_string.split("\n").map((l) => "-" + l)).concat(input.new_string.split("\n").map((l) => "+" + l)).join("\n");
    return ["\n"].concat(renderDiff(rpCut(diff, 600)));
  }
  return rpJsonBlock(input, 300);
}
const RP_JSON_PARSE_MAX = 65536;
function rpToolOutput(text) {
  const s = String(text == null ? "" : text);
  const t = s.length <= RP_JSON_PARSE_MAX ? s.trim() : "";
  if (/^[\[{]/.test(t)) {
    try {
      return rpJsonBlock(JSON.parse(t), 600);
    } catch (e) {
    }
  }
  if (looksLikeDiff(s)) return renderDiff(rpCut(s, 600));
  return [rpCut(s, 600)];
}
function runnerEventBody(ev) {
  const p = ev.payload || {};
  switch (ev.kind) {
    case "text":
    case "user":
      return renderMarkdownLite(p.text);
    case "tool":
      return [rpSpan("rp-tool-name", p.name || ""), " "].concat(rpToolInput(p.input));
    case "tool_result":
    case "output":
      return (p.is_error ? [rpSpan("rp-err", "error: ")] : []).concat(rpToolOutput(p.text));
    case "tool_call":
      return [rpSpan("rp-tool-name", p.tool || ""), " " + (p.command || "")].concat(p.is_error ? [rpSpan("rp-err", " error")] : []).concat([rpSpan("rp-dim", " (" + p.ms + " ms)")]);
    case "state":
    case "turn_start":
    case "thinking":
    case "result":
    case "error":
    case "session":
      return [runnerEventLine(ev)];
    default:
      return highlightJson(rpCut(JSON.stringify(p), 300));
  }
}
const RP_BODY_CACHE = /* @__PURE__ */ new WeakMap();
function rpRowBody(ev) {
  let body = RP_BODY_CACHE.get(ev);
  if (!body) {
    body = runnerEventBody(ev).map((n, j) => rpToReact(n, j));
    RP_BODY_CACHE.set(ev, body);
  }
  return body;
}
function rpToReact(node, key) {
  if (node == null) return null;
  if (typeof node !== "object") return String(node);
  const tag = RP_TAGS[node.tag] ? node.tag : "span";
  const props = { key, className: node.cls || void 0 };
  if (tag === "a") {
    const href = rpSafeHref(node.href);
    if (href) {
      props.href = href;
      props.target = "_blank";
      props.rel = "noopener noreferrer";
    }
  }
  return React.createElement(tag, props, ...(node.children || []).map((c, i) => rpToReact(c, i)));
}
function paneLiveness(prev, event, fleetRunner) {
  const p = prev || { alive: null, state: null };
  if (!event) return p;
  switch (event.kind) {
    case "session":
      return { alive: true, state: "starting" };
    case "state": {
      const to = event.payload && event.payload.to;
      return { alive: to !== "stopped", state: to };
    }
    case "fleet":
      return fleetRunner ? { alive: fleetRunner.alive !== false, state: fleetRunner.state != null ? fleetRunner.state : null } : p;
    default:
      return p;
  }
}
function runnerFleetKey(runner) {
  const r = runner || {};
  return JSON.stringify([r.alive, r.state, r.session, r.since]);
}
const rpIsTick = (ev) => !!ev && ev.kind === "system" && !!ev.payload && ev.payload.subtype === "thinking_tokens";
function rpCompact(prev, add) {
  const out = prev.slice();
  for (const ev of add) {
    const last = out[out.length - 1];
    if (rpIsTick(ev) && rpIsTick(last)) out[out.length - 1] = Object.assign({}, last, { n: (last.n || 1) + 1, last_ts: ev.ts });
    else out.push(ev);
  }
  return out;
}
const RP_BOOT_KINDS = { runner: 1, policy: 1, mcp_config: 1, session: 1 };
const RP_SKIP_SYSTEM = { vcs_state_changed: 1, background_tasks_changed: 1 };
const RP_GATE_KEEPS = { gate: 1, text: 1, thinking: 1, usage: 1 };
const RP_SYSTEM_LINES = {
  compact_boundary: (p) => ["rp-roll", "context compacted" + (p.trigger ? " · " + p.trigger : "")],
  reconnected: (p) => ["rp-warn", "reconnected" + (p.why ? " · " + rpCut(p.why, 140) : "")],
  restart_note: (p) => ["rp-roll", "restart" + (p.why ? " · " + rpCut(p.why, 140) : "")],
  retry: (p) => ["rp-warn", "retry " + (p.attempt || 1) + (p.message ? " · " + rpCut(p.message, 140) : "")],
  stall: (p) => ["rp-warn", "stall · " + (p.site || "?") + (p.seconds != null ? " " + Math.round(p.seconds) + " s" : "") + (p.ongoing ? ", ongoing" : "")],
  permission_denied: (p) => ["rp-warn", "permission denied" + (p.tool_name ? " · " + p.tool_name : "")],
  resume_failed: (p) => ["rp-warn", "resume failed" + (p.error ? " · " + rpCut(p.error, 140) : "")],
  connect_failed: (p) => ["rp-err", "connect failed" + (p.error ? " · " + rpCut(p.error, 140) : "")],
  side_restarted: (p) => ["rp-warn", "side session restarted · " + (p.session || "?") + (p.attempt ? ", attempt " + p.attempt : "")],
  interrupt_dropped: (p) => ["rp-dim", "interrupt dropped · its turn had ended"],
  drained: (p) => ["rp-dim", "drained · " + (Array.isArray(p.messages) ? p.messages.length : p.messages || 0) + " messages"],
  resync_skipped: (p) => ["rp-dim", "resync skipped" + (p.why ? " · " + p.why : "")],
  opencode_leftover: (p) => ["rp-roll", "leftover opencode killed · " + (p.pids || []).length + " process" + ((p.pids || []).length === 1 ? "" : "es")],
  pressure_off: (p) => ["rp-dim", "context pressure off" + (p.why ? " · " + rpCut(p.why, 140) : "")],
  event_gap: (p) => ["rp-dim", "event stream gap" + (p.settled ? " · settled" : "")],
  adopt_refused: (p) => ["rp-warn", "pane not adopted" + (p.reason || p.detail || p.why ? " · " + rpCut(p.reason || p.detail || p.why, 140) : "")],
  hooks_silent: (p) => ["rp-warn", "hooks silent" + (p.detail ? " · " + rpCut(p.detail, 140) : "")],
  notice_not_typed: (p) => ["rp-warn", "notice not typed" + (p.waited_s != null ? " after " + p.waited_s + " s" : "")],
  pane_failing: (p) => ["rp-err", "pane failing" + (p.reason ? " · " + rpCut(p.reason, 140) : "")],
  pane_lost: (p) => ["rp-err", "pane lost" + (p.requeued ? " · " + p.requeued + " requeued" : "")],
  pane_reopened: (p) => ["rp-roll", "pane reopened" + (p.source ? " · " + p.source : "")],
  pane_unreachable: (p) => ["rp-err", "pane unreachable" + (p.detail ? " · " + rpCut(p.detail, 140) : "")],
  session_changed: (p) => ["rp-dim", "new session · " + rpCut(p.new_session_id || "", 60)],
  // a removed config key found at start (runner/main.py): a boot line
  config: (p) => ["rp-warn", "config · " + (p.key && p.key !== p.where ? (p.where || "") + " " + p.key : p.key || p.where || "?") + " is no longer read (2.0.0); cousin-migrate tidy removes it"],
  typing_blocked: (p) => ["rp-warn", "typing blocked · " + (p.what || "?") + (p.screen ? " (" + p.screen + ")" : "")]
};
const RP_SYSTEM_BOOT = { session: 1 };
const RP_SYSTEM_BOOT_LINES = { config: 1 };
const RP_KIND_LINES = {
  memory_update: (p) => ["rp-dim", "memory updated · " + (p.entries || 0) + " entr" + (p.entries === 1 ? "y" : "ies")],
  config_change: (p) => ["rp-dim", "config changed · " + (p.files || []).join(", ")],
  hook: (p) => [p.error ? "rp-warn" : "rp-dim", "hook " + (p.event || "") + (p.error ? " · " + rpCut(p.error, 140) : "")],
  cap: (p) => ["rp-warn", "cost cap · $" + Number(p.spent || 0).toFixed(2) + " of $" + Number(p.limit || 0).toFixed(2) + " today" + (p.refused ? " · turn refused" : p.allowed === "person" ? " · only a person's chat runs" : "")],
  duplicate_delivery: (p) => ["rp-dim", "duplicate delivery ignored"],
  foreign_turn: (p) => ["rp-dim", "a turn typed in the pane"],
  other: (p) => ["rp-dim", "SDK message · " + (p.type || "?")],
  permission: (p) => ["rp-warn", p.tool_name ? "permission asked · " + p.tool_name : "notification · " + rpCut(p.message || p.event || "", 140)]
};
function rpGateLine(p) {
  const threads = (p.threads || []).join(", ");
  if (p.gate === "reply") return { cls: "rp-warn", text: "reply gate · sent back once, no reply to " + (threads || "the thread") + (p.written_call ? " · reply written as text" : "") };
  if (p.gate === "send") return { cls: "rp-warn", text: "send gate · sent back once, no send to " + (threads || "the peer") };
  if (p.gate === "repeat") return { cls: "rp-warn", text: "repeat gate · " + (p.tool || "a tool") + " called " + (p.count || "?") + " times in a row" + (p.action === "end" ? " · turn ended" : p.action ? " · " + p.action : "") };
  return { cls: "rp-warn", text: "gate · " + String(p.gate || "?") + (threads ? " · " + threads : "") };
}
const RP_TASK_SUBTYPES = { task_started: 1, task_progress: 1, task_updated: 1, task_notification: 1 };
const RP_TASK_ENDS = { completed: 1, failed: 1, stopped: 1, killed: 1 };
function rpTaskEvent(tasks, p, ts) {
  const id = p.task_id;
  if (!id) return p.subtype === "task_started" ? 1 : p.subtype === "task_notification" ? -1 : 0;
  if (p.subtype === "task_started") {
    tasks[id] = {
      id,
      desc: p.description || "",
      type: p.task_type || null,
      started: ts,
      tool: null,
      status: "running",
      ended: null,
      summary: null
    };
    return 0;
  }
  const t = tasks[id];
  if (!t) return 0;
  if (p.subtype === "task_progress") {
    if (t.ended == null && p.last_tool_name) t.tool = p.last_tool_name;
  } else if (p.subtype === "task_notification") {
    t.status = p.status || (t.ended == null ? "ended" : t.status);
    if (p.summary) t.summary = p.summary;
    if (t.ended == null) t.ended = ts;
  } else if (p.subtype === "task_updated" && p.status && t.ended == null) {
    t.status = p.status;
    if (RP_TASK_ENDS[p.status]) t.ended = ts;
  }
  return 0;
}
function rpTaskList(tasks, keep) {
  const all = Object.keys(tasks || {}).map((k) => tasks[k]);
  return {
    running: all.filter((t) => t.ended == null).sort((a, b) => (a.started || 0) - (b.started || 0)),
    ended: all.filter((t) => t.ended != null).sort((a, b) => b.ended - a.ended).slice(0, keep == null ? 5 : keep)
  };
}
function rpTaskType(type) {
  const t = String(type || "");
  if (/bash|shell/.test(t)) return "shell";
  if (/agent/.test(t)) return "agent";
  return t.replace(/_/g, " ") || "task";
}
function rpModel(events) {
  const strip = {
    state: null,
    activity: null,
    rate: {},
    session: null,
    tokens: 0,
    cost: 0,
    turns: 0,
    bg: 0,
    bgUntracked: 0,
    tasks: {},
    auth: null
  };
  const rows = [];
  const tools = {};
  let recalls = [];
  let turn = null, meta = {}, thinkSince = null;
  let gateRow = null;
  const newMeta = () => {
    meta = {};
    return meta;
  };
  (events || []).forEach((ev, i) => {
    const p = ev.payload || {};
    const k = ev.kind, ts = ev.ts || null;
    const key = (ts != null ? ts : "") + ":" + (ev.seq != null ? ev.seq : "i" + i) + ":" + k;
    if (rpIsTick(ev)) {
      if (thinkSince == null) thinkSince = ts;
      strip.activity = { kind: "thinking", since: thinkSince };
      return;
    }
    const thought = thinkSince;
    thinkSince = null;
    const lastGate = gateRow;
    if (!RP_GATE_KEEPS[k] && !(k === "system" && p.subtype === "notification")) gateRow = null;
    if (k === "state") {
      strip.state = p.to || null;
      if (p.to === "running") strip.activity = { kind: "working", since: ts };
      else if (p.to === "waiting_permission") strip.activity = { kind: "waiting", since: ts };
      else strip.activity = null;
      return;
    }
    if (k === "system") {
      if (RP_TASK_SUBTYPES[p.subtype]) strip.bgUntracked = Math.max(0, strip.bgUntracked + rpTaskEvent(strip.tasks, p, ts));
      else if (p.subtype === "fresh" || p.subtype === "init" || p.subtype === "resumed") rpBoot(rows, key, ev);
      else if (p.subtype === "mcp" && p.status === "connected" || p.subtype === "policy_plugin" && !(p.errors || []).length) rpBoot(rows, key, ev);
      else if (p.subtype === "mcp")
        rows.push({ t: "line", boot: true, key, ev, cls: "rp-err", text: "mcp · " + String(p.status || "not connected") + (p.error ? " · " + rpCut(p.error, 120) : "") });
      else if (p.subtype === "policy_plugin")
        rows.push({ t: "line", boot: true, key, ev, cls: "rp-err", text: "policy plugin · " + rpCut((p.errors || []).map((e) => e && typeof e === "object" ? (e.source != null ? e.source + ": " : "") + String(e.error || "") : String(e)).join("; "), 160) });
      else if (p.subtype === "perimeter")
        rows.push({ t: "line", boot: true, key, ev, cls: p.level === "warning" ? "rp-warn" : "rp-dim", text: "perimeter · " + rpCut(p.line || "", 160), detail: p.line || null });
      else if (p.subtype === "background_turn" && p.phase === "start") {
        turn = { t: "turn", key, ev, thread: "background", bodies: [], user: null, recall: null, meta: newMeta() };
        recalls = [];
        rows.push(turn);
      } else if (p.subtype === "background_turn" || p.subtype === "background_end")
        rows.push({
          t: "line",
          key,
          ev,
          cls: "rp-dim",
          text: p.subtype === "background_end" ? "the CLI's stream ended between turns" : "background turn handed to the next turn"
        });
      else if (p.subtype === "api_retry") {
        const auth = p.error_status === 401 || p.error_status === 403;
        rows.push({
          t: "line",
          key,
          ev,
          cls: auth ? "rp-err" : "rp-warn",
          text: "API retry " + (p.attempt || 1) + ": " + [p.error_status, p.error].filter((x) => x != null && x !== "").join(" ")
        });
      } else if (p.subtype === "notification") {
        if (p.message) rows.push({ t: "line", key, ev, cls: "rp-dim", text: "notification · " + rpCut(String(p.message), 160) });
      } else if (RP_SYSTEM_BOOT[p.subtype] && rpBootGroup(rows)) rpBoot(rows, key, ev);
      else if (RP_SYSTEM_BOOT[p.subtype])
        rows.push({ t: "line", key, ev, cls: "rp-dim", text: "new session · " + rpCut(p.session_id || "", 60) });
      else if (RP_SYSTEM_LINES[p.subtype]) {
        const [cls, text] = RP_SYSTEM_LINES[p.subtype](p);
        rows.push({ t: "line", key, ev, cls, text, ...RP_SYSTEM_BOOT_LINES[p.subtype] ? { boot: true } : {} });
      } else if (!RP_SKIP_SYSTEM[p.subtype] && Object.keys(p).some((f) => f !== "subtype")) rows.push({ t: "meta", key, ev });
      return;
    }
    if (k === "runner") {
      Object.keys(strip.tasks).forEach((id) => {
        const t = strip.tasks[id];
        if (t.ended == null) {
          t.ended = ts;
          t.status = "lost";
          t.summary = "the runner restarted";
        }
      });
    }
    if (k === "harness" && !p.ok) {
      rows.push({
        t: "line",
        boot: true,
        key,
        ev,
        cls: p.level === "error" ? "rp-err" : "rp-warn",
        text: "harness · " + String(p.message || (p.problems || []).join("; ") || "version check failed")
      });
      return;
    }
    if (k === "policy" && !("describe" in p)) {
      rows.push({
        t: "line",
        key,
        ev,
        cls: p.decision === "deny" ? "rp-err" : "rp-warn",
        text: "policy · " + (p.decision || "?") + " " + (p.tool || "a tool") + (p.agent_id ? " (subagent)" : "") + (p.reason ? " · " + rpCut(p.reason, 140) : "")
      });
      return;
    }
    if (RP_BOOT_KINDS[k] || k === "harness") {
      rpBoot(rows, key, ev);
      return;
    }
    if (RP_KIND_LINES[k]) {
      const [cls, text] = RP_KIND_LINES[k](p);
      rows.push({ t: "line", key, ev, cls, text });
      return;
    }
    switch (k) {
      case "rate_limit":
        strip.rate[p.type || "limit"] = p;
        return;
      case "session_init":
        strip.session = { model: p.model || null, auth: p.apiKeySource == null ? null : p.apiKeySource === "none" ? "your login" : "API key (" + p.apiKeySource + ")" };
        return;
      case "turn_start":
        turn = { t: "turn", key, ev, thread: p.thread_id || null, bodies: p.bodies || [], user: null, recall: null, meta: newMeta() };
        recalls = [];
        rows.push(turn);
        return;
      case "gate": {
        const line = rpGateLine(p);
        gateRow = { t: "line", key, ev, cls: line.cls, text: line.text, detail: null };
        rows.push(gateRow);
        return;
      }
      case "user":
        if (lastGate) {
          lastGate.detail = (lastGate.detail ? lastGate.detail + "\n" : "") + String(p.text || "");
          return;
        }
        if (turn && turn.user == null) {
          turn.user = String(p.text || "");
          return;
        }
        rows.push({ t: "turn", mid: true, key, ev, thread: null, bodies: [], user: String(p.text || ""), recall: recalls.shift() || null });
        return;
      case "recall":
        if (!turn) return;
        if (turn.user == null && turn.recall == null) turn.recall = p;
        else recalls.push(p);
        return;
      case "auth": {
        const line = rpAuthLine(p);
        if (line.blocking) strip.auth = p;
        else if (p.restored) strip.auth = null;
        rows.push({ t: "line", key, ev, cls: line.cls, text: line.text });
        return;
      }
      case "rollover":
        rows.push({
          t: "line",
          key,
          ev,
          cls: "rp-roll" + (p.phase === "failed" ? " rp-err" : ""),
          text: "session rollover · " + (p.phase || "") + (p.reason ? " · " + String(p.reason).replace(/_/g, " ") : "")
        });
        return;
      case "review_gate":
        rows.push({
          t: "line",
          key,
          ev,
          cls: p.error ? "rp-warn" : "rp-dim",
          text: "memory review · " + (p.kept || 0) + " kept, " + (p.dropped || 0) + " dropped" + (p.pending ? ", " + p.pending + " pending" : "") + (p.error ? " · " + p.error : "")
        });
        return;
      case "tool": {
        const row = { t: p.name === "mcp__cousin__reply" ? "reply" : "tool", key, ev, result: null, ms: null };
        if (p.id) tools[p.id] = row;
        rows.push(row);
        strip.activity = { kind: "tool", name: rpToolLabel(p.name, p.input).name, since: ts };
        return;
      }
      case "tool_result": {
        const row = tools[p.tool_use_id];
        if (row && !row.result) {
          row.result = ev;
          strip.activity = strip.activity && { kind: "working", since: ts };
          return;
        }
        break;
      }
      case "tool_call": {
        for (let j = rows.length - 1; j >= 0; j--) {
          const r = rows[j];
          if ((r.t === "tool" || r.t === "reply") && r.ms == null) {
            const n = String((r.ev.payload || {}).name || "");
            if (n === p.tool || n.endsWith("__" + p.tool)) {
              r.ms = p.ms;
              return;
            }
          }
        }
        if (p.is_error) rows.push({ t: "line", key, ev, cls: "rp-err", text: "tool " + (p.tool || "?") + " failed" + (p.command ? " · " + rpCut(p.command, 90) : "") });
        return;
      }
      case "thinking":
        if (p.text || thought != null) rows.push({ t: "thinking", key, ev, secs: thought != null && ts ? Math.max(0, ts - thought) : null });
        if (strip.activity) strip.activity = { kind: "working", since: ts };
        return;
      case "text":
        rows.push({ t: "text", key, ev });
        return;
      case "error":
        rows.push({ t: "error", key, ev });
        return;
      case "checkpoint":
        meta.checkpoint = p;
        return;
      case "extract":
        meta.extract = p;
        return;
      case "propose":
        meta.propose = p;
        return;
      case "usage":
        meta.usage = p;
        strip.tokens += Number(p.total) || 0;
        strip.cost += Number(p.cost_usd) || 0;
        return;
      case "result":
        meta.result = p;
        strip.turns += 1;
        rows.push({ t: "footer", key, ev, meta });
        return;
    }
    rows.push({ t: "meta", key, ev });
  });
  strip.bg = strip.bgUntracked + rpTaskList(strip.tasks, 0).running.length;
  return { strip, rows };
}
function rpBootGroup(rows) {
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].t === "boot") return rows[i];
    if (!rows[i].boot) return null;
  }
  return null;
}
function rpBoot(rows, key, ev) {
  const group = rpBootGroup(rows);
  if (group) group.events.push(ev);
  else rows.push({ t: "boot", key, events: [ev] });
}
function rpAuthLine(p) {
  if (p.restored) return { cls: "rp-ok", text: "login restored" + (p.account ? " · " + p.account : ""), blocking: false };
  if (p.retry) return { cls: "rp-warn", text: "login retry · " + p.retry, blocking: false };
  if (p.mismatch) return { cls: "rp-err", text: "credentials mismatch · expected " + p.expected + ", got " + p.got, blocking: true };
  const why = [p.detail || p.reason || (p.login_required ? "login required" : "auth"), p.action].filter(Boolean).join(" · ");
  return { cls: "rp-err", text: "login required · " + why, blocking: true };
}
function rpToolLabel(name, input) {
  let n = String(name || "tool");
  let server = null;
  if (n.startsWith("mcp__")) {
    const parts = n.split("__");
    server = parts[1] || null;
    n = parts.slice(2).join(" ") || server || n;
  }
  const i = input || {};
  let desc = "";
  for (const f of ["description", "file_path", "pattern", "query", "command", "url", "topic", "prompt", "text"]) {
    if (typeof i[f] === "string" && i[f].trim()) {
      desc = f === "file_path" ? i[f].split("/").pop() : i[f];
      if (f === "command" && typeof i.topic === "string") desc += " " + i.topic;
      break;
    }
  }
  return { name: n, server, desc: rpCut(desc.replace(/\s+/g, " ").trim(), 90) };
}
function rpElapsed(sec) {
  if (sec == null || !isFinite(sec)) return "";
  if (sec < 1) return Math.round(sec * 1e3) + "ms";
  if (sec < 60) return (sec < 10 ? sec.toFixed(1) : String(Math.round(sec))) + "s";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m + "m " + String(s).padStart(2, "0") + "s";
}
function rpTok(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "k";
  return String(n);
}
const RP_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
function rpRate(type, p) {
  const label = type === "seven_day" ? "7-day" : type === "five_hour" ? "5-hour" : String(type).replace(/_/g, " ");
  const u = typeof p.utilization === "number" ? p.utilization : null;
  const pct = u == null ? null : Math.max(0, Math.min(100, Math.round(u * 100)));
  const level = p.status === "rejected" || p.overage ? "over" : p.status === "allowed_warning" || pct != null && pct >= 80 ? "warn" : "ok";
  let resets = null;
  if (p.resets_at) {
    const d = new Date(p.resets_at * 1e3);
    resets = RP_DAYS[d.getDay()] + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  return { label, pct, level, resets };
}
function rpFooterParts(meta) {
  const m = meta || {}, r = m.result || {};
  const parts = [r.interrupted ? "interrupted" : r.is_error ? "ended with an error" : "done"];
  if (r.num_turns) parts.push(r.num_turns + (r.num_turns === 1 ? " step" : " steps"));
  if (m.usage && m.usage.total) parts.push(rpTok(m.usage.total) + " tok");
  const cost = m.usage && m.usage.cost_usd != null ? m.usage.cost_usd : r.total_cost_usd;
  if (cost) parts.push("$" + Number(cost).toFixed(2) + (m.usage && m.usage.estimate ? " est" : ""));
  if (m.extract && m.extract.written) parts.push(m.extract.written + (m.extract.written === 1 ? " memory" : " memories"));
  if (m.propose && m.propose.proposal) parts.push("proposal");
  if (m.checkpoint) parts.push("checkpoint");
  return parts;
}
function rpTurnHead(turn) {
  const src = turn.user != null ? turn.user : String((turn.bodies || [])[0] || "");
  const lines = src.split("\n");
  let head = turn.thread || "", rest = src;
  let title = head;
  if (/^\[[^\]]+\]/.test(lines[0] || "")) {
    title = lines[0];
    rest = lines.slice(1).join("\n");
    const m = /^\[[^\]]+\]\s+\S+\s+from\s+(.+?)\s+at\s+\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})/.exec(title);
    head = m ? m[1] + " " + m[2] : title;
  }
  return { head: rpCut(head, 90), title, snippet: rpCut(rest.replace(/\s+/g, " ").trim(), 110), full: src };
}
function RunnerPaneView({ cousin, onClose, chatUser, chatHidden }) {
  const slug = cousin && cousin.slug;
  const runner = cousin && cousin.runner || {};
  const [events, setEvents] = React.useState([]);
  const [status, setStatus] = React.useState("connecting");
  const [live, setLive] = React.useState(() => paneLiveness(null, { kind: "fleet" }, runner));
  const [said, setSaid] = React.useState("");
  const [note, setNote] = React.useState(null);
  const listRef = React.useRef(null);
  const runnerRef = React.useRef(runner);
  runnerRef.current = runner;
  const runnerKey = runnerFleetKey(runner);
  React.useEffect(() => {
    setLive((prev) => paneLiveness(prev, { kind: "fleet" }, runnerRef.current));
  }, [runnerKey]);
  React.useEffect(() => {
    if (!slug) return void 0;
    setEvents([]);
    let batch = [], frame = null;
    const flush = () => {
      frame = null;
      const add = batch;
      batch = [];
      setEvents((prev) => rpCompact(prev, add).slice(-RUNNER_PANE_KEEP));
    };
    const push = (ev) => {
      batch.push(ev);
      if (frame === null) frame = window.requestAnimationFrame(flush);
    };
    const es = new EventSource(`/api/cousins/${encodeURIComponent(slug)}/stream`);
    es.onopen = () => setStatus("live");
    es.onerror = () => setStatus("reconnecting");
    es.addEventListener("runner-event", (m) => {
      let ev;
      try {
        ev = JSON.parse(m.data);
      } catch (e) {
        return;
      }
      if (ev.kind === "state" && ev.payload) setLive((prev) => paneLiveness(prev, ev, runnerRef.current));
      push(ev);
    });
    es.addEventListener("session", (m) => {
      let payload = {};
      try {
        payload = JSON.parse(m.data) || {};
      } catch (e) {
      }
      setLive((prev) => paneLiveness(prev, { kind: "session" }, runnerRef.current));
      push({ kind: "session", payload });
    });
    return () => {
      es.close();
      if (frame !== null) window.cancelAnimationFrame(frame);
    };
  }, [slug]);
  const atBottomRef = React.useRef(true);
  const [behind, setBehind] = React.useState(false);
  const onListScroll = () => {
    const el = listRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottomRef.current) setBehind(false);
  };
  const toBottom = () => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    atBottomRef.current = true;
    setBehind(false);
  };
  const [raw, setRaw] = React.useState(() => {
    try {
      return localStorage.getItem("fw_rp_raw") === "1";
    } catch (e) {
      return false;
    }
  });
  React.useEffect(() => {
    try {
      localStorage.setItem("fw_rp_raw", raw ? "1" : "0");
    } catch (e) {
    }
  }, [raw]);
  const model = React.useMemo(() => rpModel(events), [events]);
  const [now, setNow] = React.useState(() => Date.now());
  const ticking = !!model.strip.activity;
  React.useEffect(() => {
    if (!ticking) return void 0;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1e3);
    return () => clearInterval(id);
  }, [ticking]);
  React.useLayoutEffect(() => {
    if (atBottomRef.current) toBottom();
    else setBehind(true);
  }, [events, raw]);
  const post = async (path, body) => {
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {})
      });
      const d = await r.json();
      setNote(r.ok ? d.outcome : d.error || `HTTP ${r.status}`);
      return r.ok;
    } catch (e) {
      setNote(String(e.message || e));
      return false;
    }
  };
  const interrupt = () => post(`/api/cousins/${encodeURIComponent(slug)}/interrupt`);
  const [asChat, setAsChat] = React.useState(false);
  const sendAsChat = asChat && !!chatHidden;
  const say = async () => {
    const text = said.trim();
    if (!text) return;
    if (sendAsChat) {
      if (!chatUser) {
        setNote("no chat user: open the chat to see why");
        return;
      }
      try {
        const res = await postChatMessage({ cousin: slug, user: chatUser, message: text });
        setNote(res.ok ? "sent to the chat" : res.error);
        if (res.ok) setSaid("");
      } catch (e) {
        setNote(String(e.message || e));
      }
      return;
    }
    if (await post(`/api/cousins/${encodeURIComponent(slug)}/say`, { text })) setSaid("");
  };
  const unsupported = runner.unsupported || [];
  const turnLive = live.alive && (live.state === "running" || live.state === "waiting_permission");
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "pane-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "led " + (!live.alive ? "gray" : live.state === "waiting_permission" ? "amber" : "green") + (turnLive && live.state === "running" ? " pulse" : "") }), /* @__PURE__ */ React.createElement("span", { className: "pane-title" }, "Reasoning"), /* @__PURE__ */ React.createElement("span", { className: "pane-chip" }, "runner ", runner.kind || "?"), /* @__PURE__ */ React.createElement("span", { style: { color: status === "live" ? "var(--green)" : "var(--amber)" } }, status), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-1)" } }, !live.alive ? "not running (last: " + (live.state || "none") + ")" : (live.state || "no state yet").replace(/_/g, " ")), unsupported.length > 0 && /* @__PURE__ */ React.createElement("span", null, "unsupported: ", unsupported.join(", ")), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), onClose && /* @__PURE__ */ React.createElement("button", { className: "btn ghost pane-x", onClick: onClose, title: "collapse the pane" }, "x")), /* @__PURE__ */ React.createElement(RpStrip, { strip: model.strip, now, raw, setRaw }), /* @__PURE__ */ React.createElement("div", { ref: listRef, className: "rp-list" + (raw ? "" : " rp-folded"), onScroll: onListScroll }, raw ? events.map((ev, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "runner-ev runner-ev-" + ev.kind + " rp-" + runnerKindClass(ev.kind) }, /* @__PURE__ */ React.createElement("span", { className: "rp-kind" }, ev.kind), /* @__PURE__ */ React.createElement("div", { className: "rp-body" }, rpRowBody(ev)))) : model.rows.map((row) => /* @__PURE__ */ React.createElement(
    RpRow,
    {
      key: row.key,
      row,
      now,
      slug,
      activeTool: !!model.strip.activity
    }
  )), events.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "rp-empty" }, status === "live" ? "no events yet: the stream shows the runner's turns as they happen" : "connecting to the stream...")), behind && /* @__PURE__ */ React.createElement("button", { className: "rp-jump", onClick: toBottom }, "new events ↓"), /* @__PURE__ */ React.createElement("div", { className: "pane-foot" }, note && /* @__PURE__ */ React.createElement("div", { className: "pane-note" }, note), /* @__PURE__ */ React.createElement("div", { className: "pane-foot-row" }, chatHidden && /* @__PURE__ */ React.createElement("label", { className: "rp-as-chat", title: "post a stored chat message, as the chat's composer does, instead of saying it to the running turn" }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: asChat, onChange: (e) => setAsChat(e.target.checked) }), /* @__PURE__ */ React.createElement("span", null, "send as chat")), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: said,
      onChange: (e) => setSaid(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") say();
      },
      placeholder: sendAsChat ? `message @${slug}` : "say to the running turn",
      style: { flex: 1 }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: say, disabled: !said.trim() }, sendAsChat ? "send" : "say"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn rp-interrupt",
      onClick: interrupt,
      title: "interrupt the running turn",
      disabled: !live.alive || live.state !== "running" && live.state !== "waiting_permission"
    },
    "interrupt"
  ))));
}
const RP_ACT_LABEL = { thinking: "thinking", working: "working", waiting: "waiting for permission" };
const RP_RECALL_WHY = {
  "context present": "the message already carried its recall",
  "empty body": "nothing to search (not a chat message)"
};
function rpRecallTitle(rc) {
  const names = (rc.items || []).map((i) => i.name).filter(Boolean);
  return names.length ? "recalled: " + names.join("; ") : "memories recalled for this message";
}
function RpRecallItem({ item, slug }) {
  const [body, setBody] = React.useState(null);
  const load = async (e) => {
    if (!e.currentTarget.open || body) return;
    const raw = item.collection === "raw";
    const [file, line] = raw ? String(item.rel || "").split("#") : [item.rel, null];
    const q = new URLSearchParams({ path: file || "" });
    if (raw && line) {
      q.set("start", line);
      q.set("count", "1");
    }
    if (item.layer === "harness") q.set("layer", "harness");
    const d = await apiGet(`/api/memory/${encodeURIComponent(slug)}/file?${q}`);
    if (!d) {
      setBody({ err: "could not read " + item.rel });
      return;
    }
    if (raw) {
      let entry = null;
      try {
        entry = JSON.parse((d.lines || [])[0] || "");
      } catch (_e) {
        entry = null;
      }
      setBody(entry ? { entry } : { text: (d.lines || []).join("\n") });
    } else {
      setBody({ text: d.text != null ? d.text : (d.lines || []).join("\n") + (d.more ? "\n..." : "") });
    }
  };
  const meta = [item.collection, item.rel].filter(Boolean).join(" · ");
  return /* @__PURE__ */ React.createElement("details", { className: "rp-recall-item", onToggle: load }, /* @__PURE__ */ React.createElement("summary", null, /* @__PURE__ */ React.createElement("span", { className: "rp-recall-name" }, item.name || item.rel), item.level && /* @__PURE__ */ React.createElement("span", { className: "rp-chip" }, String(item.level).replace(/^L\d_/, "").toLowerCase()), item.similarity != null && /* @__PURE__ */ React.createElement("span", { className: "rp-chip", title: "semantic similarity" }, Number(item.similarity).toFixed(2)), /* @__PURE__ */ React.createElement("span", { className: "rp-recall-src" }, meta)), /* @__PURE__ */ React.createElement("div", { className: "rp-recall-body" }, !body ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, "loading...") : body.err ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, body.err) : body.entry ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", null, body.entry.content), /* @__PURE__ */ React.createElement("div", { className: "rp-recall-src" }, [body.entry.truth_level, body.entry.timestamp, body.entry.cite && "cite: " + body.entry.cite].filter(Boolean).join(" · "))) : renderMarkdownLite(body.text || "").map((n, j) => rpToReact(n, j))));
}
function RpRecall({ recall, slug }) {
  const why = recall.timed_out ? "recall timed out" : RP_RECALL_WHY[recall.skipped] || recall.skipped;
  if (why) return /* @__PURE__ */ React.createElement("div", { className: "rp-recall muted" }, "No recall: ", why);
  if (!recall.hits) return /* @__PURE__ */ React.createElement("div", { className: "rp-recall muted" }, "No recall: nothing in memory matched");
  const items = recall.items || [];
  return /* @__PURE__ */ React.createElement("div", { className: "rp-recall" }, /* @__PURE__ */ React.createElement("div", { className: "rp-recall-head" }, "Recalled for this message"), items.length ? items.map((it, i) => /* @__PURE__ */ React.createElement(RpRecallItem, { key: i, item: it, slug })) : /* @__PURE__ */ React.createElement("div", { className: "rp-recall-src" }, recall.text || recall.hits + " hits (not listed: an older runner did not record them)"));
}
function RpRow({ row, now, activeTool, slug }) {
  const ev = row.ev || {};
  const p = ev.payload || {};
  switch (row.t) {
    case "turn": {
      const h = rpTurnHead(row);
      const rc = row.recall;
      const hits = rc && rc.hits;
      return /* @__PURE__ */ React.createElement("details", { className: "rp-turn" + (row.mid ? " mid" : "") }, /* @__PURE__ */ React.createElement("summary", { title: h.title || void 0 }, /* @__PURE__ */ React.createElement("span", { className: "rp-turn-head" }, h.head || "turn"), hits > 0 && /* @__PURE__ */ React.createElement("span", { className: "rp-chip rp-recall-chip", title: rpRecallTitle(rc) + " (open to see them)" }, "recall ", hits), /* @__PURE__ */ React.createElement("span", { className: "rp-turn-snip" }, h.snippet)), /* @__PURE__ */ React.createElement("div", { className: "rp-turn-full" }, h.full), rc && slug && /* @__PURE__ */ React.createElement(RpRecall, { recall: rc, slug }));
    }
    case "tool":
    case "reply": {
      const lab = rpToolLabel(p.name, p.input);
      const res = row.result && row.result.payload;
      const err = !!(res && res.is_error);
      const dur = row.result && row.result.ts && ev.ts ? row.result.ts - ev.ts : row.ms != null ? row.ms / 1e3 : null;
      const pending = !row.result;
      const mark = pending ? /* @__PURE__ */ React.createElement("span", { className: "rp-spin" + (activeTool ? "" : " rp-spin-idle") }) : /* @__PURE__ */ React.createElement("span", { className: err ? "rp-mark rp-err" : "rp-mark rp-ok" }, err ? "✗" : "✓");
      if (row.t === "reply") {
        const said = String((p.input || {}).text || "");
        const where = res && !err ? String(res.text || "").replace(/^replied to /, "") : err ? rpCut(res.text, 120) : "sending...";
        return /* @__PURE__ */ React.createElement("details", { className: "rp-card rp-reply" + (err ? " is-err" : "") }, /* @__PURE__ */ React.createElement("summary", null, mark, /* @__PURE__ */ React.createElement("span", { className: "rp-card-name" }, "reply"), /* @__PURE__ */ React.createElement("span", { className: "rp-card-desc" }, where), /* @__PURE__ */ React.createElement("span", { className: "rp-card-desc rp-reply-prev" }, rpCut(said.replace(/\s+/g, " "), 120))), /* @__PURE__ */ React.createElement("div", { className: "rp-card-body rp-sans" }, renderMarkdownLite(said).map((n, j) => rpToReact(n, j))));
      }
      return /* @__PURE__ */ React.createElement("details", { className: "rp-card" + (err ? " is-err" : "") }, /* @__PURE__ */ React.createElement("summary", { title: lab.server ? "MCP server " + lab.server : void 0 }, mark, /* @__PURE__ */ React.createElement("span", { className: "rp-card-name" }, lab.name), /* @__PURE__ */ React.createElement("span", { className: "rp-card-desc" }, lab.desc), dur != null && /* @__PURE__ */ React.createElement("span", { className: "rp-card-dur" }, rpElapsed(dur))), /* @__PURE__ */ React.createElement("div", { className: "rp-card-body" }, /* @__PURE__ */ React.createElement("div", { className: "rp-card-in" }, rpToolInput(p.input).map((n, j) => rpToReact(n, j))), res && /* @__PURE__ */ React.createElement("div", { className: "rp-card-out" }, rpToolOutput(res.text).map((n, j) => rpToReact(n, j)))));
    }
    case "thinking": {
      const label = "thought" + (row.secs != null ? " " + rpElapsed(row.secs) : "");
      if (!p.text) return /* @__PURE__ */ React.createElement("div", { className: "rp-line rp-thought" }, label);
      return /* @__PURE__ */ React.createElement("details", { className: "rp-thought-d" }, /* @__PURE__ */ React.createElement("summary", { className: "rp-thought" }, label), /* @__PURE__ */ React.createElement("div", { className: "rp-thought-text" }, p.text, p.truncated ? " [truncated]" : ""));
    }
    case "text":
      return /* @__PURE__ */ React.createElement("div", { className: "rp-text-row" }, rpRowBody(ev));
    case "error":
      return /* @__PURE__ */ React.createElement("div", { className: "rp-line rp-err" }, "error: ", String(p.error || ""));
    case "line":
      return /* @__PURE__ */ React.createElement("div", { className: "rp-line " + row.cls, title: row.detail || void 0 }, row.text);
    case "footer": {
      const parts = rpFooterParts(row.meta);
      const bad = row.meta.result && (row.meta.result.is_error || row.meta.result.interrupted);
      return /* @__PURE__ */ React.createElement("div", { className: "rp-footer" + (bad ? " is-err" : "") }, parts.join(" · "));
    }
    case "boot":
      return /* @__PURE__ */ React.createElement("details", { className: "rp-boot" }, /* @__PURE__ */ React.createElement("summary", null, "runner started · ", row.events.length, " setup ", row.events.length === 1 ? "event" : "events"), row.events.map((e, j) => /* @__PURE__ */ React.createElement("div", { key: j, className: "rp-boot-ev" }, /* @__PURE__ */ React.createElement("span", { className: "rp-kind" }, e.kind), " ", rpRowBody(e))));
    default:
      return /* @__PURE__ */ React.createElement("div", { className: "runner-ev rp-" + runnerKindClass(ev.kind) }, /* @__PURE__ */ React.createElement("span", { className: "rp-kind" }, ev.kind), /* @__PURE__ */ React.createElement("div", { className: "rp-body" }, rpRowBody(ev)));
  }
}
function RpBgTasks({ strip }) {
  const [open, setOpen] = React.useState(false);
  const [now, setNow] = React.useState(() => Date.now());
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!open) return void 0;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1e3);
    const away = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    const esc = (e) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      clearInterval(id);
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  if (strip.bg <= 0 && !open) return null;
  const list = rpTaskList(strip.tasks, 5);
  const secs = now / 1e3;
  return /* @__PURE__ */ React.createElement("span", { className: "rp-bg", ref }, /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      className: "rp-chip rp-bg-chip" + (open ? " on" : ""),
      "aria-expanded": open,
      title: "background tasks: click for the list",
      onClick: () => setOpen((o) => !o)
    },
    strip.bg,
    " bg ",
    strip.bg === 1 ? "task" : "tasks"
  ), open && /* @__PURE__ */ React.createElement("div", { className: "rp-tasks", role: "dialog", "aria-label": "background tasks" }, list.running.map((t) => /* @__PURE__ */ React.createElement("div", { key: t.id, className: "rp-task", title: t.desc }, /* @__PURE__ */ React.createElement("span", { className: "rp-spin" }), /* @__PURE__ */ React.createElement("span", { className: "rp-task-desc" }, t.desc || t.id), /* @__PURE__ */ React.createElement("span", { className: "rp-chip" }, rpTaskType(t.type)), t.tool && /* @__PURE__ */ React.createElement("span", { className: "rp-dim" }, rpToolLabel(t.tool).name), /* @__PURE__ */ React.createElement("span", { className: "rp-task-t" }, t.started ? rpElapsed(Math.max(0, secs - t.started)) : ""))), strip.bgUntracked > 0 && /* @__PURE__ */ React.createElement("div", { className: "rp-task rp-dim" }, strip.bgUntracked, " started with no details (recorded before 1.27)"), list.running.length === 0 && strip.bgUntracked === 0 && /* @__PURE__ */ React.createElement("div", { className: "rp-task rp-dim" }, "none running"), list.ended.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "rp-tasks-sep" }, "ended"), list.ended.map((t) => /* @__PURE__ */ React.createElement("div", { key: t.id, className: "rp-task is-ended", title: t.summary || t.desc }, /* @__PURE__ */ React.createElement("span", { className: "rp-mark " + (t.status === "completed" ? "rp-ok" : t.status === "failed" || t.status === "lost" ? "rp-err" : "rp-dim") }, t.status === "completed" ? "✓" : t.status === "failed" || t.status === "lost" ? "✗" : "■"), /* @__PURE__ */ React.createElement("span", { className: "rp-task-desc" }, t.desc || t.id), /* @__PURE__ */ React.createElement("span", { className: "rp-dim" }, t.status), /* @__PURE__ */ React.createElement("span", { className: "rp-task-t" }, fmtAgoShort(secs - t.ended))))));
}
function RpStrip({ strip, now, raw, setRaw }) {
  const a = strip.activity;
  const rates = Object.keys(strip.rate).map((t) => rpRate(t, strip.rate[t]));
  return /* @__PURE__ */ React.createElement("div", { className: "rp-strip" }, /* @__PURE__ */ React.createElement("span", { className: "rp-act" + (a ? " on" : "") }, a ? /* @__PURE__ */ React.createElement("span", { className: "rp-spin" }) : /* @__PURE__ */ React.createElement("span", { className: "rp-idle-dot" }), a ? a.kind === "tool" ? a.name : RP_ACT_LABEL[a.kind] || a.kind : (strip.state || "idle").replace(/_/g, " "), a && a.since ? /* @__PURE__ */ React.createElement("span", { className: "rp-act-t" }, rpElapsed(Math.max(0, now / 1e3 - a.since))) : null), rates.map((r) => /* @__PURE__ */ React.createElement("span", { key: r.label, className: "rp-rate is-" + r.level, title: "Claude usage limit, " + r.label + " window" + (r.resets ? ", resets " + r.resets : "") }, r.label, r.pct != null && /* @__PURE__ */ React.createElement("span", { className: "rp-bar" }, /* @__PURE__ */ React.createElement("span", { style: { width: r.pct + "%" } })), r.pct != null && /* @__PURE__ */ React.createElement("span", null, r.pct, "%"), r.resets && /* @__PURE__ */ React.createElement("span", { className: "rp-dim" }, "resets ", r.resets))), strip.session && strip.session.model && /* @__PURE__ */ React.createElement("span", { className: "rp-chip" + (strip.session.auth && strip.session.auth !== "your login" ? " is-warn" : ""), title: "from the session's init: model and where its credentials come from" }, strip.session.model.replace(/^claude-/, ""), strip.session.auth ? " · " + strip.session.auth : ""), strip.auth && /* @__PURE__ */ React.createElement("span", { className: "rp-chip is-err", title: rpAuthLine(strip.auth).text }, "login required"), strip.turns > 0 && /* @__PURE__ */ React.createElement("span", { className: "rp-dim", title: "tokens and estimated cost of the turns in this view" }, strip.turns, " ", strip.turns === 1 ? "turn" : "turns", " · ", rpTok(strip.tokens), " tok", strip.cost ? " · $" + strip.cost.toFixed(2) : ""), /* @__PURE__ */ React.createElement(RpBgTasks, { strip }), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("label", { className: "rp-raw", title: "one row per stream event, as it arrives" }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: raw, onChange: (e) => setRaw(e.target.checked) }), " raw"));
}
const _REVEALED = window.__bubbleRevealed = window.__bubbleRevealed || /* @__PURE__ */ new Set();
const _GLITCH_GLYPHS = "▓▒░█▚▞▛▜▟▙▂▅▆▇╬╫╪╧╦╩═║╠╣╔╗╚╝#@%&*+=/\\<>";
const _MATRIX_GLYPHS = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789";
const _BOOT_GLYPHS = "·.:°";
function _scrambleWithPalette(word, palette) {
  let out = "";
  for (const ch of word) {
    if (/\s/.test(ch)) {
      out += ch;
      continue;
    }
    out += palette[Math.floor(Math.random() * palette.length)];
  }
  return out;
}
const REACTION_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🎉", "🔥", "👀", "🙏", "✅"];
function groupReactions(rows) {
  const by = /* @__PURE__ */ new Map();
  for (const r of rows || []) {
    if (!r || !r.emoji) continue;
    let g = by.get(r.emoji);
    if (!g) {
      g = { emoji: r.emoji, count: 0, users: [] };
      by.set(r.emoji, g);
    }
    g.count += Number(r.tap_count || 1);
    if (r.user && !g.users.includes(r.user)) g.users.push(r.user);
  }
  return Array.from(by.values());
}
function ChatBubble({ msg, cousin, search, isLast, onReply, chatUser, mediaShown, onOpenMedia }) {
  const isUser = msg.type === "user";
  let replyQuote = null;
  if (msg.reply_to) {
    try {
      replyQuote = typeof msg.reply_to === "string" ? JSON.parse(msg.reply_to) : msg.reply_to;
    } catch (_) {
    }
  }
  const [reactions, setReactions] = React.useState(msg.reactions || []);
  React.useEffect(() => {
    setReactions(msg.reactions || []);
  }, [msg.reactions]);
  const react = async (emoji, action) => {
    if (!msg.id || !chatUser) return;
    try {
      const r = await fetch("/api/chat/reactions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cousin: cousin && cousin.slug,
          message_id: msg.id,
          user: chatUser,
          emoji,
          action: action || "tap"
        })
      });
      const d = await r.json();
      if (r.ok && Array.isArray(d.reactions)) setReactions(d.reactions);
    } catch (_) {
    }
  };
  const [menu, setMenu] = React.useState(null);
  React.useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (e) => {
      if (e.key === "Escape") setMenu(null);
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);
  const longPressTimer = React.useRef(null);
  const startLongPress = (e) => {
    const t = e.touches && e.touches[0];
    longPressTimer.current = setTimeout(() => {
      try {
        if (navigator.vibrate) navigator.vibrate(15);
      } catch (_) {
      }
      setMenu({ x: t ? t.clientX : 0, y: t ? t.clientY : 0 });
    }, 500);
  };
  const cancelLongPress = () => {
    if (longPressTimer.current) {
      clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  };
  const onContextMenu = (e) => {
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY });
  };
  const fullText = msg.message || "";
  const streamSpeed = window.__fwSettings && window.__fwSettings.chatStreamSpeed || "normal";
  const shouldStream = isLast && !isUser && !_REVEALED.has(msg.id) && fullText.length > 0 && streamSpeed !== "off";
  const [visible, setVisible] = React.useState(shouldStream ? "" : fullText);
  const [streaming, setStreaming] = React.useState(shouldStream);
  React.useEffect(() => {
    if (!shouldStream) return;
    const effect = window.__fwSettings && window.__fwSettings.chatStreamEffect || "plain";
    const pool = { slow: 3600, normal: 1800, fast: 900 }[streamSpeed] || 1800;
    if (effect === "typewriter") {
      const perChar = Math.max(6, Math.min(40, Math.floor(pool / Math.max(20, fullText.length))));
      let i2 = 0;
      const id2 = setInterval(() => {
        i2 += 1;
        if (i2 >= fullText.length) {
          setVisible(fullText);
          setStreaming(false);
          _REVEALED.add(msg.id);
          window.dispatchEvent(new CustomEvent("chat-stream-tick"));
          clearInterval(id2);
          return;
        }
        setVisible(fullText.slice(0, i2));
        window.dispatchEvent(new CustomEvent("chat-stream-tick"));
      }, perChar);
      return () => clearInterval(id2);
    }
    if (effect === "boot") {
      const chunks = fullText.match(/[\s\S]{1,18}/g) || [fullText];
      const perChunk = Math.max(28, Math.min(140, Math.floor(pool / Math.max(1, chunks.length))));
      let i2 = 0;
      const id2 = setInterval(() => {
        i2 += 1;
        if (i2 >= chunks.length) {
          setVisible(fullText);
          setStreaming(false);
          _REVEALED.add(msg.id);
          window.dispatchEvent(new CustomEvent("chat-stream-tick"));
          clearInterval(id2);
          return;
        }
        const head = chunks.slice(0, i2).join("");
        const tail = _scrambleWithPalette(chunks.slice(i2).join("").slice(0, 12), _BOOT_GLYPHS);
        setVisible(head + tail);
        window.dispatchEvent(new CustomEvent("chat-stream-tick"));
      }, perChunk);
      return () => clearInterval(id2);
    }
    const words = fullText.split(/(\s+)/);
    const total = Math.min(words.length, 400);
    const perStep = Math.max(8, Math.min(80, Math.floor(pool / Math.max(1, total))));
    const palette = effect === "glitch" ? _GLITCH_GLYPHS : effect === "matrix" ? _MATRIX_GLYPHS : null;
    const scrambleFrames = palette ? 2 : 0;
    const frameStep = palette ? Math.max(12, Math.floor(perStep / 2)) : perStep;
    let i = 0, frame = 0;
    const tick = () => {
      if (i >= words.length) {
        setVisible(fullText);
        setStreaming(false);
        _REVEALED.add(msg.id);
        window.dispatchEvent(new CustomEvent("chat-stream-tick"));
        clearInterval(id);
        return;
      }
      const revealedSoFar = words.slice(0, i).join("");
      const currentWord = words[i] || "";
      if (/^\s+$/.test(currentWord) || scrambleFrames === 0) {
        setVisible(revealedSoFar + currentWord);
        i += 1;
        frame = 0;
      } else if (frame < scrambleFrames) {
        setVisible(revealedSoFar + _scrambleWithPalette(currentWord, palette));
        frame += 1;
      } else {
        setVisible(revealedSoFar + currentWord);
        i += 1;
        frame = 0;
      }
      window.dispatchEvent(new CustomEvent("chat-stream-tick"));
    };
    const id = setInterval(tick, frameStep);
    return () => clearInterval(id);
  }, [msg.id, shouldStream, fullText, streamSpeed]);
  const rendered = React.useMemo(
    () => renderMarkdown(visible, search),
    [msg.id, visible, search]
  );
  const divRef = React.useRef(null);
  React.useEffect(() => {
    if (streaming) return;
    if (!divRef.current) return;
    const blocks = divRef.current.querySelectorAll("pre code.language-mermaid, code.language-mermaid");
    if (!blocks.length) return;
    blocks.forEach(async (block, i) => {
      const src = block.textContent;
      const id = `mmd-${msg.id}-${i}`;
      try {
        const mermaid = await window.loadLib("mermaid");
        if (!block.isConnected) return;
        const { svg } = await mermaid.render(id, src);
        const container = document.createElement("div");
        container.className = "mermaid-rendered";
        container.innerHTML = svg;
        const parent = block.closest("pre") || block;
        parent.replaceWith(container);
      } catch (e) {
      }
    });
  }, [rendered, streaming]);
  const media = attachmentMedia(msg);
  const chips = groupReactions(reactions);
  return /* @__PURE__ */ React.createElement("div", { "data-msg-id": msg.id, style: {
    marginBottom: 10,
    display: "flex",
    flexDirection: "column",
    alignItems: isUser ? "flex-end" : "flex-start"
  } }, /* @__PURE__ */ React.createElement(
    "div",
    {
      className: `chat-bubble ${isUser ? "chat-bubble-user" : "chat-bubble-assistant"} ${streaming ? "chat-streaming" : ""}`,
      onContextMenu,
      onTouchStart: startLongPress,
      onTouchEnd: cancelLongPress,
      onTouchMove: cancelLongPress,
      onTouchCancel: cancelLongPress
    },
    replyQuote && /* @__PURE__ */ React.createElement(
      "div",
      {
        style: {
          marginBottom: 6,
          padding: "4px 8px",
          borderLeft: "3px solid var(--accent)",
          background: "oklch(from var(--accent) l c h / 0.08)",
          borderRadius: 3,
          fontSize: 11,
          fontFamily: "var(--mono)",
          color: "var(--fg-3)",
          cursor: replyQuote.id ? "pointer" : "default"
        },
        title: replyQuote.id ? "click to scroll to original" : "",
        onClick: () => {
          if (!replyQuote.id) return;
          const el = document.querySelector(`[data-msg-id="${replyQuote.id}"]`);
          if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      },
      /* @__PURE__ */ React.createElement("div", { style: { color: "var(--accent)", fontWeight: 500 } }, "↩ ", replyQuote.user || "msg"),
      /* @__PURE__ */ React.createElement("div", { style: { whiteSpace: "nowrap", textOverflow: "ellipsis", overflow: "hidden" } }, replyQuote.snippet || "")
    ),
    media && /* @__PURE__ */ React.createElement("div", { className: "chat-media-slot", style: { marginBottom: visible ? 6 : 0 } }, /* @__PURE__ */ React.createElement(
      InlineMedia,
      {
        media,
        shown: mediaShown !== false,
        onOpen: onOpenMedia ? () => onOpenMedia(msg) : null
      }
    )),
    /* @__PURE__ */ React.createElement("div", { ref: divRef, className: "chat-md", dangerouslySetInnerHTML: { __html: rendered } }),
    /* @__PURE__ */ React.createElement("div", { className: "chat-meta" }, fmtShortTime(msg.timestamp))
  ), chips.length > 0 && /* @__PURE__ */ React.createElement("div", { style: {
    marginTop: 4,
    display: "flex",
    gap: 4,
    flexWrap: "wrap",
    justifyContent: isUser ? "flex-end" : "flex-start"
  } }, chips.map((r) => {
    const mine = !!chatUser && r.users.includes(chatUser);
    return /* @__PURE__ */ React.createElement(
      "button",
      {
        key: r.emoji,
        onClick: () => react(r.emoji, "tap"),
        onContextMenu: (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (mine) react(r.emoji, "remove");
        },
        title: r.users.join(", ") + (mine ? " (click: tap again; right-click: remove yours)" : " (click: tap)"),
        style: {
          fontSize: 12,
          padding: "2px 8px",
          borderRadius: 12,
          background: mine ? "oklch(from var(--accent) l c h / 0.22)" : "var(--bg-1)",
          border: mine ? "1px solid var(--accent)" : "1px solid var(--line)",
          color: "var(--fg-1)",
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          gap: 4
        }
      },
      /* @__PURE__ */ React.createElement("span", null, r.emoji),
      /* @__PURE__ */ React.createElement("span", { style: { fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-2)" } }, r.count)
    );
  })), menu && /* @__PURE__ */ React.createElement(
    "div",
    {
      onClick: (e) => e.stopPropagation(),
      style: {
        position: "fixed",
        left: menu.x,
        top: menu.y,
        background: "var(--bg-1)",
        border: "1px solid var(--line)",
        borderRadius: 10,
        padding: 6,
        zIndex: 9e3,
        boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
        display: "flex",
        flexDirection: "column",
        gap: 4,
        maxWidth: "92vw"
      }
    },
    onReply && /* @__PURE__ */ React.createElement(
      "button",
      {
        onClick: () => {
          onReply();
          setMenu(null);
        },
        className: "btn ghost",
        style: { fontSize: 12, padding: "4px 10px", textAlign: "left" }
      },
      "↩ Reply"
    ),
    /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 2, flexWrap: "wrap", maxWidth: 260 } }, REACTION_EMOJIS.map((e) => /* @__PURE__ */ React.createElement(
      "button",
      {
        key: e,
        onClick: () => {
          react(e, "tap");
          setMenu(null);
        },
        disabled: !chatUser,
        style: {
          fontSize: 18,
          lineHeight: 1,
          padding: "4px 6px",
          background: "transparent",
          border: "1px solid transparent",
          borderRadius: 4,
          cursor: "pointer"
        },
        onMouseEnter: (ev) => ev.currentTarget.style.background = "var(--bg-0)",
        onMouseLeave: (ev) => ev.currentTarget.style.background = "transparent"
      },
      e
    )))
  ));
}
function renderMarkdown(text, highlight) {
  let html;
  if (!window.marked) html = escapeHtml(text).replace(/\n/g, "<br/>");
  else {
    try {
      html = window.marked.parse(text, { breaks: true, gfm: true });
    } catch (e) {
      html = escapeHtml(text).replace(/\n/g, "<br/>");
    }
  }
  return highlight ? highlightHtml(html, highlight) : html;
}
function highlightHtml(html, query) {
  const q = (query || "").trim();
  if (!q) return html;
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp("(" + escaped + ")", "gi");
  let out = "", i = 0;
  while (i < html.length) {
    if (html[i] === "<") {
      const end = html.indexOf(">", i);
      if (end === -1) {
        out += html.slice(i);
        break;
      }
      out += html.slice(i, end + 1);
      i = end + 1;
    } else {
      const nextTag = html.indexOf("<", i);
      const seg = nextTag === -1 ? html.slice(i) : html.slice(i, nextTag);
      out += seg.replace(re, '<mark class="hl-match">$1</mark>');
      i = nextTag === -1 ? html.length : nextTag;
    }
  }
  return out;
}
function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[ch]);
}
function fmtAgoShort(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 2) return "just now";
  if (sec < 60) return sec + "s ago";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  return Math.floor(sec / 3600) + "h ago";
}
function fmtShortTime(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString(void 0, { hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return "";
  }
}
Object.assign(window, { ChatView, ChatBubble, PaneView, RunnerPaneView, rpModel, rpCompact, runnerEventLine, runnerEventBody, paneLiveness, runnerFleetKey, renderMarkdown, resolveChatUser, groupReactions });
