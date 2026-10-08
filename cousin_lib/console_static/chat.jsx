// Chat view: the per-cousin chat widget and the live terminal pane.
// Renders markdown via marked, diagrams via mermaid. Polls /api/messages
// every 3.5 s in live mode. Every route it calls is in docs/reference/console-api.md.

// The chat user is the thread the view reads and writes: `?user=` on the
// page (passed in as embedUser), else the cousin's configured operator,
// else the console session's user. With none of the three the composer is
// disabled and says why: there is no default operator anywhere.
// Everything term.reset() used to do for a full pane frame, as escape
// codes, so the clear and the repaint land in one render.
const FRAME_PREFIX = "\x1b[0m\x1b[r\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
  + "\x1b[?25h\x1b[H\x1b[2J\x1b[3J";

function resolveChatUser(embedUser, cousin, sessionUser) {
  return embedUser || (cousin && cousin.operator) || sessionUser || "";
}

// The chat column's width below which its header is the compact one row
// (the phone's): the desktop header needs about this much for one row with
// the pane open, so any narrower and it would wrap onto two or three.
const CHAT_COMPACT_PX = 740;
// How near an edge of the split a dragged divider snaps: the chat's left
// edge collapses the chat, the right edge closes the pane.
const SPLIT_SNAP_PX = 40;
function splitSnap(x, left, right, zone = SPLIT_SNAP_PX) {
  if (!(right > left)) return null;
  if (x <= left + zone) return "chat";
  if (x >= right - zone) return "pane";
  return null;
}

// POST /api/chat/send: a stored chat message, as the composer sends it
// (the pane's say box reuses it for "send as chat"). Resolves to
// { ok: true, data } or { ok: false, error }; a network error throws.
async function postChatMessage(body) {
  const r = await fetch("/api/chat/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) return { ok: false, error: d.error || `HTTP ${r.status}` };
  return { ok: true, data: d };
}

function ChatView({ activeCousin, cousins, embedUser, embed, sessionUser }) {
  const c = (cousins || []).find(x => x.slug === activeCousin);
  // The session user is a prop when the shell knows it; otherwise ask once.
  const [me, setMe] = React.useState(sessionUser || "");
  React.useEffect(() => {
    if (sessionUser !== undefined) { setMe(sessionUser || ""); return; }
    let cancelled = false;
    fetch("/api/auth/me", { cache: "no-store" })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (!cancelled && d) setMe(d.user || ""); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [sessionUser]);
  const chatUser = resolveChatUser(embedUser, c, me);
  // The pane sits beside the chat (it used to replace it, under the key
  // fw_pane_open): you watch the cousin work while you talk to it. With
  // no choice stored yet it opens wherever both fit.
  const [paneOpen, setPaneOpen] = React.useState(() => {
    try {
      const v = localStorage.getItem("fw_pane_side");
      if (v === "1" || v === "0") return v === "1";
    } catch (e) { /* storage unavailable: fall through to the default */ }
    return typeof window !== "undefined" && window.innerWidth > 1100;
  });
  const [search, setSearch] = React.useState("");
  const [toast, setToast] = React.useState(null);
  const toastTimerRef = React.useRef(null);
  React.useEffect(() => () => { if (toastTimerRef.current) clearTimeout(toastTimerRef.current); }, []);
  const scheduleToastClear = (ms) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(null), ms);
  };
  const [archiveArmed, setArchiveArmed] = React.useState(false);
  const [showArchived, setShowArchived] = React.useState(false);
  const [fullscreen, setFullscreen] = React.useState(() => {
    try { return localStorage.getItem("fw_chat_fullscreen") === "1"; }
    catch (e) { return false; }
  });
  // Media on/off: a browser preference (media.jsx), default on.
  const [mediaShown, setMediaShown] = React.useState(() => readMediaShown());
  React.useEffect(() => { writeMediaShown(mediaShown); }, [mediaShown]);
  // The pane column's tab: "pane" (the reasoning stream or the terminal) or a
  // plugin's name (plugins.jsx). Without a plugin tab there is no strip at all.
  // A plugin page placed "chat" is not a tab: it is a strip over the messages.
  const [paneTab, setPaneTab] = React.useState("pane");

  // Per-tab last-viewed marker: stamp localStorage whenever the operator
  // is on this cousin's chat tab + the document is visible. This is what
  // the sidebar's unread-dot logic compares against c.lastMsgTs.
  React.useEffect(() => {
    if (!activeCousin || !chatUser) return;
    const key = chatSeenKey(activeCousin, chatUser);
    const stamp = () => {
      if (document.visibilityState === "visible") {
        try { localStorage.setItem(key, String(Math.floor(Date.now()/1000))); }
        catch (e) { /* ignore */ }
      }
    };
    stamp();
    document.addEventListener("visibilitychange", stamp);
    return () => document.removeEventListener("visibilitychange", stamp);
  }, [activeCousin, chatUser]);

  React.useEffect(() => {
    try { localStorage.setItem("fw_pane_side", paneOpen ? "1" : "0"); }
    catch (e) { /* ignore */ }
  }, [paneOpen]);
  // The pane's share of the width, dragged on the divider between the two
  // and kept per browser; a double-click puts it back to half.
  const [paneW, setPaneW] = React.useState(() => {
    try {
      const v = Number(localStorage.getItem("fw_pane_w"));
      if (v >= 20 && v <= 80) return v;
    } catch (e) { /* storage unavailable */ }
    return 50;
  });
  React.useEffect(() => {
    try { localStorage.setItem("fw_pane_w", String(Math.round(paneW * 10) / 10)); }
    catch (e) { /* ignore */ }
  }, [paneW]);
  // The divider also snaps: let go within SPLIT_SNAP_PX of the
  // chat's left edge and the chat collapses, the pane covering the whole
  // area; a strip at that edge brings it back. Let go within SPLIT_SNAP_PX
  // of the right edge and the pane closes, as its x does. Either way the
  // split keeps the width it had before the drag (fw_pane_w), so the chat
  // comes back at its last width; the collapse is kept per browser too.
  const [chatCollapsed, setChatCollapsed] = React.useState(() => {
    try { return localStorage.getItem("fw_chat_collapsed") === "1"; }
    catch (e) { return false; }
  });
  React.useEffect(() => {
    try { localStorage.setItem("fw_chat_collapsed", chatCollapsed ? "1" : "0"); }
    catch (e) { /* ignore */ }
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
  // The header goes compact when the chat column is narrow, not only on a phone.
  const [chatColRef, narrowCol] = window.useNarrowerThan ? useNarrowerThan(CHAT_COMPACT_PX) : [null, false];
  React.useEffect(() => {
    try { localStorage.setItem("fw_chat_fullscreen", fullscreen ? "1" : "0"); }
    catch (e) { /* ignore */ }
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
        body: JSON.stringify({ cousin: c.slug, user: chatUser, keep: 0 }),
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
    scheduleToastClear(3000);
  };

  if (!c) {
    return (
      <div style={{ padding: 24, color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 12 }}>
        no cousin selected
      </div>
    );
  }

  // A remote cousin (a hive node on another machine) has no pane here:
  // the console does not run it, it only proxies its chat.
  const paneShown = paneOpen && !c.remote;
  // The chat is collapsed only while the pane is there to cover it.
  const chatHidden = paneShown && chatCollapsed;
  const closePane = () => { setPaneOpen(false); setChatCollapsed(false); };
  const pluginTabAll = window.pluginTabs ? window.pluginTabs(c) : [];
  const pluginTabList = pluginTabAll.filter(t => t.placement !== "chat");
  // The "chat" pages, over the messages; not in an embed (no pane there either).
  const chatStrips = embed || c.remote ? [] : pluginTabAll.filter(t => t.placement === "chat");
  const activeTab = pluginTabList.find(t => t.name === paneTab) || null;

  return (
    <div style={{ position: "relative", height: "100%", minHeight: 0 }}>
      {/* The chat and the pane side by side: the pane is the runner's
          reasoning stream (with the interrupt) or the tmux terminal. On a
          phone the open pane takes the whole width, as it always did. */}
      <div ref={splitRef} className={"chat-split" + (paneShown ? " pane-open" : "") + (chatHidden ? " chat-collapsed" : "")} style={{ "--pane-w": paneW + "%" }}>
        <div className="chat-col" ref={chatColRef} aria-hidden={chatHidden || undefined}>
          <ChatHeader cousin={c} chatUser={chatUser} paneOpen={paneShown} setPaneOpen={setPaneOpen} search={search} setSearch={setSearch} onArchive={onArchive} fullscreen={fullscreen} setFullscreen={embed ? null : setFullscreen} embed={embed} showArchived={showArchived} setShowArchived={setShowArchived} mediaShown={mediaShown} setMediaShown={setMediaShown} narrow={narrowCol} />
          {fullscreen && (
            <button className="chat-fullscreen-exit"
                    onClick={() => setFullscreen(false)}
                    title="exit fullscreen">x exit</button>
          )}
          {chatStrips.length > 0 && window.PluginChatStrips && (
            <PluginChatStrips slug={c.slug} tabs={chatStrips} />
          )}
          {/* key by slug: remount ChatBody on cousin switch so its messages +
              draft state reset to empty. Without this React reuses the instance
              and the previous cousin's messages render until the new fetch lands
              -- a cross-cousin content leak between private chats. */}
          <ChatBody key={c.slug + "|" + chatUser} cousin={c} search={search} setSearch={setSearch} chatUser={chatUser} showArchived={showArchived} mediaShown={mediaShown} />
          {toast && <div className="chat-toast">{toast}</div>}
        </div>
        {paneShown && !chatHidden && (
          <div className="split-div" onPointerDown={onDividerDown} onDoubleClick={() => setPaneW(50)}
               title="drag to resize, double-click to reset; drag to an edge to collapse" role="separator" aria-orientation="vertical" />
        )}
        {chatHidden && (
          <button className="chat-restore" onClick={() => setChatCollapsed(false)}
                  title="show the chat" aria-label="show the chat">&rsaquo;</button>
        )}
        <div className={`pane-col ${paneShown ? "open" : ""}`}>
          {paneShown && pluginTabList.length > 0 && window.PluginPaneTabs && (
            <PluginPaneTabs tabs={pluginTabList} active={activeTab ? activeTab.name : "pane"}
                            onPick={setPaneTab} paneLabel={c.runner ? "reasoning" : "terminal"} />
          )}
          {paneShown && activeTab && window.PluginFrame && (
            <PluginFrame key={c.slug + "|" + activeTab.name} tab={activeTab} />
          )}
          {paneShown && !activeTab && (c.runner
            ? <RunnerPaneView key={c.slug} cousin={c} onClose={closePane} chatUser={chatUser} chatHidden={chatHidden} />
            : <PaneView cousin={c} onClose={closePane} />)}
        </div>
      </div>
    </div>
  );
}

function ChatHeader({ cousin, chatUser, paneOpen, setPaneOpen, search, setSearch, onArchive, fullscreen, setFullscreen, embed, showArchived, setShowArchived, mediaShown, setMediaShown, narrow }) {
  const btnH = 28;  // shared height for input + buttons
  // A remote cousin's node serves send and history only: no effort to
  // set, no archive, no pane.
  const remote = !!cousin.remote;

  // Effort: the levels come from the server (GET /api/spawn/options),
  // the current value from the cousin row, and a change persists to
  // cousin.toml through the effort route: [agent] for a runner cousin,
  // [runtime] for a tmux one. The running agent
  // keeps the level it started with, so a saved change shows "restart
  // to apply" rather than pretending it is live.
  const [efforts, setEfforts] = React.useState([]);
  const [laneKeys, setLaneKeys] = React.useState(null);
  const [effort, setEffort] = React.useState(cousin.effort || "");
  const [effortHint, setEffortHint] = React.useState(null);
  React.useEffect(() => { setEffort(cousin.effort || ""); setEffortHint(null); }, [cousin.effort, cousin.slug]);
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      const d = await apiGet("/api/spawn/options");
      if (cancelled || !d) return;
      setEfforts(d.efforts || []);
      setLaneKeys(d.lane_keys || {});
      setEffort(e => e || d.default_effort || "");
    })();
    return () => { cancelled = true; };
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

  // Shown only on a lane that reads effort (agent.jsx agentLaneReads, from the
  // lane_keys the options route serves): opencode and fake read none. The
  // route writes it through the lane's own path (spawn.persist_agent_values
  // for a runner cousin).
  const readsEffort = !laneKeys || !window.agentLaneReads
    || window.agentLaneReads(cousin.lane, "effort", laneKeys);

  // On a phone, or in a chat column narrower than CHAT_COMPACT_PX (the
  // reasoning pane open beside it), and not in an embed (which has its own
  // one-line header), the toolbar is one row: effort, a search button that
  // opens the field, a "⋯" menu with archive / archived / media, then the
  // pane and the fullscreen toggles as icons. Every desktop control stays
  // reachable.
  const compact = ((window.useMobileLayout ? useMobileLayout() : false) || !!narrow) && !embed;
  const [searchOpen, setSearchOpen] = React.useState(() => !!search);
  const [menuOpen, setMenuOpen] = React.useState(false);
  const menuRef = React.useRef(null);
  React.useEffect(() => {
    if (!menuOpen) return undefined;
    const away = (e) => { if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false); };
    const esc = (e) => { if (e.key === "Escape") setMenuOpen(false); };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [menuOpen]);
  React.useEffect(() => { setMenuOpen(false); }, [cousin.slug, compact]);
  const closeSearch = () => { setSearch(""); setSearchOpen(false); };

  if (compact && searchOpen && !remote) {
    return (
      <div className="chat-header ch-mobile ch-searching">
        <input
          className="chat-search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          onKeyDown={e => { if (e.key === "Escape") closeSearch(); }}
          placeholder="search..."
          aria-label="search this chat"
          autoFocus
        />
        <button className="btn ghost ch-icon ch-search-close" onClick={closeSearch}
                title="close the search" aria-label="close the search">&times;</button>
      </div>
    );
  }

  if (compact) {
    const menuItems = !remote || !!setMediaShown;
    return (
      <div className="chat-header ch-mobile">
        {remote && <span className="ch-sub" title={`a hive node at ${cousin.host}:${cousin.port}`}>remote</span>}
        {!remote && readsEffort && (
          <label className="ch-effort">
            <select
              value={effort}
              onChange={e => applyEffort(e.target.value)}
              disabled={!efforts.length}
              title={cousin.runner
                ? "effort: cousin.toml [agent] effort, read by the runner at the next start"
                : "effort: cousin.toml [runtime] effort, rendered into the agent command at the next start"}
              aria-label="effort"
              className="sel-inline"
            >
              {!efforts.length && <option value={effort}>{effort || "..."}</option>}
              {efforts.map(l => <option key={l} value={l}>{l}</option>)}
            </select>
            {effortHint && (
              <span className="ch-effort-hint" title={effortHint}
                    style={{ color: effortHint.startsWith("failed") ? "var(--red)" : "var(--fg-2)" }}>
                {effortHint}
              </span>
            )}
          </label>
        )}
        <span className="ch-fill" />
        {!remote && (
          <button className={"btn ghost ch-icon ch-search-open" + (search ? " active" : "")}
                  onClick={() => setSearchOpen(true)}
                  title="search this chat" aria-label="search this chat">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5L14 14" />
            </svg>
          </button>
        )}
        {menuItems && (
          <span className="ch-more-wrap" ref={menuRef}>
            <button className={"btn ghost ch-icon ch-more" + (showArchived || !mediaShown ? " active" : "")}
                    onClick={() => setMenuOpen(v => !v)}
                    aria-haspopup="menu" aria-expanded={menuOpen}
                    title="more: archive, archived, media" aria-label="more">
              <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true">
                <circle cx="3" cy="8" r="1.4" /><circle cx="8" cy="8" r="1.4" /><circle cx="13" cy="8" r="1.4" />
              </svg>
            </button>
            {menuOpen && (
              <div className="ch-menu" role="menu">
                <div className="ch-menu-who">chat &middot; @{cousin.slug}{chatUser ? ` as ${chatUser}` : ""}</div>
                {!remote && (<>
                <button
                  role="menuitem"
                  className="btn ghost"
                  onClick={onArchive}
                  disabled={!chatUser}
                  title="archive ALL active messages"
                >archive</button>
                <button
                  role="menuitem"
                  className={"btn ghost" + (showArchived ? " active" : "")}
                  onClick={() => { setShowArchived(v => !v); setMenuOpen(false); }}
                  title={showArchived ? "showing archived messages, click to return to live" : "browse archived messages"}
                >{showArchived ? "live" : "archived"}</button>
                </>)}
                {setMediaShown && (
                  <button
                    role="menuitem"
                    className={"btn ghost chat-media-toggle" + (mediaShown ? "" : " active")}
                    onClick={() => { setMediaShown(v => !v); setMenuOpen(false); }}
                    aria-pressed={!mediaShown}
                    title={mediaShown ? "media shown: click to hide images, videos and audio" : "media hidden: click to show"}
                  >{mediaShown ? "media on" : "media off"}</button>
                )}
              </div>
            )}
          </span>
        )}
        {!paneOpen && !remote && (
          <button className="btn ghost ch-icon ch-pane-open"
                  onClick={() => setPaneOpen(true)}
                  title={cousin.runner ? "show the reasoning stream" : "show the terminal pane"}
                  aria-label={cousin.runner ? "reasoning" : "terminal"}>
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true">
              <rect x="2" y="3" width="12" height="10" rx="1" /><path d="M9.5 3v10M11 6h1.5M11 8.5h1.5" />
            </svg>
          </button>
        )}
        {setFullscreen && (
          <button
            className="btn ghost ch-icon chat-fullscreen-toggle"
            onClick={() => setFullscreen(v => !v)}
            title={fullscreen ? "exit fullscreen" : "fullscreen chat"}
            aria-label={fullscreen ? "exit fullscreen" : "fullscreen chat"}
          >
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
              <path d="M9.5 2.5h4v4M13.5 2.5L9 7M6.5 13.5h-4v-4M2.5 13.5L7 9" />
            </svg>
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="chat-header">
      <span className="ch-title">
        <span className="ch-sub">chat &middot; @{cousin.slug}{chatUser ? ` as ${chatUser}` : ""}</span>
        {remote && <span className="ch-sub" title={`a hive node at ${cousin.host}:${cousin.port}`}>&middot; remote</span>}
      </span>
      <span style={{ flex: 1 }} />
      {!embed && !remote && readsEffort && (
        <label className="ch-effort">
          <span>effort</span>
          <select
            value={effort}
            onChange={e => applyEffort(e.target.value)}
            disabled={!efforts.length}
            title={cousin.runner
              ? "cousin.toml [agent] effort: read by the runner at the next start"
              : "cousin.toml [runtime] effort: rendered into the agent command at the next start"}
            className="sel-inline"
            style={{ height: btnH }}
          >
            {!efforts.length && <option value={effort}>{effort || "..."}</option>}
            {efforts.map(l => <option key={l} value={l}>{l}</option>)}
          </select>
          {effortHint && (
            <span style={{ color: effortHint.startsWith("failed") ? "var(--red)" : "var(--fg-2)" }}>
              {effortHint}
            </span>
          )}
        </label>
      )}
      {!remote && (<>
      <button
        className="btn ghost"
        onClick={onArchive}
        disabled={!chatUser}
        title="archive ALL active messages"
        style={{ height: btnH }}
      >archive</button>
      <button
        className={"btn ghost" + (showArchived ? " active" : "")}
        onClick={() => setShowArchived(v => !v)}
        title={showArchived ? "showing archived messages, click to return to live" : "browse archived messages"}
        style={{ height: btnH }}
      >{showArchived ? "live" : "archived"}</button>
      </>)}
      {setMediaShown && (
        <button
          className={"btn ghost chat-media-toggle" + (mediaShown ? "" : " active")}
          onClick={() => setMediaShown(v => !v)}
          aria-pressed={!mediaShown}
          title={mediaShown ? "media shown: click to hide images, videos and audio" : "media hidden: click to show"}
          style={{ height: btnH }}
        >{mediaShown ? "media on" : "media off"}</button>
      )}
      {!remote && (
      <input
        className="chat-search"
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="search..."
        style={{ height: btnH }}
      />
      )}
      {!paneOpen && !embed && !remote && (
        <button
          className="btn ghost"
          onClick={() => setPaneOpen(true)}
          title={cousin.runner ? "show the reasoning stream beside the chat" : "show the terminal pane beside the chat"}
          style={{ height: btnH }}
        >{cousin.runner ? "reasoning" : "terminal"} &rsaquo;</button>
      )}
      {setFullscreen && !embed && (
        <button
          className="btn ghost chat-fullscreen-toggle"
          onClick={() => setFullscreen(v => !v)}
          title={fullscreen ? "exit fullscreen" : "fullscreen chat"}
          style={{ height: btnH }}
        >{fullscreen ? "⤢ exit" : "⤢"}</button>
      )}
    </div>
  );
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

  // Collect highlighted match elements after each render in search mode, update index
  React.useEffect(() => {
    if (!scrollerRef.current) { matchElsRef.current = []; return; }
    if (!searchActive) { matchElsRef.current = []; setMatchIndex(0); return; }
    const marks = Array.from(scrollerRef.current.querySelectorAll(".hl-match"));
    matchElsRef.current = marks;
    setMatchIndex(i => (marks.length === 0 ? 0 : Math.min(i, marks.length - 1)));
  }, [messages, search, searchActive]);

  // When matchIndex changes, scroll that match into view + mark it as current
  React.useEffect(() => {
    const marks = matchElsRef.current;
    if (!marks || marks.length === 0) return;
    marks.forEach((el, i) => el.classList.toggle("current", i === matchIndex));
    const el = marks[matchIndex];
    if (el && el.scrollIntoView) {
      atBottomRef.current = false;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [matchIndex, messages, search]);

  const matchCount = matchElsRef.current.length;
  const goPrev = () => {
    if (matchCount === 0) return;
    setMatchIndex(i => (i - 1 + matchCount) % matchCount);
  };
  const goNext = () => {
    if (matchCount === 0) return;
    setMatchIndex(i => (i + 1) % matchCount);
  };

  React.useEffect(() => {
    if (!c) return;
    if (!chatUser) { setLoaded(true); return; }
    let cancelled = false;
    async function pull() {
      try {
        const url = searchActive
          ? `/api/search?cousin=${c.slug}&q=${encodeURIComponent(search.trim())}&user=${encodeURIComponent(chatUser)}${showArchived ? "&archived=all" : ""}`
          : `/api/messages?cousin=${c.slug}&limit=500&user=${encodeURIComponent(chatUser)}${showArchived ? "&archived=1" : ""}`;
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        if (cancelled) return;
        setError(d.error || null);
        // Both routes answer { messages: [...] }; search is newest-first,
        // so reverse it into chat order.
        const list = searchActive ? (d.messages || []).slice().reverse() : (d.messages || []);
        setMessages(list);
        setTotalCount(typeof d.total === "number" ? d.total : list.length);
        setLoaded(true);
      } catch (e) {
        if (!cancelled) { setError(String(e.message || e)); setLoaded(true); }
      }
    }
    pull();
    // Only poll in live mode; keep search results stable while typing.
    // 3.5s rather than 2s: a 500-message payload over a slow link makes
    // every saved poll real, and a new reply still shows within 4s.
    const id = !searchActive ? setInterval(pull, 3500) : null;
    return () => { cancelled = true; if (id) clearInterval(id); };
  }, [c.slug, chatUser, search, searchActive, showArchived]);

  // Follow the tail when a NEW message arrives, unless the user is clearly
  // reading history. Keying on the tail message id means we don't re-snap on
  // every poll, and we don't depend on atBottomRef (which gets stomped by
  // scroll events, so a cousin's replies never triggered autoscroll).
  const chatAnchorRef = React.useRef(null);
  const lastTailIdRef = React.useRef(null);
  // Reset the tail tracker when the user switches cousin so that the first
  // paint of the new chat treats it as "first paint" and snaps to bottom.
  React.useEffect(() => {
    lastTailIdRef.current = null;
  }, [cousin?.slug, chatUser]);
  React.useEffect(() => {
    if (!messages.length) return;
    const last = messages[messages.length - 1];
    const tailId = last?.id ?? last?.timestamp ?? null;
    const prev = lastTailIdRef.current;
    lastTailIdRef.current = tailId;
    if (prev === tailId) return;                              // same batch, nothing new
    const el = scrollerRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    // First-paint (prev===null) always snaps; otherwise stay put if user has scrolled up far.
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
  // Load-more on scroll-to-top: when the user reaches the top of the
  // history pane, fetch the previous page using the lowest known id as
  // a `before=` cursor and prepend. Preserves scroll position by
  // anchoring on the previously-top message height.
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
      const more = (d.messages || []).filter(m => m.id < earliest);
      if (more.length) {
        const el = scrollerRef.current;
        const prevH = el ? el.scrollHeight : 0;
        setMessages(prev => [...more, ...prev]);
        // After React paints, restore relative scroll position.
        requestAnimationFrame(() => {
          if (el) el.scrollTop = el.scrollHeight - prevH;
        });
      }
    } catch (_) {}
    finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  };
  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    const near = (el.scrollHeight - el.scrollTop - el.clientHeight) < 80;
    atBottomRef.current = near;
    setAtBottom(near);
    if (el.scrollTop < 60 && !loadingMoreRef.current) loadMore();
  };

  // Follow the cursor while a cousin reply is revealing - but only if the user
  // is already near the bottom. If they scrolled up to re-read history, leave
  // them in peace.
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
  // Keep the tail in view when the list's box changes height while at the
  // bottom (a plugin strip over the chat dragged or collapsed, the window).
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

  // The media viewer: a snapshot of the thread's images and videos taken
  // at the click, so a poll landing while it is open does not move it.
  const [viewer, setViewer] = React.useState(null);  // {items, start}
  const openMedia = React.useCallback((msg) => {
    const items = threadMedia(messages);
    const at = items.findIndex(x => x.id === msg.id);
    if (at < 0) return;
    setViewer({ items, start: at });
  }, [messages]);
  const closeViewer = React.useCallback(() => setViewer(null), []);
  React.useEffect(() => { if (!mediaShown) setViewer(null); }, [mediaShown]);

  const [attachment, setAttachment] = React.useState(null); // {dataUrl, name}
  const [replyingTo, setReplyingTo] = React.useState(null);  // {id, user, snippet}
  const fileInputRef = React.useRef(null);

  const pickImage = () => fileInputRef.current && fileInputRef.current.click();
  const acceptImageFile = (f, label) => {
    if (!f) return;
    if (!f.type || !f.type.startsWith("image/")) {
      setError("only image files supported here"); return;
    }
    if (f.size > 8 * 1024 * 1024) {
      setError("image too large (>8 MB)"); return;
    }
    const reader = new FileReader();
    reader.onload = () => setAttachment({ dataUrl: reader.result, name: f.name || label || "image" });
    reader.readAsDataURL(f);
  };
  const onFile = (e) => {
    acceptImageFile(e.target.files && e.target.files[0]);
    e.target.value = "";
  };
  // paste-from-clipboard: when the user pastes inside the composer, walk
  // clipboardData.items for the first image/* and capture it.
  const onPaste = (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
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
  // drag-drop: drop an image anywhere on the composer and it lands in
  // the attachment slot.
  const [dragOver, setDragOver] = React.useState(false);
  const onDragOver = (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.items || []).some(i => (i.type || "").startsWith("image/"))) {
      e.preventDefault(); setDragOver(true);
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

  const send = async () => {
    const text = draft.trim();
    if ((!text && !attachment) || sending || !c || !chatUser) return;
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
      // Collapse the textarea back to single-line height after send.
      if (textareaRef.current) textareaRef.current.style.height = "36px";
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setSending(false);
    }
  };

  const onKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  };

  const noUser = !chatUser;
  const showSecondaryBar = searchActive || !loaded || !!error || noUser;
  return (
    <React.Fragment>
      {showSecondaryBar && (
        <div style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "6px 24px", borderBottom: "1px solid var(--hair)",
          fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)",
          background: searchActive ? "oklch(from var(--accent) 0.25 0.06 h)" : "var(--bg-1)",
        }}>
          {searchActive ? (
            <>
              <span style={{ color: "var(--accent, var(--cyan))", fontWeight: 600 }}>
                search: "{search}" &middot; {matchCount > 0 ? `${matchIndex + 1}/${matchCount} hits in ${messages.length} msg${messages.length !== 1 ? "s" : ""}` : `no hits in ${messages.length} msg${messages.length !== 1 ? "s" : ""}`}
              </span>
              <span style={{ flex: 1 }} />
              <button className="btn ghost" onClick={goPrev} disabled={matchCount === 0} style={{ padding: "2px 8px", minHeight: 20, fontSize: 12 }} title="previous match">&uarr;</button>
              <button className="btn ghost" onClick={goNext} disabled={matchCount === 0} style={{ padding: "2px 8px", minHeight: 20, fontSize: 12 }} title="next match">&darr;</button>
              <button className="btn ghost" onClick={() => setSearch && setSearch("")} style={{ padding: "2px 8px", minHeight: 20, fontSize: 10 }} title="clear search">clear x</button>
            </>
          ) : noUser ? (
            <span>no chat user: pass ?user=, set [operator] name in this cousin's cousin.toml, or log in</span>
          ) : (
            <span>{loaded ? error : "loading..."}</span>
          )}
        </div>
      )}
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        style={{
          flex: 1, overflowY: "auto", overflowX: "hidden", padding: "18px 24px 10px",
          background: "var(--bg-0)",
          fontFamily: "var(--mono)", fontSize: 13, lineHeight: 1.55,
          minHeight: 0, minWidth: 0,
        }}
      >
        {!loaded && (
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>loading messages from @{c.slug}...</div>
        )}
        {loaded && messages.length === 0 && !error && !noUser && (
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
            {searchActive ? `no matches for "${search}"` : "no messages yet. send one below."}
          </div>
        )}
        {messages.map((m, i) => (
          <ChatBubble
            key={m.id || m.timestamp}
            msg={m}
            cousin={c}
            search={searchActive ? search : ""}
            isLast={i === messages.length - 1}
            chatUser={chatUser}
            mediaShown={mediaShown !== false}
            onOpenMedia={openMedia}
            onReply={() => setReplyingTo({
              id: m.id, user: m.user,
              snippet: (m.message || "").replace(/\s+/g, " ").slice(0, 120),
            })}
          />
        ))}
        <div ref={chatAnchorRef} style={{ height: 1 }} />
      </div>
      {viewer && <MediaViewer items={viewer.items} start={viewer.start} onClose={closeViewer} />}
      {!atBottom && (
        <button
          onClick={jumpToBottom}
          title="jump to latest"
          style={{
            position: "absolute", bottom: 76, right: 18,
            width: 36, height: 36, borderRadius: "50%",
            background: "var(--bg-2)", color: "var(--fg-0)",
            border: "1px solid var(--line)",
            cursor: "pointer",
            display: "inline-flex", alignItems: "center", justifyContent: "center",
            fontSize: 16, lineHeight: 1,
            boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
            zIndex: 5,
          }}
        >&darr;</button>
      )}
      <div
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className="chat-composer"
        style={{
        background: dragOver ? "oklch(from var(--accent) l c h / 0.18)" : "var(--bg-0)",
        outline: dragOver ? "2px dashed var(--accent)" : "none",
        outlineOffset: -4,
      }}>
        {replyingTo && (
          <div style={{ display: "flex", alignItems: "stretch", gap: 8, padding: 6,
                        background: "var(--bg-1)", border: "1px solid var(--hair)", borderRadius: 8,
                        borderLeft: "3px solid var(--accent)" }}>
            <div style={{ flex: 1, fontSize: 11, fontFamily: "var(--mono)", color: "var(--fg-2)",
                          cursor: "pointer", overflow: "hidden" }}
                 title="click to scroll to original"
                 onClick={() => {
                   const el = document.querySelector(`[data-msg-id="${replyingTo.id}"]`);
                   if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "smooth" });
                 }}>
              <div style={{ color: "var(--accent)", fontWeight: 500 }}>
                replying to {replyingTo.user || "msg"}
              </div>
              <div style={{ whiteSpace: "nowrap", textOverflow: "ellipsis", overflow: "hidden",
                            color: "var(--fg-3)" }}>
                {replyingTo.snippet || "(empty)"}
              </div>
            </div>
            <button className="btn danger" style={{ fontSize: 10, padding: "2px 6px" }}
                    onClick={() => setReplyingTo(null)}>x</button>
          </div>
        )}
        {attachment && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 6,
                        background: "var(--bg-1)", border: "1px solid var(--hair)", borderRadius: 8 }}>
            <img src={attachment.dataUrl} alt="" style={{ height: 40, borderRadius: 2 }} />
            <span style={{ fontSize: 11, fontFamily: "var(--mono)", color: "var(--fg-2)", flex: 1 }}>
              {attachment.name || "image"}
            </span>
            <button className="btn danger" style={{ fontSize: 10, padding: "2px 6px" }}
                    onClick={() => setAttachment(null)}>x</button>
          </div>
        )}
        <div style={{ display: "flex", gap: 10, alignItems: "flex-end" }}>
        <input ref={fileInputRef} type="file" accept="image/*" onChange={onFile} style={{ display: "none" }} />
        <button className="btn ghost" title="attach image" onClick={pickImage}
                disabled={sending || noUser}
                style={{ height: 36, width: 36, padding: 0, boxSizing: "border-box", flexShrink: 0,
                         display: "inline-flex", alignItems: "center", justifyContent: "center",
                         lineHeight: 1, fontSize: 16 }}>
          +
        </button>
        <textarea
          ref={textareaRef}
          className="chat-input"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={onKey}
          onPaste={onPaste}
          placeholder={noUser ? "composer disabled: no chat user" : `message @${c.slug}...${dragOver ? " (drop image to attach)" : ""}`}
          disabled={sending || noUser}
          rows={1}
          style={{
            flex: 1, resize: "none",
            height: 36, maxHeight: 180,        /* one-line height matches button exactly */
            padding: "7px 12px",
            lineHeight: "20px",                /* 20 + 7 + 7 + 1 + 1 = 36px */
            fontFamily: "var(--sans)",
            fontSize: 14,
            background: "var(--bg-1)",
            color: "var(--fg-0)",
            border: "1px solid var(--line)",
            borderRadius: 10,
            outline: "none",
            boxSizing: "border-box",
            overflowY: "auto",
          }}
          onInput={e => {
            // Desktop: two discrete heights (36 single-line, 120 multi-line).
            // Mobile (<=820px): stay fixed at 36px so the viewport doesn't
            // jump around while typing; internal overflow scrolls instead.
            const el = e.target;
            if (window.innerWidth <= 820) {
              el.style.height = "36px";
              return;
            }
            el.style.height = "36px";
            const overflows = el.scrollHeight > 40;
            el.style.height = overflows ? "120px" : "36px";
          }}
        />
        <button
          className="btn primary"
          onClick={send}
          disabled={sending || noUser || (!draft.trim() && !attachment)}
          style={{ height: 36, padding: "0 14px", boxSizing: "border-box", flexShrink: 0 }}
        >{sending ? "sending" : "send"}</button>
        </div>
      </div>
    </React.Fragment>
  );
}

// `serial`: one input POST at a time, each awaited before the next (a
// tmux-kind runner's pane, whose gate reads the screen before every key).
function PaneView({ cousin, onClose, serial }) {
  const slug = cousin && cousin.slug;
  const serialRef = React.useRef(!!serial);
  serialRef.current = !!serial;
  const sendingRef = React.useRef(false);
  const tmuxSession = (cousin && cousin.tmuxSession) || "?";
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
  // Following: the reader is at the bottom, so every `pane` frame is
  // written as it lands. Scrolled up, the latest frame waits in
  // pendingRef (a rewrite would yank the viewport back to the bottom) and
  // is applied the moment the reader returns to the bottom.
  const followRef = React.useRef(true);
  const pendingRef = React.useRef(null);
  const applyingRef = React.useRef(false);
  // The pane state that came with the frame on screen (and with the held
  // one): whether the program tracks the mouse, the pane's rows, and how
  // many frame lines sit above the screen's first row.
  const paneStateRef = React.useRef(null);
  const pendingStateRef = React.useRef(null);
  const [held, setHeld] = React.useState(false);

  // Rewrite the terminal with one full frame and land on its last line.
  // Only refs are read, so the closure a stale render captured still
  // writes to the live terminal. reset() also clears the mouse mode and
  // the cursor state; the frame's own control tail sets them again
  // (mouse mode while the program tracks the mouse, the cursor on
  // tmux's cell, shown or hidden as tmux has it).
  const applyFrame = (text, state) => {
    const term = termRef.current;
    if (!term) return;
    if (text === lastTextRef.current) return;
    applyingRef.current = true;
    paneStateRef.current = state || null;
    // One write that clears and repaints, never reset() then write():
    // reset() blanks the screen at once and the write lands a render
    // later, so a busy pane (a spinner changes it every poll) flashed
    // blank twice a second. The prefix does what reset() did for a
    // frame - attributes, scroll region, mouse modes off, screen and
    // scrollback cleared, cursor home - and the frame's own control
    // tail sets the mouse mode and the cursor again.
    term.write(FRAME_PREFIX + text, () => {
      try { term.scrollToBottom(); } catch (_e) {}
      applyingRef.current = false;
    });
    lastTextRef.current = text;
  };

  // Boot an xterm.Terminal once per slug. `pane` frames reset and rewrite
  // the terminal; `delta` frames append, so the cursor stays at the tail
  // and scrollback survives.
  React.useEffect(() => {
    if (!slug || !hostRef.current || !window.Terminal) return;
    const term = new window.Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: "var(--mono), monospace",
      fontSize: 12,
      theme: { background: "#0a0a0a", foreground: "#dcdcdc" },
      scrollback: 4000,
      // Wheel events scroll N lines per tick instead of the default 1.
      // On mobile, momentum-scroll fires lots of tiny wheel events; without
      // this, the panel feels like it barely moves per gesture.
      scrollSensitivity: 5,
      fastScrollSensitivity: 8,
    });
    const fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fit) term.loadAddon(fit);
    term.open(hostRef.current);
    termRef.current = term;
    fitRef.current = fit;
    lastTextRef.current = "";

    // Tell tmux to resize its window to match the terminal.
    let resizeTimer = null;
    const pushResize = (cols, rows) => {
      fetch("/api/pane/resize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cousin: slug, cols, rows }),
      }).catch(() => {});
    };
    const debouncedResize = () => {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        try { fit && fit.fit(); } catch (_e) {}
        if (term.cols && term.rows) pushResize(term.cols, term.rows);
      }, 120);
    };
    // Initial fit + push (delay one frame so the host has its final size)
    requestAnimationFrame(() => debouncedResize());
    term.onResize(({ cols, rows }) => pushResize(cols, rows));

    // A mouse report names a cell of this terminal's viewport; the pane
    // wants its own screen row. Frame line k is buffer line k (a frame
    // is written into a reset terminal), and the pane's first row is
    // frame line `top`, so viewport row v is pane row viewportY + v - top.
    const toPaneRows = (data) => {
      const st = paneStateRef.current;
      if (!st || data.indexOf("\x1b[<") < 0) return data;
      const b = term.buffer.active;
      const maxRow = Math.max(1, st.rows || term.rows || 1);
      return data.replace(/\x1b\[<(\d+);(\d+);(\d+)([Mm])/g, (_m, btn, col, row, fin) => {
        const r = Math.max(1, Math.min(maxRow, b.viewportY + Number(row) - (st.top || 0)));
        return "\x1b[<" + btn + ";" + col + ";" + r + fin;
      });
    };

    // Input: buffer keystrokes for 40ms then POST as one blob. Reduces
    // subprocess calls when the user types fast.
    const sendInput = async () => {
      flushTimerRef.current = null;
      // serial: the send in flight flushes what was typed meanwhile
      if (serialRef.current && sendingRef.current) return;
      const buf = toPaneRows(inputBufRef.current);
      inputBufRef.current = "";
      if (!buf) return;
      sendingRef.current = true;
      try {
        const r = await fetch("/api/pane/input", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cousin: slug, data: buf }),
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
    term.onData(data => {
      inputBufRef.current += data;
      if (!flushTimerRef.current) {
        flushTimerRef.current = setTimeout(sendInput, 40);
      }
    });

    // Scrolling. A cousin session usually runs a full-screen program on
    // tmux's alternate screen: nothing to capture above it, so this
    // terminal has no scrollback to flick through and the gesture must
    // reach the program itself, as the program's own wheel reports. The
    // frame's tail puts this terminal in SGR mouse mode exactly while the
    // program tracks the mouse, so a desktop wheel becomes a report
    // (xterm.js emits them natively; the server forwards them only while
    // the program tracks the mouse). xterm.js generates no reports from
    // touch: vertical drag is translated into SGR wheel ticks (button 64
    // up / 65 down) at the touched cell. With tracking off (plain shell,
    // normal buffer) nothing is sent: xterm's viewport scrolls its own
    // scrollback, by wheel and by touch.
    const host = hostRef.current;
    let touchLastY = null;
    let touchAccPx = 0;
    const TOUCH_PX_PER_TICK = 24; // ~2 text rows per wheel tick
    const appTracksMouse = () => {
      // the frame's state first: between a frame's reset() and the end
      // of its write the terminal's own mode reads "none" for a moment
      const st = paneStateRef.current;
      if (st && st.mouse && st.sgr) return true;
      try {
        const m = term.modes && term.modes.mouseTrackingMode;
        return !!m && m !== "none";
      } catch (_e) { return false; }
    };
    const queueWheel = (up, touch) => {
      const rect = host.getBoundingClientRect();
      const cols = Math.max(1, term.cols || 1);
      const rows = Math.max(1, term.rows || 1);
      const col = Math.max(1, Math.min(cols,
        Math.ceil((touch.clientX - rect.left) / (rect.width / cols))));
      const row = Math.max(1, Math.min(rows,
        Math.ceil((touch.clientY - rect.top) / (rect.height / rows))));
      inputBufRef.current += "\x1b[<" + (up ? 64 : 65) + ";" + col + ";" + row + "M";
      if (!flushTimerRef.current) {
        flushTimerRef.current = setTimeout(sendInput, 40);
      }
    };
    const onTouchStart = (e) => {
      if (e.touches.length !== 1) { touchLastY = null; return; }
      touchLastY = e.touches[0].clientY;
      touchAccPx = 0;
    };
    const onTouchMove = (e) => {
      if (touchLastY == null || e.touches.length !== 1) return;
      const t = e.touches[0];
      const dy = t.clientY - touchLastY;
      touchLastY = t.clientY;
      if (!appTracksMouse()) return; // normal buffer: xterm scrolls itself
      e.preventDefault(); // we own the gesture; stop the page from panning
      touchAccPx += dy;
      // Finger down reveals earlier content = wheel-up, like native scroll.
      while (touchAccPx >= TOUCH_PX_PER_TICK) { queueWheel(true, t); touchAccPx -= TOUCH_PX_PER_TICK; }
      while (touchAccPx <= -TOUCH_PX_PER_TICK) { queueWheel(false, t); touchAccPx += TOUCH_PX_PER_TICK; }
    };
    const onTouchEnd = () => { touchLastY = null; touchAccPx = 0; };
    // A wheel must never reach the program as keys: on its alternate
    // buffer with mouse tracking off, xterm.js turns a wheel into arrow
    // keys, and an Escape-led sequence throws a modal input box out of
    // insert mode (the next injected line is then eaten as commands).
    // Frames never switch this terminal to the alternate buffer; if
    // anything does, the wheel is swallowed here before xterm sees it.
    const onWheelCapture = (e) => {
      if (appTracksMouse()) return;
      let alt = false;
      try { alt = term.buffer.active.type === "alternate"; } catch (_e) {}
      if (alt) { e.preventDefault(); e.stopPropagation(); }
    };
    host.addEventListener("wheel", onWheelCapture, { capture: true, passive: false });
    host.addEventListener("touchstart", onTouchStart, { passive: true });
    host.addEventListener("touchmove", onTouchMove, { passive: false });
    host.addEventListener("touchend", onTouchEnd, { passive: true });
    host.addEventListener("touchcancel", onTouchEnd, { passive: true });

    // Follow or hold, from where the viewport is. xterm moves its own
    // viewport on wheel, drag and keyboard scroll; both its onScroll and
    // the viewport element's scroll event report it, and a rewrite in
    // flight (applyingRef) is not the reader moving.
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

    // Refit whenever the host box changes, not only on a window resize:
    // the pane column slides open over a transition and the sidebar
    // collapses without a window event, and a fit taken mid-change
    // leaves rows hanging below the bottom edge.
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
      try { scrollSub.dispose(); } catch (_e) {}
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
  }, [slug]);

  // SSE stream -> xterm. Auto-reconnect on error with capped backoff so a
  // console restart, a backgrounded tab and transient network blips heal
  // without user action. The contract allows any mix of `pane` and `delta`
  // frames; both are handled.
  React.useEffect(() => {
    if (!slug) return;
    let es;
    let reconnectTimer;
    let attempt = 0;
    let cancelled = false;

    const open = () => {
      if (cancelled) return;
      try {
        es = new EventSource("/api/pane/stream?cousin=" + encodeURIComponent(slug) + "&lines=2000");
      es.addEventListener("pane", function (ev) {
        // Full frame: once on connect, after a geometry change, and on
        // every change when the server polls instead of tapping.
        try {
          const payload = JSON.parse(ev.data);
          const text = payload.text || "";
          if (followRef.current) {
            applyFrame(text, payload.state);
          } else {
            // the reader is scrolled up: keep their place, hold the frame
            pendingRef.current = text;
            pendingStateRef.current = payload.state || null;
          }
          setStatus("live");
          const ts = payload.ts ? new Date(payload.ts) : new Date();
          setLastPoll(ts);
          setLastChange(ts);
        } catch (_e) { /* ignore */ }
      });
      es.addEventListener("delta", function (ev) {
        // New bytes since the last event. xterm.js handles the ANSI
        // escape codes; we just append as-is.
        try {
          const payload = JSON.parse(ev.data);
          const text = payload.text || "";
          const term = termRef.current;
          if (term && text) term.write(text);
          setStatus("live");
          const ts = payload.ts ? new Date(payload.ts) : new Date();
          setLastPoll(ts);
          setLastChange(ts);
        } catch (_e) { /* ignore */ }
      });
      es.addEventListener("heartbeat", function (ev) {
        try {
          const payload = JSON.parse(ev.data);
          setLastPoll(payload.ts ? new Date(payload.ts) : new Date());
          setStatus("live");
        } catch (_e) { /* ignore */ }
      });
      es.addEventListener("geom", function (ev) {
        // The tmux pane resized (usually because another client attached
        // with a different window size). Match its columns so rendering
        // doesn't wrap at the wrong place, but keep the rows the host was
        // fitted to: taller tmux rows would hang below the window's
        // bottom edge, and the extra lines are in the scrollback anyway.
        try {
          const payload = JSON.parse(ev.data);
          const term = termRef.current;
          if (term && payload.cols && term.cols !== payload.cols) {
            term.resize(payload.cols, term.rows);
          }
        } catch (_e) { /* ignore */ }
      });
      es.onopen = function () { attempt = 0; setStatus("live"); };
      es.onerror = function () {
        if (cancelled) return;
        setStatus(attempt === 0 ? "reconnecting" : "reconnecting...");
        try { es.close(); } catch (_e) {}
        attempt += 1;
        // Exponential-ish backoff: 1s, 2s, 4s, 8s, capped at 15s.
        const wait = Math.min(15000, 1000 * Math.pow(2, Math.min(4, attempt - 1)));
        reconnectTimer = setTimeout(open, wait);
      };
      } catch (_e) {
        setStatus("error");
        reconnectTimer = setTimeout(open, 3000);
      }
    };
    open();
    return function () {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      try { if (es) es.close(); } catch (_e) {}
    };
  }, [slug]);

  // Tick for 'X s ago' labels
  const [, setNow] = React.useState(Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <React.Fragment>
      <div className="pane-hdr">
        <span className={"led " + (status === "live" ? "green" : status === "connecting" ? "amber" : "red")} />
        <span className="pane-title">Terminal</span>
        <span className="pane-chip">tmux {tmuxSession}</span>
        <span style={{ color: status === "live" ? "var(--green)" : status === "connecting" ? "var(--amber)" : "var(--red)" }}>{status}</span>
        <span style={{ color: "var(--fg-2)" }}>interactive</span>
        {lastPoll && <span>polled {fmtAgoShort((Date.now() - lastPoll.getTime()) / 1000)}</span>}
        {lastChange && <span>changed {fmtAgoShort((Date.now() - lastChange.getTime()) / 1000)}</span>}
        {sendError && <span style={{ color: "var(--red)" }}>input err: {sendError}</span>}
        <span style={{ flex: 1 }} />
        {held && (
          <button className="btn ghost"
                  onClick={() => { const t = termRef.current; if (t) t.scrollToBottom(); }}
                  title="scrolled back: live updates are held until you return to the bottom"
                  style={{ padding: "0 8px", minHeight: 22, color: "var(--amber)" }}
          >scrolled back &middot; jump to live</button>
        )}
        {onClose && <button className="btn ghost pane-x" onClick={onClose} title="collapse the terminal pane">x</button>}
      </div>
      {/* The padding lives on this wrapper, not on the xterm host:
          FitAddon counts rows from the host's computed height, which
          includes padding under box-sizing: border-box. */}
      <div style={{
        flex: 1, minHeight: 0,
        display: "flex",
        padding: "6px 10px",
        background: "#0a0a0a",
      }}>
        <div
          ref={hostRef}
          style={{
            flex: 1, minWidth: 0, minHeight: 0,
            overflow: "hidden",
          }}
        />
      </div>
    </React.Fragment>
  );
}

// The reasoning pane of a runner cousin (docs/reference/console-api.md,
// "The runner stream"): its event stream live, an interrupt and a say box.
// A runner cousin has no tmux session; its fleet row carries `runner`.
const RUNNER_PANE_KEEP = 500;

function runnerEventLine(ev) {
  const p = ev.payload || {};
  const cut = (s, n) => { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n - 3) + "..." : s; };
  switch (ev.kind) {
    case "state": return p.from + " -> " + p.to + (p.detail ? " (" + p.detail + ")" : "");
    case "turn_start": return (p.thread_id || "") + ": " + cut((p.bodies || [""])[0], 200);
    case "text": case "user": return String(p.text || "");
    case "thinking": return p.text ? p.text + (p.truncated ? " [truncated]" : "") : "(" + p.length + " chars, not recorded)";
    case "tool": return (p.name || "") + " " + cut(JSON.stringify(p.input), 300);
    case "tool_result": return (p.is_error ? "error: " : "") + cut(p.text, 600);
    case "tool_call": return (p.tool || "") + " " + (p.command || "") + (p.is_error ? " error" : "") + " (" + p.ms + " ms)";
    case "result": return "rows " + JSON.stringify(p.inbox_ids || []) + (p.interrupted ? " interrupted" : "") + (p.is_error ? " is_error" : "");
    case "error": return String(p.error || "");
    case "session": return String(p.session || "");
    default: return cut(JSON.stringify(p), 300);
  }
}

// Syntax highlighting for the runner pane. Every helper below is pure and
// returns a node tree, never HTML: a node is a string (text, which React
// escapes) or {tag, cls, children[, href]}. rpToReact is the only place a
// node becomes an element, through a fixed tag list, so model and tool
// output (untrusted) can never inject markup. Colors live in styles.css
// (`.rp-*`) on the theme variables.
const RP_TAGS = { span: 1, strong: 1, em: 1, code: 1, pre: 1, div: 1, a: 1 };
const rpSafeHref = (u) => /^https?:\/\//i.test(String(u || "")) ? String(u) : null;
const rpCut = (s, n) => { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n - 3) + "..." : s; };
const rpSpan = (cls, text) => ({ tag: "span", cls, children: [String(text)] });

// Which color group an event's body takes: the model's words ("text"), its
// thinking, tool executions (muted), errors, and the runner's own
// bookkeeping ("meta": state, turn, session, usage, ...).
function runnerKindClass(kind) {
  switch (kind) {
    case "text": case "user": case "result": return "text";
    case "thinking": return "thinking";
    case "tool": case "tool_call": case "tool_result": case "output": return "tool";
    case "error": case "auth": return "error";
    default: return "meta";
  }
}

// A unified diff, detected conservatively: at least two +/- lines AND a
// real header (an `@@ -a,b +c,d @@` hunk, a `--- `/`+++ ` pair, or
// `diff --git`), so a list of "- item" lines is never colored as one.
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

// A lexer, not a parser: it colors truncated JSON (an unterminated string
// at the cut) as well as whole JSON. A string followed by `:` is a key.
function highlightJson(text) {
  const s = String(text == null ? "" : text);
  const re = /("(?:[^"\\\n]|\\.)*"?)(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false)\b|\b(null)\b|([{}\[\],:])/g;
  const out = [];
  let last = 0, m;
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) out.push(s.slice(last, m.index));
    if (m[1] !== undefined) {
      out.push(rpSpan(m[2] !== undefined ? "rp-json-key" : "rp-json-str", m[1]));
      if (m[2] !== undefined) out.push(rpSpan("rp-json-punc", m[2]));
    } else if (m[3] !== undefined) out.push(rpSpan("rp-json-num", m[3]));
    else if (m[4] !== undefined) out.push(rpSpan("rp-json-bool", m[4]));
    else if (m[5] !== undefined) out.push(rpSpan("rp-json-null", m[5]));
    else out.push(rpSpan("rp-json-punc", m[6]));
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}

// A JSON value within the pane's existing budget (`limit` characters of
// its compact form): pretty-printed when it fits and its indentation does
// not blow it up (deep nesting), else the compact form cut, as the plain
// line always was.
function rpJsonBlock(value, limit) {
  const compact = JSON.stringify(value);
  if (compact === undefined) return [];
  if (compact.length <= limit) {
    const pretty = JSON.stringify(value, null, 2);
    if (pretty.length <= limit * 3) return highlightJson(pretty);
  }
  return highlightJson(rpCut(compact, limit));
}

// Inline markdown: `code`, **bold**, *italic*, [label](http(s) url). A
// link with any other scheme stays literal text. Every alternative's
// repeat is bounded: the text is untrusted, and an unbounded group that
// can fail late (a line of unclosed `[`) is quadratic in the line length.
function mdInline(text) {
  const s = String(text == null ? "" : text);
  const re = /`([^`\n]{1,2000})`|\*\*([^*\n]{1,2000}?)\*\*|\*([^*\s](?:[^*\n]{0,2000}?[^*\s])?)\*|\[([^\]\n]{1,300})\]\(([^)\s]{1,500})\)/g;
  const out = [];
  let last = 0, m;
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) out.push(s.slice(last, m.index));
    if (m[1] !== undefined) out.push({ tag: "code", cls: "rp-md-code", children: [m[1]] });
    else if (m[2] !== undefined) out.push({ tag: "strong", children: [m[2]] });
    else if (m[3] !== undefined) out.push({ tag: "em", children: [m[3]] });
    else if (rpSafeHref(m[5])) out.push({ tag: "a", cls: "rp-md-a", href: m[5], children: [m[4]] });
    else out.push(m[0]);
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}

// Block markdown, one node per line: fenced code (```diff and a detected
// diff get diff colors, ```json the JSON colors), headings, list items,
// quotes, rules, paragraphs. Only the first RP_MD_MAX characters are
// parsed; the rest follows as plain text, under a marker (.rp-md-rest in
// styles.css: a thin rule and "raw text from here").
const RP_MD_MAX = 20000;
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
      const kids = (lang === "diff" || looksLikeDiff(code)) ? renderDiff(code)
        : lang === "json" ? highlightJson(code) : [code];
      out.push({ tag: "pre", cls: "rp-md-pre", children: kids });
      continue;
    }
    let m;
    if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
      out.push({ tag: "div", cls: "rp-md-h rp-md-h" + m[1].length, children: mdInline(m[2]) });
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push({ tag: "div", cls: "rp-md-hr", children: [] });
    } else if ((m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line))) {
      out.push({ tag: "div", cls: "rp-md-li", children: [m[1], rpSpan("rp-md-bullet", m[2]), " "].concat(mdInline(m[3])) });
    } else if ((m = /^\s*>\s?(.*)$/.exec(line))) {
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

// A tool's input: an edit (old_string/new_string) as a diff of the file,
// anything else as JSON.
function rpToolInput(input) {
  if (input && typeof input.old_string === "string" && typeof input.new_string === "string") {
    const f = String(input.file_path || "");
    const diff = ["--- " + f, "+++ " + f]
      .concat(input.old_string.split("\n").map(l => "-" + l))
      .concat(input.new_string.split("\n").map(l => "+" + l)).join("\n");
    return ["\n"].concat(renderDiff(rpCut(diff, 600)));
  }
  return rpJsonBlock(input, 300);
}

// A tool's output: JSON when it parses as JSON, a diff when it is one,
// plain text otherwise; within runnerEventLine's 600-character budget.
// Above RP_JSON_PARSE_MAX the JSON path is skipped (it would parse the
// whole output to show 600 characters of it).
const RP_JSON_PARSE_MAX = 65536;
function rpToolOutput(text) {
  const s = String(text == null ? "" : text);
  const t = s.length <= RP_JSON_PARSE_MAX ? s.trim() : "";
  if (/^[\[{]/.test(t)) {
    try { return rpJsonBlock(JSON.parse(t), 600); } catch (e) { /* not JSON */ }
  }
  if (looksLikeDiff(s)) return renderDiff(rpCut(s, 600));
  return [rpCut(s, 600)];
}

// The body of one pane line as a node tree; the text is runnerEventLine's
// where nothing is highlighted.
function runnerEventBody(ev) {
  const p = ev.payload || {};
  switch (ev.kind) {
    case "text": case "user": return renderMarkdownLite(p.text);
    case "tool": return [rpSpan("rp-tool-name", p.name || ""), " "].concat(rpToolInput(p.input));
    case "tool_result": case "output":
      return (p.is_error ? [rpSpan("rp-err", "error: ")] : []).concat(rpToolOutput(p.text));
    case "tool_call":
      return [rpSpan("rp-tool-name", p.tool || ""), " " + (p.command || "")]
        .concat(p.is_error ? [rpSpan("rp-err", " error")] : [])
        .concat([rpSpan("rp-dim", " (" + p.ms + " ms)")]);
    case "state": case "turn_start": case "thinking": case "result": case "error": case "session":
      return [runnerEventLine(ev)];
    default: return highlightJson(rpCut(JSON.stringify(p), 300));
  }
}

// A pane row's rendered body, once per event: the pane re-renders all its
// rows (up to RUNNER_PANE_KEEP) on every frame that brings new events, and
// an event never changes after it arrives.
const RP_BODY_CACHE = new WeakMap();
function rpRowBody(ev) {
  let body = RP_BODY_CACHE.get(ev);
  if (!body) {
    body = runnerEventBody(ev).map((n, j) => rpToReact(n, j));
    RP_BODY_CACHE.set(ev, body);
  }
  return body;
}

// The one place a node becomes a React element: a tag outside RP_TAGS
// renders as a span, an href is re-checked, text stays text.
function rpToReact(node, key) {
  if (node == null) return null;
  if (typeof node !== "object") return String(node);
  const tag = RP_TAGS[node.tag] ? node.tag : "span";
  const props = { key, className: node.cls || undefined };
  if (tag === "a") {
    const href = rpSafeHref(node.href);
    if (href) { props.href = href; props.target = "_blank"; props.rel = "noopener noreferrer"; }
  }
  return React.createElement(tag, props, ...(node.children || []).map((c, i) => rpToReact(c, i)));
}

// The pane's liveness/state follow the stream it already reads, with the
// fleet row (the 15s `cousins-refresh` poll) as the fallback before any
// stream evidence: a `session` frame (the runner started or restarted)
// marks it alive with a neutral "starting" state (never the previous
// runner's leftover state, e.g. "stopped" surviving as if it were live); a
// `state` event marks it alive and records the state; a `state` event to
// the runner's one terminal state ("stopped", cousin_lib/runner/state.py)
// marks it not alive. A `fleet` event is dispatched only when the fleet
// row's own runner data actually changed (never on an unrelated patch,
// e.g. app.jsx's `cousin-status` handler, which replaces the cousin object
// but not its `runner`), so whichever event this function sees LAST is by
// construction the freshest evidence - a fleet update landing after the
// last stream evidence wins, exactly because it is newer, and it adopts
// BOTH alive and state from the fleet row together, never just alive (a
// mismatched pair, e.g. alive:true with a leftover state:"stopped", is
// never produced).
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
      return fleetRunner
        ? { alive: fleetRunner.alive !== false, state: fleetRunner.state != null ? fleetRunner.state : null }
        : p;
    default:
      return p;
  }
}

// The fleet row's own data, as a value (not object-reference) key: two
// runner snapshots with the same alive/state/session/since are the same
// evidence, even as two distinct objects (a fresh, otherwise-unchanged
// cousins-refresh; or app.jsx's `cousin-status` SSE handler, which spreads
// a brand new cousin row on the console's own start/stop actions WITHOUT
// touching `c.runner`). Keying the fleet effect on this, not on `cousin`
// itself, is what keeps an unrelated status patch from re-dispatching a
// stale runner snapshot as "fresh" fleet evidence over live stream events.
function runnerFleetKey(runner) {
  const r = runner || {};
  return JSON.stringify([r.alive, r.state, r.session, r.since]);
}

// === The folded view of the stream ===
// The raw stream is one row per event; most of it is bookkeeping. rpModel
// folds it into a status strip (what the runner is doing now, the quota,
// the session, the totals) and a log of real things: a turn, a tool with
// its result, a reply, the model's text, a turn's summary line. Pure: the
// pane re-folds its kept events on every batch. The raw toggle keeps the
// one-row-per-event view.

const rpIsTick = (ev) => !!ev && ev.kind === "system" && !!ev.payload && ev.payload.subtype === "thinking_tokens";

// The kept events, with a run of thinking ticks collapsed into its first
// (n counts them, last_ts is the newest): a thinking model sends one per
// chunk, and they would push real events out of RUNNER_PANE_KEEP.
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

// The other system subtypes a lane or the CLI sends, as a line (cls,
// text), or null for none. A subtype with no payload of its own and no
// entry here says nothing worth a row (the CLI's status, informational,
// code_change_published, commands_changed notices).
const RP_SYSTEM_LINES = {
  compact_boundary: p => ["rp-roll", "context compacted" + (p.trigger ? " · " + p.trigger : "")],
  reconnected: p => ["rp-warn", "reconnected" + (p.why ? " · " + rpCut(p.why, 140) : "")],
  restart_note: p => ["rp-roll", "restart" + (p.why ? " · " + rpCut(p.why, 140) : "")],
  retry: p => ["rp-warn", "retry " + (p.attempt || 1) + (p.message ? " · " + rpCut(p.message, 140) : "")],
  stall: p => ["rp-warn", "stall · " + (p.site || "?") + (p.seconds != null ? " " + Math.round(p.seconds) + " s" : "") + (p.ongoing ? ", ongoing" : "")],
  permission_denied: p => ["rp-warn", "permission denied" + (p.tool_name ? " · " + p.tool_name : "")],
  resume_failed: p => ["rp-warn", "resume failed" + (p.error ? " · " + rpCut(p.error, 140) : "")],
  connect_failed: p => ["rp-err", "connect failed" + (p.error ? " · " + rpCut(p.error, 140) : "")],
  side_restarted: p => ["rp-warn", "side session restarted · " + (p.session || "?") + (p.attempt ? ", attempt " + p.attempt : "")],
  interrupt_dropped: p => ["rp-dim", "interrupt dropped · its turn had ended"],
  drained: p => ["rp-dim", "drained · " + (Array.isArray(p.messages) ? p.messages.length : (p.messages || 0)) + " messages"],
  resync_skipped: p => ["rp-dim", "resync skipped" + (p.why ? " · " + p.why : "")],
  opencode_leftover: p => ["rp-roll", "leftover opencode killed · " + (p.pids || []).length + " process" + ((p.pids || []).length === 1 ? "" : "es")],
  pressure_off: p => ["rp-dim", "context pressure off" + (p.why ? " · " + rpCut(p.why, 140) : "")],
  event_gap: p => ["rp-dim", "event stream gap" + (p.settled ? " · settled" : "")],
  adopt_refused: p => ["rp-warn", "pane not adopted" + (p.reason || p.detail || p.why ? " · " + rpCut(p.reason || p.detail || p.why, 140) : "")],
  hooks_silent: p => ["rp-warn", "hooks silent" + (p.detail ? " · " + rpCut(p.detail, 140) : "")],
  notice_not_typed: p => ["rp-warn", "notice not typed" + (p.waited_s != null ? " after " + p.waited_s + " s" : "")],
  pane_failing: p => ["rp-err", "pane failing" + (p.reason ? " · " + rpCut(p.reason, 140) : "")],
  pane_lost: p => ["rp-err", "pane lost" + (p.requeued ? " · " + p.requeued + " requeued" : "")],
  pane_reopened: p => ["rp-roll", "pane reopened" + (p.source ? " · " + p.source : "")],
  pane_unreachable: p => ["rp-err", "pane unreachable" + (p.detail ? " · " + rpCut(p.detail, 140) : "")],
  session_changed: p => ["rp-dim", "new session · " + rpCut(p.new_session_id || "", 60)],
  typing_blocked: p => ["rp-warn", "typing blocked · " + (p.what || "?") + (p.screen ? " (" + p.screen + ")" : "")],
};
const RP_SYSTEM_BOOT = { session: 1 };
// The framework's own kinds that are not turn content, as a line.
const RP_KIND_LINES = {
  memory_update: p => ["rp-dim", "memory updated · " + (p.entries || 0) + " entr" + (p.entries === 1 ? "y" : "ies")],
  config_change: p => ["rp-dim", "config changed · " + (p.files || []).join(", ")],
  hook: p => [p.error ? "rp-warn" : "rp-dim", "hook " + (p.event || "") + (p.error ? " · " + rpCut(p.error, 140) : "")],
  cap: p => ["rp-warn", "cost cap · $" + Number(p.spent || 0).toFixed(2) + " of $" + Number(p.limit || 0).toFixed(2) + " today"
                        + (p.refused ? " · turn refused" : p.allowed === "person" ? " · only a person's chat runs" : "")],
  duplicate_delivery: p => ["rp-dim", "duplicate delivery ignored"],
  foreign_turn: p => ["rp-dim", "a turn typed in the pane"],
  other: p => ["rp-dim", "SDK message · " + (p.type || "?")],
  permission: p => ["rp-warn", p.tool_name ? "permission asked · " + p.tool_name
                                            : "notification · " + rpCut(p.message || p.event || "", 140)],
};

// A gate that sent a turn back (hooks.gate_events; the opencode lane's
// repeat nudge): what it says in the pane.
function rpGateLine(p) {
  const threads = (p.threads || []).join(", ");
  if (p.gate === "reply") return { cls: "rp-warn", text: "reply gate · sent back once, no reply to " + (threads || "the thread")
                                                    + (p.written_call ? " · reply written as text" : "") };
  if (p.gate === "send") return { cls: "rp-warn", text: "send gate · sent back once, no send to " + (threads || "the peer") };
  if (p.gate === "repeat") return { cls: "rp-warn", text: "repeat gate · " + (p.tool || "a tool") + " called " + (p.count || "?") + " times in a row"
                                                     + (p.action === "end" ? " · turn ended" : p.action ? " · " + p.action : "") };
  return { cls: "rp-warn", text: "gate · " + String(p.gate || "?") + (threads ? " · " + threads : "") };
}
const RP_TASK_SUBTYPES = { task_started: 1, task_progress: 1, task_updated: 1, task_notification: 1 };
// the SDK's TERMINAL_TASK_STATUSES: task_notification says "stopped",
// task_updated the raw "killed"
const RP_TASK_ENDS = { completed: 1, failed: 1, stopped: 1, killed: 1 };

// A background task's lifecycle event onto the strip's task map:
// task_started adds it, task_progress names its last tool, a terminal
// task_updated status or a task_notification ends it (the SDK sends
// either, not always both). An update or an end for a task not in view is
// ignored. An event with no task_id (a stream recorded before 1.27, the
// subtype alone) returns its change to the old count instead.
function rpTaskEvent(tasks, p, ts) {
  const id = p.task_id;
  if (!id) return p.subtype === "task_started" ? 1 : p.subtype === "task_notification" ? -1 : 0;
  if (p.subtype === "task_started") {
    tasks[id] = { id, desc: p.description || "", type: p.task_type || null, started: ts,
                  tool: null, status: "running", ended: null, summary: null };
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

// The list the chip opens: the running tasks, oldest first, then the last
// `keep` ended ones, newest first.
function rpTaskList(tasks, keep) {
  const all = Object.keys(tasks || {}).map(k => tasks[k]);
  return {
    running: all.filter(t => t.ended == null).sort((a, b) => (a.started || 0) - (b.started || 0)),
    ended: all.filter(t => t.ended != null).sort((a, b) => b.ended - a.ended).slice(0, keep == null ? 5 : keep),
  };
}

// "agent" or "shell" for the SDK's task types, else the type as sent.
function rpTaskType(type) {
  const t = String(type || "");
  if (/bash|shell/.test(t)) return "shell";
  if (/agent/.test(t)) return "agent";
  return t.replace(/_/g, " ") || "task";
}

function rpModel(events) {
  const strip = { state: null, activity: null, rate: {}, session: null, tokens: 0, cost: 0, turns: 0,
                  bg: 0, bgUntracked: 0, tasks: {}, auth: null };
  const rows = [];
  const tools = {};
  // the runner emits a message's recall BEFORE its user event: a folded
  // message's recall waits here for its own divider
  let recalls = [];
  let turn = null, meta = {}, thinkSince = null;
  // the gate row whose send-back message (a user event) comes next
  let gateRow = null;
  const newMeta = () => { meta = {}; return meta; };
  (events || []).forEach((ev, i) => {
    const p = ev.payload || {};
    const k = ev.kind, ts = ev.ts || null;
    // seq restarts with a new runner session; ts keeps two sessions apart
    const key = (ts != null ? ts : "") + ":" + (ev.seq != null ? ev.seq : "i" + i) + ":" + k;
    if (rpIsTick(ev)) {
      if (thinkSince == null) thinkSince = ts;
      strip.activity = { kind: "thinking", since: thinkSince };
      return;
    }
    const thought = thinkSince;
    thinkSince = null;
    // A gate's send-back can trail the turn's last words (text, thinking)
    // before it arrives: the gate stays open through those, and closes at
    // anything else.
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
      // background tasks are a list behind a chip on the strip, not rows:
      // the SDK reports one around many ordinary tool calls
      if (RP_TASK_SUBTYPES[p.subtype]) strip.bgUntracked = Math.max(0, strip.bgUntracked + rpTaskEvent(strip.tasks, p, ts));
      else if (p.subtype === "fresh" || p.subtype === "init" || p.subtype === "resumed") rpBoot(rows, key, ev);
      // the opencode lane's boot report: in the boot group when it holds
      else if ((p.subtype === "mcp" && p.status === "connected")
               || (p.subtype === "policy_plugin" && !(p.errors || []).length)) rpBoot(rows, key, ev);
      // no producer sends a failed one today (the runner raises instead):
      // a guard, so a lane that does is a line, not raw JSON
      else if (p.subtype === "mcp")
        rows.push({ t: "line", boot: true, key, ev, cls: "rp-err", text: "mcp · " + String(p.status || "not connected") + (p.error ? " · " + rpCut(p.error, 120) : "") });
      else if (p.subtype === "policy_plugin")
        rows.push({ t: "line", boot: true, key, ev, cls: "rp-err", text: "policy plugin · " + rpCut((p.errors || []).map(e =>
          e && typeof e === "object" ? (e.source != null ? e.source + ": " : "") + String(e.error || "") : String(e)).join("; "), 160) });
      else if (p.subtype === "perimeter")
        rows.push({ t: "line", boot: true, key, ev, cls: p.level === "warning" ? "rp-warn" : "rp-dim", text: "perimeter · " + rpCut(p.line || "", 160), detail: p.line || null });
      // a CLI turn of its own between turns (a task notification)
      else if (p.subtype === "background_turn" && p.phase === "start") {
        turn = { t: "turn", key, ev, thread: "background", bodies: [], user: null, recall: null, meta: newMeta() };
        recalls = [];
        rows.push(turn);
      }
      else if (p.subtype === "background_turn" || p.subtype === "background_end")
        rows.push({ t: "line", key, ev, cls: "rp-dim",
                    text: p.subtype === "background_end" ? "the CLI's stream ended between turns" : "background turn handed to the next turn" });
      else if (p.subtype === "api_retry") {
        const auth = p.error_status === 401 || p.error_status === 403;
        rows.push({ t: "line", key, ev, cls: auth ? "rp-err" : "rp-warn",
                    text: "API retry " + (p.attempt || 1) + ": " + [p.error_status, p.error].filter(x => x != null && x !== "").join(" ") });
      }
      // the CLI's own notice around a hook's feedback: says nothing alone
      else if (p.subtype === "notification") {
        if (p.message) rows.push({ t: "line", key, ev, cls: "rp-dim", text: "notification · " + rpCut(String(p.message), 160) });
      }
      // the opencode lane names its session at boot and again when it
      // opens a new one mid-stream: the boot group's only at boot
      else if (RP_SYSTEM_BOOT[p.subtype] && rpBootGroup(rows)) rpBoot(rows, key, ev);
      else if (RP_SYSTEM_BOOT[p.subtype])
        rows.push({ t: "line", key, ev, cls: "rp-dim", text: "new session · " + rpCut(p.session_id || "", 60) });
      else if (RP_SYSTEM_LINES[p.subtype]) {
        const [cls, text] = RP_SYSTEM_LINES[p.subtype](p);
        rows.push({ t: "line", key, ev, cls, text });
      }
      else if (!RP_SKIP_SYSTEM[p.subtype] && Object.keys(p).some(f => f !== "subtype")) rows.push({ t: "meta", key, ev });
      return;
    }
    if (k === "runner") {
      // a new runner is a new CLI: the old one's tasks went with it
      Object.keys(strip.tasks).forEach(id => {
        const t = strip.tasks[id];
        if (t.ended == null) { t.ended = ts; t.status = "lost"; t.summary = "the runner restarted"; }
      });
    }
    // the runner's SDK and CLI version check: part of the boot when it
    // holds, a line of its own when it does not
    if (k === "harness" && !p.ok) {
      rows.push({ t: "line", boot: true, key, ev, cls: p.level === "error" ? "rp-err" : "rp-warn",
                  text: "harness · " + String(p.message || (p.problems || []).join("; ") || "version check failed") });
      return;
    }
    // a policy decision on one call (hooks.on_policy) is turn content;
    // only the policy the runner started on (`describe`) is boot
    if (k === "policy" && !("describe" in p)) {
      rows.push({ t: "line", key, ev, cls: p.decision === "deny" ? "rp-err" : "rp-warn",
                  text: "policy · " + (p.decision || "?") + " " + (p.tool || "a tool")
                        + (p.agent_id ? " (subagent)" : "") + (p.reason ? " · " + rpCut(p.reason, 140) : "") });
      return;
    }
    if (RP_BOOT_KINDS[k] || k === "harness") { rpBoot(rows, key, ev); return; }
    if (RP_KIND_LINES[k]) {
      const [cls, text] = RP_KIND_LINES[k](p);
      rows.push({ t: "line", key, ev, cls, text });
      return;
    }
    switch (k) {
      case "rate_limit": strip.rate[p.type || "limit"] = p; return;
      case "session_init":
        strip.session = { model: p.model || null, auth: p.apiKeySource == null ? null : (p.apiKeySource === "none" ? "your login" : "API key (" + p.apiKeySource + ")") };
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
        // a gate's send-back is the gate's text, not a message: onto its row
        if (lastGate) { lastGate.detail = (lastGate.detail ? lastGate.detail + "\n" : "") + String(p.text || ""); return; }
        if (turn && turn.user == null) { turn.user = String(p.text || ""); return; }
        // a message folded into the running turn: its own divider
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
        rows.push({ t: "line", key, ev, cls: "rp-roll" + (p.phase === "failed" ? " rp-err" : ""),
                    text: "session rollover · " + (p.phase || "") + (p.reason ? " · " + String(p.reason).replace(/_/g, " ") : "") });
        return;
      case "review_gate":
        rows.push({ t: "line", key, ev, cls: p.error ? "rp-warn" : "rp-dim",
                    text: "memory review · " + (p.kept || 0) + " kept, " + (p.dropped || 0) + " dropped"
                          + (p.pending ? ", " + p.pending + " pending" : "") + (p.error ? " · " + p.error : "") });
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
        if (row && !row.result) { row.result = ev; strip.activity = strip.activity && { kind: "working", since: ts }; return; }
        break;
      }
      case "tool_call": {
        // the framework's own timing of one of its tools: onto the card
        for (let j = rows.length - 1; j >= 0; j--) {
          const r = rows[j];
          if ((r.t === "tool" || r.t === "reply") && r.ms == null) {
            const n = String((r.ev.payload || {}).name || "");
            if (n === p.tool || n.endsWith("__" + p.tool)) { r.ms = p.ms; return; }
          }
        }
        // no card to time (e.g. a call outside a turn): a failure still shows
        if (p.is_error) rows.push({ t: "line", key, ev, cls: "rp-err", text: "tool " + (p.tool || "?") + " failed" + (p.command ? " · " + rpCut(p.command, 90) : "") });
        return;
      }
      case "thinking":
        if (p.text || thought != null) rows.push({ t: "thinking", key, ev, secs: thought != null && ts ? Math.max(0, ts - thought) : null });
        if (strip.activity) strip.activity = { kind: "working", since: ts };
        return;
      case "text": rows.push({ t: "text", key, ev }); return;
      case "error": rows.push({ t: "error", key, ev }); return;
      case "checkpoint": meta.checkpoint = p; return;
      case "extract": meta.extract = p; return;
      case "propose": meta.propose = p; return;
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

// The boot group a boot event joins: the last row, or the group just
// above the boot-phase lines under it (a perimeter warning, a failed
// check), so one start is one group however its warnings interleave.
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

// An `auth` event as one line (runner/sdk.py, opencode.py, tmux_runner.py):
// a login required or a credential mismatch blocks the cousin, a retry is
// a warning, a restore clears it.
function rpAuthLine(p) {
  if (p.restored) return { cls: "rp-ok", text: "login restored" + (p.account ? " · " + p.account : ""), blocking: false };
  if (p.retry) return { cls: "rp-warn", text: "login retry · " + p.retry, blocking: false };
  if (p.mismatch) return { cls: "rp-err", text: "credentials mismatch · expected " + p.expected + ", got " + p.got, blocking: true };
  const why = [p.detail || p.reason || (p.login_required ? "login required" : "auth"), p.action].filter(Boolean).join(" · ");
  return { cls: "rp-err", text: "login required · " + why, blocking: true };
}

// A tool's short name and a one-line description of what it was asked.
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
  if (sec < 1) return Math.round(sec * 1000) + "ms";
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
// One quota window as the strip shows it: a label, a 0-100 fill, a level
// (ok, warn, over) and when it resets, on this browser's clock.
function rpRate(type, p) {
  const label = type === "seven_day" ? "7-day" : type === "five_hour" ? "5-hour" : String(type).replace(/_/g, " ");
  const u = typeof p.utilization === "number" ? p.utilization : null;
  const pct = u == null ? null : Math.max(0, Math.min(100, Math.round(u * 100)));
  const level = p.status === "rejected" || p.overage ? "over" : (p.status === "allowed_warning" || (pct != null && pct >= 80)) ? "warn" : "ok";
  let resets = null;
  if (p.resets_at) {
    const d = new Date(p.resets_at * 1000);
    resets = RP_DAYS[d.getDay()] + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  return { label, pct, level, resets };
}

// A turn's summary line from its result, usage, extract, propose and checkpoint.
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

// A turn's divider: the envelope's first line and the start of the message.
function rpTurnHead(turn) {
  const src = turn.user != null ? turn.user : String((turn.bodies || [])[0] || "");
  const lines = src.split("\n");
  let head = turn.thread || "", rest = src;
  let title = head;
  if (/^\[[^\]]+\]/.test(lines[0] || "")) {
    title = lines[0];
    rest = lines.slice(1).join("\n");
    // "[operator:x] chat from x at 2026-09-25 09:48 UTC" reads as "x 09:48"
    const m = /^\[[^\]]+\]\s+\S+\s+from\s+(.+?)\s+at\s+\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})/.exec(title);
    head = m ? m[1] + " " + m[2] : title;
  }
  return { head: rpCut(head, 90), title, snippet: rpCut(rest.replace(/\s+/g, " ").trim(), 110), full: src };
}

function RunnerPaneView({ cousin, onClose, chatUser, chatHidden }) {
  const slug = cousin && cousin.slug;
  const runner = (cousin && cousin.runner) || {};
  const [events, setEvents] = React.useState([]);
  const [status, setStatus] = React.useState("connecting");
  // Liveness/state follow the stream (paneLiveness), with the fleet row as
  // the fallback before any stream evidence and the tiebreaker whenever it
  // refreshes after the last stream evidence (the fleet row's
  // own 15s poll otherwise left the header and the interrupt button lagging
  // a runner restart or stop by up to 15s).
  const [live, setLive] = React.useState(() => paneLiveness(null, { kind: "fleet" }, runner));
  const [said, setSaid] = React.useState("");
  const [note, setNote] = React.useState(null);
  const listRef = React.useRef(null);
  const runnerRef = React.useRef(runner);
  runnerRef.current = runner;
  // A stable signature of the fields paneLiveness reads off the fleet row.
  // Keyed on this, not on `cousin` itself: app.jsx's `cousin-status`
  // handler (around app.jsx:454, driven by routes_fleet.py's `cousin-status`
  // SSE events on the console's own start/stop actions) replaces the whole
  // cousin object on every status patch WITHOUT touching `c.runner`, so
  // keying on `cousin` re-dispatched that unchanged (and possibly stale)
  // runner snapshot as "fresh" fleet evidence, able to land after and
  // override a live stream event with the same old bug, just shorter.
  const runnerKey = runnerFleetKey(runner);

  // The fleet row's own data only actually changes when the cousins-refresh
  // poll lands (or the initial snapshot), not on every render and not on an
  // unrelated cousin-status patch, so this fires once per real refresh: a
  // fresh, authoritative snapshot that outranks stale stream state.
  React.useEffect(() => {
    setLive(prev => paneLiveness(prev, { kind: "fleet" }, runnerRef.current));
  }, [runnerKey]);

  React.useEffect(() => {
    if (!slug) return undefined;
    setEvents([]);
    // A burst of events (a fresh connect sends the newest 200) is ONE render
    // per animation frame, not one per SSE message.
    let batch = [], frame = null;
    const flush = () => {
      frame = null;
      const add = batch;
      batch = [];
      setEvents(prev => rpCompact(prev, add).slice(-RUNNER_PANE_KEEP));
    };
    const push = (ev) => {
      batch.push(ev);
      if (frame === null) frame = window.requestAnimationFrame(flush);
    };
    // The server starts a fresh connect at the stream's tail; an automatic
    // reconnect resends the last `<session>:<seq>` id and resumes after it.
    const es = new EventSource(`/api/cousins/${encodeURIComponent(slug)}/stream`);
    es.onopen = () => setStatus("live");
    es.onerror = () => setStatus("reconnecting");
    es.addEventListener("runner-event", (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch (e) { return; }
      if (ev.kind === "state" && ev.payload) setLive(prev => paneLiveness(prev, ev, runnerRef.current));
      push(ev);
    });
    es.addEventListener("session", (m) => {
      let payload = {};
      try { payload = JSON.parse(m.data) || {}; } catch (e) {}
      setLive(prev => paneLiveness(prev, { kind: "session" }, runnerRef.current));
      push({ kind: "session", payload });
    });
    return () => { es.close(); if (frame !== null) window.cancelAnimationFrame(frame); };
  }, [slug]);

  // Follow the stream only while the reader is at the bottom: scrolled up,
  // a new event leaves the view where it is and offers a jump back.
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
    try { return localStorage.getItem("fw_rp_raw") === "1"; } catch (e) { return false; }
  });
  React.useEffect(() => {
    try { localStorage.setItem("fw_rp_raw", raw ? "1" : "0"); } catch (e) { /* ignore */ }
  }, [raw]);
  const model = React.useMemo(() => rpModel(events), [events]);
  // The strip's elapsed timer ticks only while the runner is doing something.
  const [now, setNow] = React.useState(() => Date.now());
  const ticking = !!model.strip.activity;
  React.useEffect(() => {
    if (!ticking) return undefined;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
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
        body: JSON.stringify(body || {}),
      });
      const d = await r.json();
      setNote(r.ok ? d.outcome : (d.error || `HTTP ${r.status}`));
      return r.ok;
    } catch (e) {
      setNote(String(e.message || e));
      return false;
    }
  };
  const interrupt = () => post(`/api/cousins/${encodeURIComponent(slug)}/interrupt`);
  // While the chat is collapsed the say box can post a normal,
  // stored chat message instead, the composer's own /api/chat/send; it
  // only injects into the running turn otherwise.
  const [asChat, setAsChat] = React.useState(false);
  const sendAsChat = asChat && !!chatHidden;
  const say = async () => {
    const text = said.trim();
    if (!text) return;
    if (sendAsChat) {
      if (!chatUser) { setNote("no chat user: open the chat to see why"); return; }
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
  return (
    <React.Fragment>
      <div className="pane-hdr">
        <span className={"led " + (!live.alive ? "gray" : live.state === "waiting_permission" ? "amber" : "green") + (turnLive && live.state === "running" ? " pulse" : "")} />
        <span className="pane-title">Reasoning</span>
        <span className="pane-chip">runner {runner.kind || "?"}</span>
        <span style={{ color: status === "live" ? "var(--green)" : "var(--amber)" }}>{status}</span>
        <span style={{ color: "var(--fg-1)" }}>{!live.alive ? "not running (last: " + (live.state || "none") + ")" : (live.state || "no state yet").replace(/_/g, " ")}</span>
        {unsupported.length > 0 && <span>unsupported: {unsupported.join(", ")}</span>}
        <span style={{ flex: 1 }} />
        {onClose && <button className="btn ghost pane-x" onClick={onClose} title="collapse the pane">x</button>}
      </div>
      <RpStrip strip={model.strip} now={now} raw={raw} setRaw={setRaw} />
      <div ref={listRef} className={"rp-list" + (raw ? "" : " rp-folded")} onScroll={onListScroll}>
        {raw ? events.map((ev, i) => (
          <div key={i} className={"runner-ev runner-ev-" + ev.kind + " rp-" + runnerKindClass(ev.kind)}>
            <span className="rp-kind">{ev.kind}</span>
            <div className="rp-body">{rpRowBody(ev)}</div>
          </div>
        )) : model.rows.map(row => (
          <RpRow key={row.key} row={row} now={now} slug={slug}
                 activeTool={!!model.strip.activity} />
        ))}
        {events.length === 0 && <div className="rp-empty">{status === "live" ? "no events yet: the stream shows the runner's turns as they happen" : "connecting to the stream..."}</div>}
      </div>
      {behind && <button className="rp-jump" onClick={toBottom}>new events ↓</button>}
      <div className="pane-foot">
        {note && <div className="pane-note">{note}</div>}
        <div className="pane-foot-row">
          {chatHidden && (
            <label className="rp-as-chat" title="post a stored chat message, as the chat's composer does, instead of saying it to the running turn">
              <input type="checkbox" checked={asChat} onChange={e => setAsChat(e.target.checked)} />
              <span>send as chat</span>
            </label>
          )}
          <input className="txt" value={said} onChange={e => setSaid(e.target.value)}
                 onKeyDown={e => { if (e.key === "Enter") say(); }}
                 placeholder={sendAsChat ? `message @${slug}` : "say to the running turn"} style={{ flex: 1 }} />
          <button className="btn" onClick={say} disabled={!said.trim()}>{sendAsChat ? "send" : "say"}</button>
          <button className="btn rp-interrupt" onClick={interrupt} title="interrupt the running turn"
                  disabled={!live.alive || (live.state !== "running" && live.state !== "waiting_permission")}>interrupt</button>
        </div>
      </div>
    </React.Fragment>
  );
}

// The folded view's rows and strip (rpModel's output as elements).
const RP_ACT_LABEL = { thinking: "thinking", working: "working", waiting: "waiting for permission" };

// What recall gave the cousin for one message (the runner's `recall`
// event): each item opens to the memory itself, read through the memory
// explorer's file route; a raw entry is one line of its jsonl.
const RP_RECALL_WHY = {
  "context present": "the message already carried its recall",
  "empty body": "nothing to search (not a chat message)",
};

function rpRecallTitle(rc) {
  const names = (rc.items || []).map(i => i.name).filter(Boolean);
  return names.length ? "recalled: " + names.join("; ") : "memories recalled for this message";
}

function RpRecallItem({ item, slug }) {
  const [body, setBody] = React.useState(null);
  const load = async (e) => {
    if (!e.currentTarget.open || body) return;
    const raw = item.collection === "raw";
    const [file, line] = raw ? String(item.rel || "").split("#") : [item.rel, null];
    const q = new URLSearchParams({ path: file || "" });
    if (raw && line) { q.set("start", line); q.set("count", "1"); }
    if (item.layer === "harness") q.set("layer", "harness");
    const d = await apiGet(`/api/memory/${encodeURIComponent(slug)}/file?${q}`);
    if (!d) { setBody({ err: "could not read " + item.rel }); return; }
    if (raw) {
      let entry = null;
      try { entry = JSON.parse((d.lines || [])[0] || ""); } catch (_e) { entry = null; }
      setBody(entry ? { entry } : { text: (d.lines || []).join("\n") });
    } else {
      setBody({ text: d.text != null ? d.text : (d.lines || []).join("\n") + (d.more ? "\n..." : "") });
    }
  };
  const meta = [item.collection, item.rel].filter(Boolean).join(" · ");
  return (
    <details className="rp-recall-item" onToggle={load}>
      <summary>
        <span className="rp-recall-name">{item.name || item.rel}</span>
        {item.level && <span className="rp-chip">{String(item.level).replace(/^L\d_/, "").toLowerCase()}</span>}
        {item.similarity != null && <span className="rp-chip" title="semantic similarity">{Number(item.similarity).toFixed(2)}</span>}
        <span className="rp-recall-src">{meta}</span>
      </summary>
      <div className="rp-recall-body">
        {!body ? <span className="muted">loading...</span>
          : body.err ? <span className="muted">{body.err}</span>
          : body.entry ? (
            <React.Fragment>
              <div>{body.entry.content}</div>
              <div className="rp-recall-src">{[body.entry.truth_level, body.entry.timestamp, body.entry.cite && ("cite: " + body.entry.cite)].filter(Boolean).join(" · ")}</div>
            </React.Fragment>)
          : renderMarkdownLite(body.text || "").map((n, j) => rpToReact(n, j))}
      </div>
    </details>
  );
}

function RpRecall({ recall, slug }) {
  const why = recall.timed_out ? "recall timed out" : (RP_RECALL_WHY[recall.skipped] || recall.skipped);
  if (why) return <div className="rp-recall muted">No recall: {why}</div>;
  if (!recall.hits) return <div className="rp-recall muted">No recall: nothing in memory matched</div>;
  const items = recall.items || [];
  return (
    <div className="rp-recall">
      <div className="rp-recall-head">Recalled for this message</div>
      {items.length ? items.map((it, i) => <RpRecallItem key={i} item={it} slug={slug} />)
        : <div className="rp-recall-src">{recall.text || recall.hits + " hits (this runner did not record which)"}</div>}
    </div>
  );
}

function RpRow({ row, now, activeTool, slug }) {
  const ev = row.ev || {};
  const p = ev.payload || {};
  switch (row.t) {
    case "turn": {
      const h = rpTurnHead(row);
      const rc = row.recall;
      const hits = rc && rc.hits;
      return (
        <details className={"rp-turn" + (row.mid ? " mid" : "")}>
          <summary title={h.title || undefined}>
            <span className="rp-turn-head">{h.head || "turn"}</span>
            {hits > 0 && <span className="rp-chip rp-recall-chip" title={rpRecallTitle(rc) + " (open to see them)"}>recall {hits}</span>}
            <span className="rp-turn-snip">{h.snippet}</span>
          </summary>
          <div className="rp-turn-full">{h.full}</div>
          {rc && slug && <RpRecall recall={rc} slug={slug} />}
        </details>
      );
    }
    case "tool": case "reply": {
      const lab = rpToolLabel(p.name, p.input);
      const res = row.result && row.result.payload;
      const err = !!(res && res.is_error);
      const dur = row.result && row.result.ts && ev.ts ? row.result.ts - ev.ts : (row.ms != null ? row.ms / 1000 : null);
      const pending = !row.result;
      const mark = pending ? <span className={"rp-spin" + (activeTool ? "" : " rp-spin-idle")} /> : <span className={err ? "rp-mark rp-err" : "rp-mark rp-ok"}>{err ? "✗" : "✓"}</span>;
      if (row.t === "reply") {
        const said = String((p.input || {}).text || "");
        const where = res && !err ? String(res.text || "").replace(/^replied to /, "") : (err ? rpCut(res.text, 120) : "sending...");
        return (
          <details className={"rp-card rp-reply" + (err ? " is-err" : "")}>
            <summary>{mark}<span className="rp-card-name">reply</span><span className="rp-card-desc">{where}</span><span className="rp-card-desc rp-reply-prev">{rpCut(said.replace(/\s+/g, " "), 120)}</span></summary>
            <div className="rp-card-body rp-sans">{renderMarkdownLite(said).map((n, j) => rpToReact(n, j))}</div>
          </details>
        );
      }
      return (
        <details className={"rp-card" + (err ? " is-err" : "")}>
          <summary title={lab.server ? "MCP server " + lab.server : undefined}>
            {mark}<span className="rp-card-name">{lab.name}</span>
            <span className="rp-card-desc">{lab.desc}</span>
            {dur != null && <span className="rp-card-dur">{rpElapsed(dur)}</span>}
          </summary>
          <div className="rp-card-body">
            <div className="rp-card-in">{rpToolInput(p.input).map((n, j) => rpToReact(n, j))}</div>
            {res && <div className="rp-card-out">{rpToolOutput(res.text).map((n, j) => rpToReact(n, j))}</div>}
          </div>
        </details>
      );
    }
    case "thinking": {
      const label = "thought" + (row.secs != null ? " " + rpElapsed(row.secs) : "");
      if (!p.text) return <div className="rp-line rp-thought">{label}</div>;
      return (
        <details className="rp-thought-d">
          <summary className="rp-thought">{label}</summary>
          <div className="rp-thought-text">{p.text}{p.truncated ? " [truncated]" : ""}</div>
        </details>
      );
    }
    case "text":
      return <div className="rp-text-row">{rpRowBody(ev)}</div>;
    case "error":
      return <div className="rp-line rp-err">error: {String(p.error || "")}</div>;
    case "line":
      return <div className={"rp-line " + row.cls} title={row.detail || undefined}>{row.text}</div>;
    case "footer": {
      const parts = rpFooterParts(row.meta);
      const bad = row.meta.result && (row.meta.result.is_error || row.meta.result.interrupted);
      return <div className={"rp-footer" + (bad ? " is-err" : "")}>{parts.join(" · ")}</div>;
    }
    case "boot":
      return (
        <details className="rp-boot">
          <summary>runner started · {row.events.length} setup {row.events.length === 1 ? "event" : "events"}</summary>
          {row.events.map((e, j) => (
            <div key={j} className="rp-boot-ev"><span className="rp-kind">{e.kind}</span> {rpRowBody(e)}</div>
          ))}
        </details>
      );
    default:
      return (
        <div className={"runner-ev rp-" + runnerKindClass(ev.kind)}>
          <span className="rp-kind">{ev.kind}</span>
          <div className="rp-body">{rpRowBody(ev)}</div>
        </div>
      );
  }
}

// The "N bg tasks" chip and the list it opens under the strip: the
// running tasks (what, agent or shell, how long, the last tool), then the
// last few ended ones. The list keeps its own clock, so the pane's rows do
// not re-render every second for it; a click outside or on the chip closes it.
function RpBgTasks({ strip }) {
  const [open, setOpen] = React.useState(false);
  const [now, setNow] = React.useState(() => Date.now());
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!open) return undefined;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    const away = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === "Escape") setOpen(false); };
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
  const secs = now / 1000;
  return (
    <span className="rp-bg" ref={ref}>
      <button type="button" className={"rp-chip rp-bg-chip" + (open ? " on" : "")} aria-expanded={open}
              title="background tasks: click for the list" onClick={() => setOpen(o => !o)}>
        {strip.bg} bg {strip.bg === 1 ? "task" : "tasks"}
      </button>
      {open && (
        <div className="rp-tasks" role="dialog" aria-label="background tasks">
          {list.running.map(t => (
            <div key={t.id} className="rp-task" title={t.desc}>
              <span className="rp-spin" />
              <span className="rp-task-desc">{t.desc || t.id}</span>
              <span className="rp-chip">{rpTaskType(t.type)}</span>
              {t.tool && <span className="rp-dim">{rpToolLabel(t.tool).name}</span>}
              <span className="rp-task-t">{t.started ? rpElapsed(Math.max(0, secs - t.started)) : ""}</span>
            </div>
          ))}
          {strip.bgUntracked > 0 && (
            <div className="rp-task rp-dim">{strip.bgUntracked} started with no details (recorded before 1.27)</div>
          )}
          {list.running.length === 0 && strip.bgUntracked === 0 && <div className="rp-task rp-dim">none running</div>}
          {list.ended.length > 0 && <div className="rp-tasks-sep">ended</div>}
          {list.ended.map(t => (
            <div key={t.id} className="rp-task is-ended" title={t.summary || t.desc}>
              <span className={"rp-mark " + (t.status === "completed" ? "rp-ok" : t.status === "failed" || t.status === "lost" ? "rp-err" : "rp-dim")}>
                {t.status === "completed" ? "✓" : t.status === "failed" || t.status === "lost" ? "✗" : "■"}
              </span>
              <span className="rp-task-desc">{t.desc || t.id}</span>
              <span className="rp-dim">{t.status}</span>
              <span className="rp-task-t">{fmtAgoShort(secs - t.ended)}</span>
            </div>
          ))}
        </div>
      )}
    </span>
  );
}

function RpStrip({ strip, now, raw, setRaw }) {
  const a = strip.activity;
  const rates = Object.keys(strip.rate).map(t => rpRate(t, strip.rate[t]));
  return (
    <div className="rp-strip">
      <span className={"rp-act" + (a ? " on" : "")}>
        {a ? <span className="rp-spin" /> : <span className="rp-idle-dot" />}
        {a ? (a.kind === "tool" ? a.name : RP_ACT_LABEL[a.kind] || a.kind) : (strip.state || "idle").replace(/_/g, " ")}
        {a && a.since ? <span className="rp-act-t">{rpElapsed(Math.max(0, now / 1000 - a.since))}</span> : null}
      </span>
      {rates.map(r => (
        <span key={r.label} className={"rp-rate is-" + r.level} title={"Claude usage limit, " + r.label + " window" + (r.resets ? ", resets " + r.resets : "")}>
          {r.label}
          {r.pct != null && <span className="rp-bar"><span style={{ width: r.pct + "%" }} /></span>}
          {r.pct != null && <span>{r.pct}%</span>}
          {r.resets && <span className="rp-dim">resets {r.resets}</span>}
        </span>
      ))}
      {strip.session && strip.session.model && (
        <span className={"rp-chip" + (strip.session.auth && strip.session.auth !== "your login" ? " is-warn" : "")} title="from the session's init: model and where its credentials come from">
          {strip.session.model.replace(/^claude-/, "")}{strip.session.auth ? " · " + strip.session.auth : ""}
        </span>
      )}
      {strip.auth && (
        <span className="rp-chip is-err" title={rpAuthLine(strip.auth).text}>login required</span>
      )}
      {strip.turns > 0 && (
        <span className="rp-dim" title="tokens and estimated cost of the turns in this view">
          {strip.turns} {strip.turns === 1 ? "turn" : "turns"} · {rpTok(strip.tokens)} tok{strip.cost ? " · $" + strip.cost.toFixed(2) : ""}
        </span>
      )}
      <RpBgTasks strip={strip} />
      <span style={{ flex: 1 }} />
      <label className="rp-raw" title="one row per stream event, as it arrives">
        <input type="checkbox" checked={raw} onChange={e => setRaw(e.target.checked)} /> raw
      </label>
    </div>
  );
}

// Track which messages have already been fully revealed, keyed by msg.id.
// Survives re-renders; uses a plain Set on window so fresh components do not
// re-animate an old message.
const _REVEALED = (window.__bubbleRevealed = window.__bubbleRevealed || new Set());

// Glyph palettes for the reveal effects (block glyphs, half-width katakana,
// dot leaders): decoration only, no words.
const _GLITCH_GLYPHS = "▓▒░█▚▞▛▜▟▙▂▅▆▇╬╫╪╧╦╩═║╠╣╔╗╚╝#@%&*+=/\\<>";
const _MATRIX_GLYPHS = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789";
const _BOOT_GLYPHS = "·.:°";

function _scrambleWithPalette(word, palette) {
  let out = "";
  for (const ch of word) {
    if (/\s/.test(ch)) { out += ch; continue; }
    out += palette[Math.floor(Math.random() * palette.length)];
  }
  return out;
}

const REACTION_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🎉", "🔥", "👀", "🙏", "✅"];

// The chat server's reaction rows are one per (user, emoji) with a tap
// count; the chips are one per emoji. Aggregate here, in row order.
function groupReactions(rows) {
  const by = new Map();
  for (const r of rows || []) {
    if (!r || !r.emoji) continue;
    let g = by.get(r.emoji);
    if (!g) { g = { emoji: r.emoji, count: 0, users: [] }; by.set(r.emoji, g); }
    g.count += Number(r.tap_count || 1);
    if (r.user && !g.users.includes(r.user)) g.users.push(r.user);
  }
  return Array.from(by.values());
}

// An attachment is a file the cousin's chat server wrote (the inbox) or a
// generated asset; the console projects it as `attachment: {url, kind}`
// on the message. attachmentMedia (media.jsx) resolves it, a `data:`
// image echo included, and InlineMedia renders it by kind.
function ChatBubble({ msg, cousin, search, isLast, onReply, chatUser, mediaShown, onOpenMedia }) {
  const isUser = msg.type === "user";
  // Parse incoming reply_to. The server stores it as a JSON string when the
  // sender provided a dict; older rows may already be objects.
  let replyQuote = null;
  if (msg.reply_to) {
    try {
      replyQuote = typeof msg.reply_to === "string"
        ? JSON.parse(msg.reply_to)
        : msg.reply_to;
    } catch (_) {}
  }
  // Local reactions state - seeded from the message and updated on tap
  // so we don't need to wait for the next history poll.
  const [reactions, setReactions] = React.useState(msg.reactions || []);
  React.useEffect(() => { setReactions(msg.reactions || []); }, [msg.reactions]);

  // A tap adds or bumps (a retap is an urgency signal, never a toggle off);
  // `remove` takes the user's reaction away. Both go through the console
  // to the cousin server's /api/reactions.
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
          action: action || "tap",
        }),
      });
      const d = await r.json();
      if (r.ok && Array.isArray(d.reactions)) setReactions(d.reactions);
    } catch (_) {}
  };

  // Action menu (right-click / long-press): floating popover at cursor.
  const [menu, setMenu] = React.useState(null); // {x, y}
  React.useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (e) => { if (e.key === "Escape") setMenu(null); };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  // Long-press detection for touch devices: 500ms hold opens the menu.
  const longPressTimer = React.useRef(null);
  const startLongPress = (e) => {
    const t = e.touches && e.touches[0];
    longPressTimer.current = setTimeout(() => {
      try { if (navigator.vibrate) navigator.vibrate(15); } catch (_) {}
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
  const streamSpeed = (window.__fwSettings && window.__fwSettings.chatStreamSpeed) || "normal";
  // Reveal only the tail message, and only while it is unread (not yet revealed).
  // Earlier messages just render instantly. User messages also render instantly.
  const shouldStream = isLast && !isUser && !_REVEALED.has(msg.id)
                       && fullText.length > 0 && streamSpeed !== "off";
  const [visible, setVisible] = React.useState(shouldStream ? "" : fullText);
  const [streaming, setStreaming] = React.useState(shouldStream);

  React.useEffect(() => {
    if (!shouldStream) return;
    const effect = (window.__fwSettings && window.__fwSettings.chatStreamEffect) || "plain";
    const pool = { slow: 3600, normal: 1800, fast: 900 }[streamSpeed] || 1800;

    // --- typewriter: char-by-char reveal. Great for short replies. ---
    if (effect === "typewriter") {
      const perChar = Math.max(6, Math.min(40, Math.floor(pool / Math.max(20, fullText.length))));
      let i = 0;
      const id = setInterval(() => {
        i += 1;
        if (i >= fullText.length) {
          setVisible(fullText);
          setStreaming(false);
          _REVEALED.add(msg.id);
          window.dispatchEvent(new CustomEvent("chat-stream-tick"));
          clearInterval(id);
          return;
        }
        setVisible(fullText.slice(0, i));
        window.dispatchEvent(new CustomEvent("chat-stream-tick"));
      }, perChar);
      return () => clearInterval(id);
    }

    // --- boot: sparse chunks with dot-leaders. ---
    if (effect === "boot") {
      const chunks = fullText.match(/[\s\S]{1,18}/g) || [fullText];
      const perChunk = Math.max(28, Math.min(140, Math.floor(pool / Math.max(1, chunks.length))));
      let i = 0;
      const id = setInterval(() => {
        i += 1;
        if (i >= chunks.length) {
          setVisible(fullText);
          setStreaming(false);
          _REVEALED.add(msg.id);
          window.dispatchEvent(new CustomEvent("chat-stream-tick"));
          clearInterval(id);
          return;
        }
        const head = chunks.slice(0, i).join("");
        const tail = _scrambleWithPalette(chunks.slice(i).join("").slice(0, 12), _BOOT_GLYPHS);
        setVisible(head + tail);
        window.dispatchEvent(new CustomEvent("chat-stream-tick"));
      }, perChunk);
      return () => clearInterval(id);
    }

    // --- glitch / matrix / plain: word-by-word with optional palette scramble. ---
    const words = fullText.split(/(\s+)/);
    const total = Math.min(words.length, 400);
    const perStep = Math.max(8, Math.min(80, Math.floor(pool / Math.max(1, total))));
    const palette = effect === "glitch" ? _GLITCH_GLYPHS
                  : effect === "matrix" ? _MATRIX_GLYPHS
                  : null;
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
    if (streaming) return;  // wait to render mermaid until full text is in
    if (!divRef.current || !window.mermaid) return;
    const blocks = divRef.current.querySelectorAll("pre code.language-mermaid, code.language-mermaid");
    blocks.forEach(async (block, i) => {
      const src = block.textContent;
      const id = `mmd-${msg.id}-${i}`;
      try {
        const { svg } = await window.mermaid.render(id, src);
        const container = document.createElement("div");
        container.className = "mermaid-rendered";
        container.innerHTML = svg;
        const parent = block.closest("pre") || block;
        parent.replaceWith(container);
      } catch (e) { /* leave code block alone */ }
    });
  }, [rendered, streaming]);

  const media = attachmentMedia(msg);
  const chips = groupReactions(reactions);

  return (
    <div data-msg-id={msg.id} style={{
      marginBottom: 10,
      display: "flex",
      flexDirection: "column",
      alignItems: isUser ? "flex-end" : "flex-start",
    }}>
      <div className={`chat-bubble ${isUser ? "chat-bubble-user" : "chat-bubble-assistant"} ${streaming ? "chat-streaming" : ""}`}
           onContextMenu={onContextMenu}
           onTouchStart={startLongPress}
           onTouchEnd={cancelLongPress}
           onTouchMove={cancelLongPress}
           onTouchCancel={cancelLongPress}>
        {replyQuote && (
          <div style={{
            marginBottom: 6, padding: "4px 8px",
            borderLeft: "3px solid var(--accent)",
            background: "oklch(from var(--accent) l c h / 0.08)",
            borderRadius: 3,
            fontSize: 11, fontFamily: "var(--mono)",
            color: "var(--fg-3)",
            cursor: replyQuote.id ? "pointer" : "default",
          }}
          title={replyQuote.id ? "click to scroll to original" : ""}
          onClick={() => {
            if (!replyQuote.id) return;
            const el = document.querySelector(`[data-msg-id="${replyQuote.id}"]`);
            if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "smooth" });
          }}>
            <div style={{ color: "var(--accent)", fontWeight: 500 }}>
              &#8617; {replyQuote.user || "msg"}
            </div>
            <div style={{ whiteSpace: "nowrap", textOverflow: "ellipsis", overflow: "hidden" }}>
              {replyQuote.snippet || ""}
            </div>
          </div>
        )}
        {media && (
          <div className="chat-media-slot" style={{ marginBottom: visible ? 6 : 0 }}>
            <InlineMedia media={media} shown={mediaShown !== false}
                         onOpen={onOpenMedia ? () => onOpenMedia(msg) : null} />
          </div>
        )}
        <div ref={divRef} className="chat-md" dangerouslySetInnerHTML={{ __html: rendered }} />
        <div className="chat-meta">{fmtShortTime(msg.timestamp)}</div>
      </div>
      {chips.length > 0 && (
        <div style={{
          marginTop: 4, display: "flex", gap: 4, flexWrap: "wrap",
          justifyContent: isUser ? "flex-end" : "flex-start",
        }}>
          {chips.map(r => {
            const mine = !!chatUser && r.users.includes(chatUser);
            return (
              <button key={r.emoji}
                onClick={() => react(r.emoji, "tap")}
                onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); if (mine) react(r.emoji, "remove"); }}
                title={r.users.join(", ") + (mine ? " (click: tap again; right-click: remove yours)" : " (click: tap)")}
                style={{
                  fontSize: 12, padding: "2px 8px", borderRadius: 12,
                  background: mine ? "oklch(from var(--accent) l c h / 0.22)" : "var(--bg-1)",
                  border: mine ? "1px solid var(--accent)" : "1px solid var(--line)",
                  color: "var(--fg-1)", cursor: "pointer",
                  display: "inline-flex", alignItems: "center", gap: 4,
                }}>
                <span>{r.emoji}</span>
                <span style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-2)" }}>{r.count}</span>
              </button>
            );
          })}
        </div>
      )}
      {menu && (
        <div onClick={e => e.stopPropagation()}
             style={{
               position: "fixed", left: menu.x, top: menu.y,
               background: "var(--bg-1)", border: "1px solid var(--line)",
               borderRadius: 10, padding: 6, zIndex: 9000,
               boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
               display: "flex", flexDirection: "column", gap: 4,
               maxWidth: "92vw",
             }}>
          {onReply && (
            <button onClick={() => { onReply(); setMenu(null); }}
                    className="btn ghost" style={{ fontSize: 12, padding: "4px 10px", textAlign: "left" }}>
              &#8617; Reply
            </button>
          )}
          <div style={{ display: "flex", gap: 2, flexWrap: "wrap", maxWidth: 260 }}>
            {REACTION_EMOJIS.map(e => (
              <button key={e}
                onClick={() => { react(e, "tap"); setMenu(null); }}
                disabled={!chatUser}
                style={{
                  fontSize: 18, lineHeight: 1, padding: "4px 6px",
                  background: "transparent", border: "1px solid transparent",
                  borderRadius: 4, cursor: "pointer",
                }}
                onMouseEnter={ev => ev.currentTarget.style.background = "var(--bg-0)"}
                onMouseLeave={ev => ev.currentTarget.style.background = "transparent"}>
                {e}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function renderMarkdown(text, highlight) {
  let html;
  if (!window.marked) html = escapeHtml(text).replace(/\n/g, "<br/>");
  else {
    try { html = window.marked.parse(text, { breaks: true, gfm: true }); }
    catch (e) { html = escapeHtml(text).replace(/\n/g, "<br/>"); }
  }
  return highlight ? highlightHtml(html, highlight) : html;
}

// HTML-aware highlighter: walks html char by char, only wraps matches inside text nodes, never inside tags.
function highlightHtml(html, query) {
  const q = (query || "").trim();
  if (!q) return html;
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp("(" + escaped + ")", "gi");
  let out = "", i = 0;
  while (i < html.length) {
    if (html[i] === "<") {
      const end = html.indexOf(">", i);
      if (end === -1) { out += html.slice(i); break; }
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
  return (s || "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

function fmtAgoShort(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 2) return "just now";
  if (sec < 60) return sec + "s ago";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  return Math.floor(sec / 3600) + "h ago";
}

// The browser's own locale: the console hardcodes none.
function fmtShortTime(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ""; }
}

Object.assign(window, { ChatView, ChatBubble, PaneView, RunnerPaneView, rpModel, rpCompact, runnerEventLine, runnerEventBody, paneLiveness, runnerFleetKey, renderMarkdown, resolveChatUser, groupReactions });
