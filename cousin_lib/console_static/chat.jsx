// Chat view: the per-cousin chat widget and the live terminal pane.
// Renders markdown via marked, diagrams via mermaid. Polls /api/messages
// every 3.5 s in live mode. Every route it calls is in docs/console-spec.md.

// The chat user is the thread the view reads and writes: `?user=` on the
// page (passed in as embedUser), else the cousin's configured operator,
// else the console session's user. With none of the three the composer is
// disabled and says why: there is no default operator anywhere.
function resolveChatUser(embedUser, cousin, sessionUser) {
  return embedUser || (cousin && cousin.operator) || sessionUser || "";
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
  const [paneOpen, setPaneOpen] = React.useState(() => {
    try { return localStorage.getItem("fw_pane_open") === "1"; }
    catch (e) { return false; }
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
    try { localStorage.setItem("fw_pane_open", paneOpen ? "1" : "0"); }
    catch (e) { /* ignore */ }
  }, [paneOpen]);
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

  return (
    <div style={{ position: "relative", height: "100%", minHeight: 0 }}>
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "row", overflow: "hidden" }}>
        {/* LEFT: chat - fills the window until the pane takes over */}
        <div
          className="chat-col"
          style={{
            flex: paneOpen ? "0 0 0" : "1 1 auto",
            minWidth: 0,
            overflow: "hidden",
            position: "relative",              /* anchor for the jump-to-bottom button */
            display: "flex", flexDirection: "column",
            transition: "flex 240ms cubic-bezier(0.4, 0, 0.2, 1)",
          }}
        >
          <ChatHeader cousin={c} chatUser={chatUser} paneOpen={paneOpen} setPaneOpen={setPaneOpen} search={search} setSearch={setSearch} onArchive={onArchive} fullscreen={fullscreen} setFullscreen={embed ? null : setFullscreen} embed={embed} showArchived={showArchived} setShowArchived={setShowArchived} />
          {fullscreen && (
            <button className="chat-fullscreen-exit"
                    onClick={() => setFullscreen(false)}
                    title="exit fullscreen">x exit</button>
          )}
          {/* key by slug: remount ChatBody on cousin switch so its messages +
              draft state reset to empty. Without this React reuses the instance
              and the previous cousin's messages render until the new fetch lands
              -- a cross-cousin content leak between private chats. */}
          <ChatBody key={c.slug + "|" + chatUser} cousin={c} search={search} setSearch={setSearch} chatUser={chatUser} showArchived={showArchived} />
          {toast && (
            <div style={{
              position: "absolute", bottom: 90, left: "50%", transform: "translateX(-50%)",
              background: "var(--bg-2)", color: "var(--fg-0)",
              border: "1px solid var(--line)", borderRadius: 4,
              padding: "6px 12px", fontFamily: "var(--mono)", fontSize: 11,
              zIndex: 10,
            }}>{toast}</div>
          )}
        </div>
        {/* RIGHT: terminal pane - slides in from the right, takes the whole chat window when open */}
        <div
          className={`pane-col ${paneOpen ? "open" : ""}`}
          style={{
            flex: paneOpen ? "1 1 auto" : "0 0 0",
            minWidth: 0,
            overflow: "hidden",
            display: "flex", flexDirection: "column",
            background: "var(--bg-0)",
            transition: "flex 240ms cubic-bezier(0.4, 0, 0.2, 1)",
          }}
        >
          {paneOpen && <PaneView cousin={c} onClose={() => setPaneOpen(false)} />}
        </div>
      </div>
    </div>
  );
}

function ChatHeader({ cousin, chatUser, paneOpen, setPaneOpen, search, setSearch, onArchive, fullscreen, setFullscreen, embed, showArchived, setShowArchived }) {
  const btnH = 24;  // shared height for input + buttons

  // Effort: the levels come from the server (GET /api/spawn/options),
  // the current value from the cousin row, and a change persists to
  // cousin.toml [runtime] through the effort route. The running agent
  // keeps the level it started with, so a saved change shows "restart
  // to apply" rather than pretending it is live.
  const [efforts, setEfforts] = React.useState([]);
  const [effort, setEffort] = React.useState(cousin.effort || "");
  const [effortHint, setEffortHint] = React.useState(null);
  React.useEffect(() => { setEffort(cousin.effort || ""); setEffortHint(null); }, [cousin.effort, cousin.slug]);
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      const d = await apiGet("/api/spawn/options");
      if (cancelled || !d) return;
      setEfforts(d.efforts || []);
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

  return (
    <div className="chat-header" style={{
      display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
      padding: "8px 14px",
      borderBottom: "1px solid var(--line)",
      background: "var(--bg-1)",
      fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)",
    }}>
      <span>chat &middot; @{cousin.slug}{chatUser ? ` as ${chatUser}` : ""}</span>
      <span style={{ flex: 1 }} />
      {!embed && (
        <label style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span>effort</span>
          <select
            value={effort}
            onChange={e => applyEffort(e.target.value)}
            disabled={!efforts.length}
            title="cousin.toml [runtime] effort: rendered into the agent command at the next start"
            style={{
              fontFamily: "var(--mono)", fontSize: 11,
              background: "var(--bg-0)", color: "var(--fg-0)",
              border: "1px solid var(--line)", borderRadius: 3,
              padding: "0 6px", height: btnH, boxSizing: "border-box", outline: "none",
            }}
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
      <button
        className="btn ghost"
        onClick={onArchive}
        disabled={!chatUser}
        title="archive ALL active messages"
        style={{ height: btnH, padding: "0 10px", boxSizing: "border-box" }}
      >archive</button>
      <button
        className="btn ghost"
        onClick={() => setShowArchived(v => !v)}
        title={showArchived ? "showing archived messages, click to return to live" : "browse archived messages"}
        style={{
          height: btnH, padding: "0 10px", boxSizing: "border-box",
          background: showArchived ? "var(--accent)" : undefined,
          color: showArchived ? "var(--bg-0)" : undefined,
        }}
      >{showArchived ? "live" : "archived"}</button>
      <input
        className="chat-search"
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="search..."
        style={{
          fontFamily: "var(--mono)", fontSize: 11,
          background: "var(--bg-0)", color: "var(--fg-0)",
          border: "1px solid var(--line)", borderRadius: 3,
          padding: "0 8px", width: 140, maxWidth: "40vw", height: btnH, lineHeight: `${btnH - 2}px`,
          boxSizing: "border-box", outline: "none",
        }}
      />
      {!paneOpen && !embed && (
        <button
          className="btn ghost"
          onClick={() => setPaneOpen(true)}
          title="show the terminal pane"
          style={{ height: btnH, padding: "0 10px", boxSizing: "border-box" }}
        >pane &rsaquo;</button>
      )}
      {setFullscreen && !embed && (
        <button
          className="btn ghost chat-fullscreen-toggle"
          onClick={() => setFullscreen(v => !v)}
          title={fullscreen ? "exit fullscreen" : "fullscreen chat"}
          style={{ height: btnH, padding: "0 8px", boxSizing: "border-box" }}
        >{fullscreen ? "⤢ exit" : "⤢"}</button>
      )}
    </div>
  );
}

function ChatBody({ cousin, search, setSearch, chatUser, showArchived }) {
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
  const jumpToBottom = () => {
    const el = scrollerRef.current;
    if (!el) return;
    atBottomRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setAtBottom(true);
  };

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
      const r = await fetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) {
        setError(d.error || `HTTP ${r.status}`);
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
          padding: "6px 16px", borderBottom: "1px solid var(--line)",
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
          flex: 1, overflowY: "auto", overflowX: "hidden", padding: "14px 18px",
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
            onReply={() => setReplyingTo({
              id: m.id, user: m.user,
              snippet: (m.message || "").replace(/\s+/g, " ").slice(0, 120),
            })}
          />
        ))}
        <div ref={chatAnchorRef} style={{ height: 1 }} />
      </div>
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
        style={{
        borderTop: "1px solid var(--line)",
        background: dragOver ? "oklch(from var(--accent) l c h / 0.18)" : "var(--bg-1)",
        outline: dragOver ? "2px dashed var(--accent)" : "none",
        outlineOffset: -4,
        padding: "10px 14px", display: "flex", flexDirection: "column", gap: 6,
      }}>
        {replyingTo && (
          <div style={{ display: "flex", alignItems: "stretch", gap: 8, padding: 6,
                        background: "var(--bg-0)", border: "1px solid var(--line)", borderRadius: 3,
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
                        background: "var(--bg-0)", border: "1px solid var(--line)", borderRadius: 3 }}>
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
            background: "var(--bg-0)",
            color: "var(--fg-0)",
            border: "1px solid var(--line)",
            borderRadius: 3,
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

function PaneView({ cousin, onClose }) {
  const slug = cousin && cousin.slug;
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
    applyingRef.current = true;
    paneStateRef.current = state || null;
    term.reset();
    term.write(text, () => {
      try { term.scrollToBottom(); } catch (_e) {}
      applyingRef.current = false;
    });
    lastTextRef.current = text;
    // reset() drops xterm's internal focus state; re-focus so the
    // onData handler keeps picking up keystrokes.
    setTimeout(() => { try { term.focus(); } catch (_) {} }, 10);
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
      const buf = toPaneRows(inputBufRef.current);
      inputBufRef.current = "";
      flushTimerRef.current = null;
      if (!buf) return;
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
      <div style={{
        display: "flex", alignItems: "center", gap: 0,
        padding: "6px 14px", borderBottom: "1px solid var(--line)",
        fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)",
        background: "var(--bg-1)",
      }}>
        <span>tmux {tmuxSession} &middot; </span>
        <span style={{ color: status === "live" ? "var(--green)" : status === "connecting" ? "var(--amber)" : "var(--red)" }}>{status}</span>
        <span style={{ marginLeft: 10, color: "var(--fg-2)" }}>interactive</span>
        {lastPoll && <span style={{ marginLeft: 10 }}>polled {fmtAgoShort((Date.now() - lastPoll.getTime()) / 1000)}</span>}
        {lastChange && <span style={{ marginLeft: 10 }}>changed {fmtAgoShort((Date.now() - lastChange.getTime()) / 1000)}</span>}
        {sendError && <span style={{ marginLeft: 10, color: "var(--red)" }}>input err: {sendError}</span>}
        <span style={{ flex: 1 }} />
        {held && (
          <button className="btn ghost"
                  onClick={() => { const t = termRef.current; if (t) t.scrollToBottom(); }}
                  title="scrolled back: live updates are held until you return to the bottom"
                  style={{ padding: "0 6px", minHeight: 20, marginRight: 6, color: "var(--amber)" }}
          >scrolled back &middot; jump to live</button>
        )}
        {onClose && <button className="btn ghost" onClick={onClose} title="collapse the terminal pane" style={{ padding: "0 6px", minHeight: 20 }}>x</button>}
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

// An inbound attachment is a file the cousin's chat server wrote; the
// console projects it as `attachment.url` on the message. A `data:` image
// on a row (a client-side echo) is accepted too. Nothing else renders.
function attachmentSrc(msg) {
  if (msg.attachment && typeof msg.attachment.url === "string") return msg.attachment.url;
  if (typeof msg.image === "string" && msg.image.startsWith("data:image/")) return msg.image;
  return null;
}

function ChatBubble({ msg, cousin, search, isLast, onReply, chatUser }) {
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

  const imgSrc = attachmentSrc(msg);
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
        {imgSrc && (
          // The attachment opens in its own tab at full size; there is no
          // in-page viewer.
          <a href={imgSrc} target="_blank" rel="noopener noreferrer" title="open full size"
             style={{ display: "block", marginBottom: visible ? 6 : 0 }}>
            <img src={imgSrc} alt="attachment" className="chat-attachment"
                 style={{ maxWidth: "100%", maxHeight: 360, borderRadius: 3, display: "block" }} />
          </a>
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
               borderRadius: 4, padding: 6, zIndex: 9000,
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

Object.assign(window, { ChatView, ChatBubble, PaneView, renderMarkdown, resolveChatUser, groupReactions });
