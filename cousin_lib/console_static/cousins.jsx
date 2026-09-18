// Cousins view: cards, inspector drawer, editors, spawn / dismiss / flip
function CousinsView({ cousins, setCousins, openLogs, setActiveCousin }) {
  const [selected, setSelected] = React.useState(null);
  const [spawning, setSpawning] = React.useState(false);
  const [toast, setToast] = React.useState(null);
  const [confirmDeleteSlug, setConfirmDeleteSlug] = React.useState(null);
  const [flipModalCousin, setFlipModalCousin] = React.useState(null);

  const toastTimerRef = React.useRef(null);
  React.useEffect(() => () => { if (toastTimerRef.current) clearTimeout(toastTimerRef.current); }, []);
  const flash = (t, ms = 1800) => {
    setToast(t);
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(null), ms);
  };

  React.useEffect(() => {
    if (!confirmDeleteSlug) return;
    const t = setTimeout(() => setConfirmDeleteSlug(null), 3500);
    return () => clearTimeout(t);
  }, [confirmDeleteSlug]);

  const act = async (c, action) => {
    try {
      if (action === "delete") {
        // Dismiss: the server stops the cousin, archives the whole home to
        // data/dismissed/ and refuses the delete when the archive fails.
        if (confirmDeleteSlug !== c.slug) {
          setConfirmDeleteSlug(c.slug);
          flash(`click delete again within 3.5s to dismiss ${c.slug} (the home is archived first)`, 3500);
          return;
        }
        setConfirmDeleteSlug(null);
        const { r, d } = await apiSend("DELETE", `/api/cousins/${c.slug}`);
        if (r.ok && d.ok) {
          const left = (d.left_in_place || []).length;
          flash(`dismissed ${c.slug} · archived to ${d.archive}${left ? ` · ${left} harness dir(s) left in place` : ""}`, 6000);
          setCousins(cs => cs.filter(x => x.slug !== c.slug));
        } else {
          flash("dismiss refused: " + (d.error || `HTTP ${r.status}`), 6000);
        }
        return;
      }
      if (action === "flip") {
        // Open the slug-typed confirm modal; the fetch lives in FlipModal so
        // the operator can pick "now" vs a delay.
        setFlipModalCousin(c);
        return;
      }
      if (action === "restart") {
        const { r, d } = await apiSend("POST", `/api/cousins/${c.slug}/restart`);
        if (r.ok && d.ok) flash(`restarted ${c.slug}`);
        else flash("restart failed: " + (d.error || (d.start && d.start.error) || `HTTP ${r.status}`), 4000);
        return;
      }
      const { r, d } = action === "start"
        ? await apiSend("POST", `/api/cousins/${c.slug}/start`)
        : await apiSend("POST", `/api/cousins/${c.slug}/stop`);
      if (r.ok && d.ok) {
        flash((action === "start" ? "started " : "stopped ") + c.slug + (d.status === "already running" ? " (already running)" : ""));
      } else {
        flash("failed: " + (d.error || `HTTP ${r.status}`), 4000);
      }
    } catch (e) {
      flash("error: " + (e.message || e), 4000);
    }
  };

  const showHidden = (window.useSetting && window.useSetting("showHidden")) || false;
  const visibleCousins = cousins.filter(c => showHidden || !c.hidden);
  const hiddenCount = cousins.filter(c => c.hidden).length;

  return (
    <div className="wrap-pad">
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
        <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
          {visibleCousins.length} cousins · {visibleCousins.filter(c => c.status === "running").length} running
          {hiddenCount > 0 && !showHidden && <span style={{ marginLeft: 8, color: "var(--fg-3)" }}>· {hiddenCount} hidden</span>}
        </div>
        <div style={{ flex: 1 }} />
        <button className="btn primary" onClick={() => setSpawning(true)}>
          {I.plus} spawn cousin
        </button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 14 }}>
        {visibleCousins.map(c => (
          <CousinCard
            key={c.slug} c={c}
            onClick={() => setSelected(c.slug)}
            onAct={act}
            onChat={() => setActiveCousin(c.slug)}
          />
        ))}
      </div>

      {selected && (
        <Inspector
          cousin={cousins.find(c => c.slug === selected)}
          onClose={() => setSelected(null)}
          onAct={act}
          openLogs={openLogs}
        />
      )}
      {spawning && (
        <SpawnModal
          onClose={() => setSpawning(false)}
          onSpawn={(c) => {
            setCousins(cs => [...cs.filter(x => x.slug !== c.slug), c]);
            setSpawning(false);
            flash(`spawned cousin ${c.slug} on port ${c.port}`);
          }}
        />
      )}
      {flipModalCousin && (
        <FlipModal
          cousin={flipModalCousin}
          onClose={() => setFlipModalCousin(null)}
          onFlash={flash}
        />
      )}
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function CousinCard({ c, onClick, onAct, onChat }) {
  const isWorker = c.type === "worker";
  return (
    <div className="cousin-card" onClick={onClick}>
      <div className="name-row">
        <Led state={c.status === "running" ? "running" : "stopped"} pulse={c.status === "running"} />
        <div className="name">{c.name}</div>
        <div className="slug">@{c.slug}</div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          {isWorker && <Pill tone="gray">worker</Pill>}
          {c.status === "running"
            ? <Pill tone={c.active ? "green" : "amber"}>{isWorker ? "enrolled" : c.active ? "active" : "idle"}</Pill>
            : <Pill tone="gray">stopped</Pill>}
        </div>
      </div>
      <div className="role">{c.role}</div>
      <div style={{ margin: "4px 0" }}>
        <HeartbeatGraph state={c.status === "running" ? (c.active ? "active" : "idle") : "stopped"} width={240} height={22} />
      </div>
      <div className="stats">
        <div>chat · <b>{c.chat === "ok" ? `:${c.port}` : c.chat}</b></div>
        <div>scope · <b>{c.memoryScope}</b></div>
        <div>operator · <b>{c.operator || "-"}</b></div>
        <div>beat · <b>{c.heartbeat}s</b></div>
        <div>flip at · <b>{c.flipAt || "-"}</b></div>
        <div>host · <b>{c.host || "local"}</b></div>
      </div>
      {c.activity && (
        <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-2)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
             title={c.activity}>
          {c.activity}
        </div>
      )}
      <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)", display: "flex", gap: 8, alignItems: "center" }}>
        <span>tokens today · <b style={{ color: "var(--fg-1)" }}>{fmtTokens(c.tokensSpent)}</b></span>
      </div>
      <div className="actions" onClick={e => e.stopPropagation()}>
        {c.status === "running" ? (
          <button className="btn" onClick={() => onAct(c, "stop")}>{I.stop} stop</button>
        ) : (
          <button className="btn" onClick={() => onAct(c, "start")}>{I.play} start</button>
        )}
        <button className="btn danger" onClick={() => onAct(c, "delete")} title="dismiss cousin (stop, archive the home, remove it)">dismiss</button>
        <div style={{ flex: 1 }} />
        {!isWorker && (
          <button className="btn" onClick={() => onChat(c.slug)}>open chat →</button>
        )}
      </div>
    </div>
  );
}

function Inspector({ cousin: c, onClose, onAct, openLogs }) {
  React.useEffect(() => {
    if (!c) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [c, onClose]);
  if (!c) return null;
  return (
    <div className="inspector">
      <div className="hdr">
        <Led state={c.status === "running" ? "running" : "stopped"} pulse={c.status === "running"} />
        <span style={{ color: "var(--fg-0)" }}>{c.name}</span>
        <span style={{ color: "var(--fg-3)" }}>@{c.slug}</span>
        <button className="close" onClick={onClose}>×</button>
      </div>
      <div className="body">
        <RoleEditor cousin={c} />

        <SectionLabel>identity</SectionLabel>
        <dl className="kv">
          <dt>slug</dt><dd>{c.slug}</dd>
          <dt>type</dt><dd>{c.type}</dd>
          <dt>home</dt><dd style={{ wordBreak: "break-all" }}>{c.home}</dd>
          <dt>operator</dt><dd>{c.operator || <span style={{ color: "var(--fg-3)" }}>none</span>}</dd>
          <dt>scope</dt><dd>{c.memoryScope}</dd>
          <dt>tmux</dt><dd>{c.tmuxSession}{c.host ? ` @ ${c.host}` : ""}</dd>
          <dt>chat</dt><dd>{c.port ? `:${c.port} · ${c.chat}` : "none"}</dd>
          <dt>heartbeat</dt><dd>{c.heartbeat}s</dd>
          <dt>flip at</dt><dd>{c.flipAt || <span style={{ color: "var(--fg-3)" }}>-</span>}</dd>
          <dt>activity</dt><dd>{c.activity || <span style={{ color: "var(--fg-3)" }}>-</span>}</dd>
        </dl>

        <SectionLabel style={{ marginTop: 20 }}>tokens · today</SectionLabel>
        <div style={{ fontFamily: "var(--mono)", fontSize: 12, color: "var(--fg-1)", marginTop: 6 }}>
          {fmtTokens(c.tokensSpent)} spent
        </div>

        <PeerMessagePanel cousin={c} />

        <ClaudeMdEditor cousin={c} />

        <LoopsEditor cousin={c} />

        <FlipStatus cousin={c} />

        <div style={{ display: "flex", gap: 8, marginTop: 20, flexWrap: "wrap" }}>
          {c.status === "running"
            ? <button className="btn" onClick={() => onAct(c, "stop")}>{I.stop} stop</button>
            : <button className="btn" onClick={() => onAct(c, "start")}>{I.play} start</button>}
          <button className="btn danger" onClick={() => onAct(c, "kill")}>{I.kill} kill</button>
          {c.status === "running" && (
            <button className="btn" onClick={() => onAct(c, "flip")}
                     title="cousin-flip: respawn on a fresh session-id, preserve identity via the boot packet">
              flip
            </button>
          )}
          {c.status === "running" && (
            <button className="btn" onClick={() => onAct(c, "restart")}
                     title="restart tmux + chat server (stop + start)">
              restart
            </button>
          )}
          <button className="btn" onClick={() => openLogs(c.slug)}>tail logs →</button>
          <HideCousinButton cousin={c} />
        </div>
      </div>
    </div>
  );
}

function HideCousinButton({ cousin: c }) {
  const [busy, setBusy] = React.useState(false);
  const [hidden, setHidden] = React.useState(!!c.hidden);
  React.useEffect(() => { setHidden(!!c.hidden); }, [c.hidden]);
  const toggle = async () => {
    setBusy(true);
    try {
      const { d } = await apiSend("POST", `/api/cousins/${c.slug}/hidden`, { hidden: !hidden });
      if (d.ok) setHidden(!!d.hidden);
    } catch (_e) { /* swallow */ }
    finally { setBusy(false); }
  };
  return (
    <button className="btn" onClick={toggle} disabled={busy}
      title="hidden cousins disappear from the sidebar and the Cousins view (Settings: show hidden reveals them)">
      {hidden ? "unhide" : "hide"}
    </button>
  );
}

// The state of the last flip the console ran plus any pending timed flip
// (GET /api/cousins/<slug>/flip), polled while the inspector is open.
function FlipStatus({ cousin }) {
  const [st, setSt] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      const d = await apiGet(`/api/cousins/${cousin.slug}/flip`);
      if (!cancelled && d) setSt(d);
    };
    pull();
    const id = setInterval(pull, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [cousin.slug]);
  const cancelPending = async () => {
    try { await apiSend("POST", `/api/cousins/${cousin.slug}/flip/cancel`); } catch (_e) {}
  };
  if (!st) return null;
  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>flip</SectionLabel>
      <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-2)", display: "flex", flexDirection: "column", gap: 4 }}>
        <div>
          last: <StatePill state={st.status === "done" ? "done" : st.status === "running" ? "running" : st.status === "failed" ? "failed" : "idle"} />
          {st.status === "stale_marker" && <span style={{ color: "var(--amber)", marginLeft: 6 }}>stale marker from a crashed flip</span>}
          {st.result && st.result.new_generation != null && <span style={{ marginLeft: 6 }}>gen {st.result.new_generation}</span>}
        </div>
        {st.pending && (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ color: "var(--amber)" }}>timed flip pending · fires in {fmtDuration(st.pending.seconds_until_fire)} (request #{st.pending.request_id})</span>
            <button className="btn ghost" style={{ fontSize: 10, padding: "2px 8px", minHeight: 18 }} onClick={cancelPending}>cancel</button>
          </div>
        )}
      </div>
    </>
  );
}

function PeerMessagePanel({ cousin }) {
  const [peers, setPeers] = React.useState([]);
  const [to, setTo] = React.useState("");
  const [text, setText] = React.useState("");
  const [sending, setSending] = React.useState(false);
  const [status, setStatus] = React.useState(null);

  React.useEffect(() => {
    fetchCousins().then(all => {
      const others = all.filter(c => c.slug !== cousin.slug && c.type !== "worker");
      setPeers(others);
      if (others.length && !to) setTo(others[0].slug);
    });
  }, [cousin.slug]);

  const send = async () => {
    if (!to || !text.trim() || sending) return;
    setSending(true);
    setStatus(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/peer`, { to, text });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setText("");
      setStatus({ ok: true, msg: `delivered to @${to} (id ${d.id})` });
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally {
      setSending(false);
    }
  };

  return (
    <div style={{ marginTop: 20 }}>
      <SectionLabel>message a peer</SectionLabel>
      {peers.length === 0 ? (
        <div style={{ color: "var(--fg-3)", fontSize: 11, fontFamily: "var(--mono)" }}>no peers registered yet.</div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
            <select className="sel" value={to} onChange={e => setTo(e.target.value)} style={{ width: 120 }}>
              {peers.map(p => <option key={p.slug} value={p.slug}>@{p.slug}</option>)}
            </select>
            <input className="txt" value={text} onChange={e => setText(e.target.value)}
                   onKeyDown={e => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), send())}
                   placeholder="message (Enter to send)" style={{ flex: 1 }} />
            <button className="btn" onClick={send} disabled={!text.trim() || sending}>send</button>
          </div>
          {status && (
            <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: status.ok ? "var(--green)" : "var(--red)" }}>
              {status.msg}
            </div>
          )}
          <div style={{ fontSize: 10, color: "var(--fg-3)", marginTop: 4 }}>
            Lands in the destination's own chat history as a line from {cousin.name}; that history is the record.
          </div>
        </>
      )}
    </div>
  );
}

function RoleEditor({ cousin }) {
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(cousin.role || "");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);

  // Reset draft when the surrounding cousin changes (Inspector switches target).
  React.useEffect(() => { setDraft(cousin.role || ""); setEditing(false); setErr(null); }, [cousin.slug]);

  const save = async () => {
    setBusy(true); setErr(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/role`, { role: draft });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      // Update the cached value so the parent re-renders the right text.
      cousin.role = d.role;
      setEditing(false);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return (
      <div style={{ marginBottom: 18, lineHeight: 1.6, position: "relative", display: "flex", alignItems: "flex-start", gap: 8 }}>
        <span style={{ flex: 1, fontSize: 12, color: "var(--fg-2)" }}>{cousin.role || <span style={{ color: "var(--fg-3)" }}>(no role set)</span>}</span>
        <button className="btn ghost" onClick={() => setEditing(true)}
          style={{ fontSize: 10, padding: "2px 8px", minHeight: 18 }}>edit</button>
      </div>
    );
  }
  return (
    <div style={{ marginBottom: 18 }}>
      <textarea value={draft} onChange={e => setDraft(e.target.value)}
        style={{
          width: "100%", minHeight: 120, resize: "vertical",
          fontFamily: "var(--mono)", fontSize: 12, lineHeight: 1.5,
          background: "var(--bg-0)", color: "var(--fg-0)",
          border: "1px solid var(--amber)", borderRadius: 3, padding: 8, boxSizing: "border-box",
        }} />
      <div style={{ display: "flex", gap: 6, marginTop: 6, alignItems: "center" }}>
        <span style={{ fontSize: 10, fontFamily: "var(--mono)", color: draft.length > 5000 ? "var(--red)" : "var(--fg-3)" }}>{draft.length} / 5000 chars</span>
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={() => { setDraft(cousin.role || ""); setEditing(false); setErr(null); }} disabled={busy}>cancel</button>
        <button className="btn primary" onClick={save} disabled={busy || draft.length > 5000}>{busy ? "saving..." : "save"}</button>
      </div>
      {err && <div style={{ marginTop: 6, padding: "5px 9px", fontSize: 11, fontFamily: "var(--mono)",
                            color: "var(--red)", background: "oklch(from var(--red) l c h / 0.08)", borderRadius: 3 }}>{err}</div>}
      <div style={{ marginTop: 4, fontSize: 10, color: "var(--fg-3)" }}>
        Saved as the [cousin] role line in cousin.toml. The cousin needs a session restart to pick it up if CLAUDE.md references it.
      </div>
    </div>
  );
}

function ClaudeMdEditor({ cousin }) {
  const [open, setOpen] = React.useState(false);
  const [content, setContent] = React.useState("");
  const [original, setOriginal] = React.useState("");
  const [loaded, setLoaded] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);

  const load = React.useCallback(async () => {
    setLoaded(false); setMsg(null);
    try {
      const r = await fetch(`/api/cousins/${cousin.slug}/claude-md`, { cache: "no-store" });
      const d = await r.json();
      if (r.ok && d.ok) {
        setContent(d.content || ""); setOriginal(d.content || ""); setLoaded(true);
        if (d.missing) setMsg({ ok: false, text: "CLAUDE.md not found - saving will create it" });
      } else {
        setMsg({ ok: false, text: d.error || `HTTP ${r.status}` });
        setLoaded(true);
      }
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) }); setLoaded(true);
    }
  }, [cousin.slug]);

  React.useEffect(() => {
    if (open && !loaded) load();
  }, [open, loaded, load]);
  React.useEffect(() => { setLoaded(false); setOpen(false); }, [cousin.slug]);

  const dirty = content !== original;

  const save = async () => {
    if (busy || !dirty) return;
    setBusy(true); setMsg(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/claude-md`, { content });
      if (r.ok && d.ok) {
        setOriginal(content);
        setMsg({ ok: true, text: `saved ${d.bytes} bytes - the cousin picks it up on the next session start` });
      } else {
        setMsg({ ok: false, text: d.error || `HTTP ${r.status}` });
      }
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally { setBusy(false); }
  };

  const revert = () => { setContent(original); setMsg(null); };

  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>
        CLAUDE.md
        <button className="btn ghost" onClick={() => setOpen(v => !v)}
          style={{ marginLeft: 8, fontSize: 10, padding: "1px 8px", minHeight: 18 }}>
          {open ? "hide" : "edit"}
        </button>
      </SectionLabel>
      {open && (
        <div style={{ marginTop: 6 }}>
          {!loaded && <div style={{ fontSize: 11, color: "var(--fg-3)" }}>loading...</div>}
          {loaded && (
            <>
              <textarea value={content} onChange={e => setContent(e.target.value)}
                style={{
                  width: "100%", minHeight: 320, resize: "vertical",
                  fontFamily: "var(--mono)", fontSize: 12, lineHeight: 1.5,
                  background: "var(--bg-0)", color: "var(--fg-0)",
                  border: `1px solid ${dirty ? "var(--amber)" : "var(--line)"}`,
                  borderRadius: 3, padding: 8, boxSizing: "border-box",
                }} />
              <div style={{ display: "flex", gap: 6, marginTop: 6, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ fontSize: 10, fontFamily: "var(--mono)",
                                color: dirty ? "var(--amber)" : "var(--fg-3)" }}>
                  {dirty ? `${content.length} chars · unsaved` : `${content.length} chars · clean`}
                </span>
                <span style={{ flex: 1 }} />
                {dirty && <button className="btn" onClick={revert} disabled={busy}>revert</button>}
                <button className="btn primary" onClick={save} disabled={!dirty || busy}>
                  {busy ? "saving..." : "save"}
                </button>
              </div>
              {msg && (
                <div style={{ marginTop: 6, padding: "5px 9px", fontSize: 11, fontFamily: "var(--mono)",
                              color: msg.ok ? "var(--green)" : "var(--red)",
                              background: msg.ok ? "oklch(from var(--green) l c h / 0.08)"
                                                  : "oklch(from var(--red) l c h / 0.08)",
                              borderRadius: 3 }}>
                  {msg.text}
                </div>
              )}
              <div style={{ marginTop: 4, fontSize: 10, color: "var(--fg-3)" }}>
                The previous file is backed up to <code>data/claude-md-backups/CLAUDE-&lt;ts&gt;.md</code> before the overwrite. The cousin needs a session restart to load the new file.
              </div>
            </>
          )}
        </div>
      )}
    </>
  );
}

function LoopsEditor({ cousin }) {
  const [loops, setLoops] = React.useState([]);
  const [loaded, setLoaded] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [lastFires, setLastFires] = React.useState({});
  const [daemon, setDaemon] = React.useState(null);

  // Load full state once per cousin. Subsequent polling only updates
  // last_fires so typing into the form doesn't get wiped every 5s.
  const loadLoops = React.useCallback(async () => {
    try {
      const r = await fetch(`/api/cousins/${cousin.slug}/loops`, { cache: "no-store" });
      const d = await r.json();
      setLoops(d.loops || []);
      setLastFires(d.last_fires || {});
      setDaemon(d.daemon || null);
      setLoaded(true);
    } catch (e) { setError(String(e.message || e)); setLoaded(true); }
  }, [cousin.slug]);
  const refreshFires = React.useCallback(async () => {
    try {
      const r = await fetch(`/api/cousins/${cousin.slug}/loops`, { cache: "no-store" });
      const d = await r.json();
      setLastFires(d.last_fires || {});
      setDaemon(d.daemon || null);
    } catch (e) { /* ignore */ }
  }, [cousin.slug]);
  React.useEffect(() => {
    loadLoops();
    const id = setInterval(refreshFires, 5000);
    return () => clearInterval(id);
  }, [loadLoops, refreshFires]);

  const update = (i, patch) => setLoops(ls => ls.map((l, idx) => idx === i ? { ...l, ...patch } : l));
  const add = () => setLoops(ls => [...ls, { name: "new_loop", interval_seconds: 600, prompt: "What changed? Report a one-line status.", enabled: true }]);
  const remove = (i) => setLoops(ls => ls.filter((_, idx) => idx !== i));

  const save = async () => {
    setSaving(true); setError(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/loops`, { loops });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      loadLoops();  // re-fetch canonical state after a successful save
    } catch (e) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  };

  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>loops ({loops.length})</SectionLabel>
      {daemon && daemon.ok === false && (
        <div style={{ color: "var(--amber)", fontSize: 10, fontFamily: "var(--mono)", marginBottom: 6 }}>{daemon.message}</div>
      )}
      <div style={{ border: "1px solid var(--line)", borderRadius: 3, background: "var(--bg-0)", padding: 8 }}>
        {!loaded && <div style={{ color: "var(--fg-3)", fontSize: 11, fontFamily: "var(--mono)" }}>loading...</div>}
        {loaded && loops.length === 0 && (
          <div style={{ color: "var(--fg-3)", fontSize: 11, fontFamily: "var(--mono)" }}>no custom loops yet. the context heartbeat fires every {cousin.heartbeat}s regardless.</div>
        )}
        {loops.map((l, i) => {
          const fired = lastFires[l.name];
          const firedAgo = fired ? fmtAgo(Math.floor(Date.now() / 1000 - fired)) : "never";
          const other = l.daily_at ? `daily ${l.daily_at}` : l.cron ? `cron ${l.cron}` : "";
          return (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "100px 80px 1fr 40px 40px", gap: 6, marginBottom: 6, alignItems: "center" }}>
              <input value={l.name || ""} onChange={e => update(i, { name: e.target.value })} placeholder="name" style={inputStyle}/>
              {other
                ? <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" }} title="edit daily/cron schedules from the Loops view">{other}</span>
                : <input type="number" value={l.interval_seconds || 0} onChange={e => update(i, { interval_seconds: Number(e.target.value) })} placeholder="sec" style={inputStyle}/>}
              <input value={l.prompt || ""} onChange={e => update(i, { prompt: e.target.value })} placeholder="prompt to inject" style={inputStyle}/>
              <label style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)", display: "inline-flex", alignItems: "center", gap: 3 }}>
                <input type="checkbox" checked={l.enabled !== false} onChange={e => update(i, { enabled: e.target.checked })}/>on
              </label>
              <button className="btn danger" style={{ padding: "2px 6px", fontSize: 10 }} onClick={() => remove(i)} title={`last fired: ${firedAgo}`}>×</button>
            </div>
          );
        })}
        <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
          <button className="btn" onClick={add} style={{ fontSize: 11 }}>+ loop</button>
          <div style={{ flex: 1 }} />
          {error && <span style={{ color: "var(--red)", fontSize: 10, fontFamily: "var(--mono)" }}>{error}</span>}
          <button className="btn primary" onClick={save} disabled={saving} style={{ fontSize: 11 }}>{saving ? "saving..." : "save"}</button>
        </div>
      </div>
    </>
  );
}

const inputStyle = {
  fontFamily: "var(--mono)", fontSize: 11,
  background: "var(--bg-1)", color: "var(--fg-0)",
  border: "1px solid var(--line)", borderRadius: 2,
  padding: "3px 6px", outline: "none",
};

function SectionLabel({ children, style }) {
  return (
    <div style={{
      fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)",
      textTransform: "uppercase", letterSpacing: "0.1em",
      borderTop: "1px solid var(--line-soft)", paddingTop: 10, marginBottom: 10,
      ...(style || {}),
    }}>{children}</div>
  );
}

function FlipModal({ cousin: c, onClose, onFlash }) {
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const [typedSlug, setTypedSlug] = React.useState("");
  const [delay, setDelay] = React.useState(0); // seconds
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [progress, setProgress] = React.useState(null);

  // Subscribe to cousin-flip SSE events (re-dispatched by the shell) for live progress.
  React.useEffect(() => {
    const onEvent = (e) => {
      const detail = e.detail;
      if (!detail || detail.slug !== c.slug) return;
      setProgress(detail);
      if (detail.phase === "complete" || detail.phase === "failed") {
        // close + flash a moment after completion
        setTimeout(() => {
          onFlash(detail.phase === "complete"
            ? `${c.slug} flipped to gen ${detail.new_generation}`
            : `flip failed: ${detail.error || "see logs"}`);
          onClose();
        }, 800);
      }
    };
    window.addEventListener("fw-cousin-flip", onEvent);
    return () => window.removeEventListener("fw-cousin-flip", onEvent);
  }, [c.slug, onClose, onFlash]);

  const slugMatches = typedSlug.trim() === c.slug;
  const submit = async () => {
    if (!slugMatches || busy) return;
    setBusy(true);
    setError(null);
    try {
      const body = delay > 0 ? { delay_seconds: delay } : { confirm: true };
      const { r, d } = await apiSend("POST", `/api/cousins/${c.slug}/flip`, body);
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      if (delay > 0) {
        const mins = delay >= 60 ? `${Math.round(delay/60)}m` : `${delay}s`;
        onFlash(`${c.slug} flip scheduled in ${mins} (request #${d.request_id})`);
        onClose();
      } else {
        // running in the background; the SSE listener closes on completion
        setProgress({ phase: "started" });
      }
    } catch (e) {
      setError(String(e.message || e));
      setBusy(false);
    }
  };

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="hdr">
          <span>cousin-flip</span>
          <span style={{ color: "var(--fg-3)", marginLeft: "auto" }}>
            console / cousins / {c.slug} / flip
          </span>
        </div>
        <div className="body">
          <p style={{ color: "var(--fg-1)", fontSize: 12, lineHeight: 1.5, marginTop: 0 }}>
            Respawns <b>@{c.slug}</b> on a fresh session-id. The cousin reads a new
            boot packet and reconstructs identity. The conversation buffer is
            lost; durable state (memory, decisions, status, handoff) survives.
          </p>
          <p style={{ color: "var(--amber)", fontSize: 12, marginTop: 4 }}>
            Blast radius: high. The cousin's tmux session is killed.
            Type <code>{c.slug}</code> below to confirm.
          </p>
          <FormField label="confirm slug" hint={`Type "${c.slug}" exactly.`}>
            <input className="txt" autoFocus value={typedSlug}
                    onChange={e => setTypedSlug(e.target.value)}
                    placeholder={c.slug}
                    style={{
                      borderColor: typedSlug && !slugMatches ? "var(--red)"
                                  : slugMatches ? "var(--green)" : undefined,
                    }} />
          </FormField>
          <div style={{ marginTop: 14 }}>
            <SectionLabel>fire when</SectionLabel>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {[
                { label: "now", v: 0 },
                { label: "in 1 min", v: 60 },
                { label: "in 5 min (warns at T-5/T-1/T-30)", v: 300 },
                { label: "in 15 min", v: 900 },
              ].map(o => (
                <button key={o.v}
                          className={"btn" + (delay === o.v ? " primary" : "")}
                          onClick={() => setDelay(o.v)}>
                  {o.label}
                </button>
              ))}
            </div>
            <div style={{ fontSize: 10, color: "var(--fg-3)", marginTop: 8 }}>
              A timed flip goes through the loops daemon, which warns the cousin at
              T-5m, T-1m and T-30s so it can wrap up tool calls and write a clean handoff.
            </div>
          </div>
          {progress && (
            <div style={{ marginTop: 14, padding: 8, border: "1px solid var(--line)",
                            borderRadius: 3, fontSize: 11, fontFamily: "var(--mono)",
                            color: "var(--fg-1)" }}>
              {progress.phase}
              {progress.new_generation != null && ` · gen ${progress.new_generation}`}
              {progress.boot_packet_tokens != null && ` · ${progress.boot_packet_tokens} tok`}
              {progress.degraded_sections && progress.degraded_sections.length > 0 &&
                ` · degraded: ${progress.degraded_sections.join(",")}`}
              {progress.error && ` · error: ${progress.error}`}
            </div>
          )}
          {error && (
            <div style={{ marginTop: 14, color: "var(--red)", fontSize: 11 }}>{error}</div>
          )}
          <div style={{ display: "flex", gap: 8, marginTop: 18, justifyContent: "flex-end" }}>
            <button className="btn" onClick={onClose} disabled={busy}>
              close
            </button>
            <button className="btn primary" onClick={submit}
                     disabled={!slugMatches || busy}>
              {busy ? (delay > 0 ? "scheduling..." : "flipping...")
                     : (delay > 0 ? `schedule flip in ${delay >= 60 ? Math.round(delay/60)+"m" : delay+"s"}`
                                   : "flip now")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SpawnModal({ onClose, onSpawn }) {
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const [name, setName] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [role, setRole] = React.useState("");
  const [roleParagraph, setRoleParagraph] = React.useState("");
  const [voice, setVoice] = React.useState("");
  const [port, setPort] = React.useState("");
  const [operator, setOperator] = React.useState("");
  // The runtime fields: what the agent command's {model} / {effort}
  // placeholders render to, the heartbeat cadence and the memory
  // scope. The catalogue and every default come from the server
  // (GET /api/spawn/options, read from config/harness.toml [agent]);
  // the dialog carries no list of its own to drift from it.
  const [options, setOptions] = React.useState(null);
  const [model, setModel] = React.useState("");
  const [effort, setEffort] = React.useState("");
  const [heartbeat, setHeartbeat] = React.useState("");
  const [scope, setScope] = React.useState("");

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      const d = await apiGet("/api/spawn/options");
      if (cancelled || !d) return;
      setOptions(d);
      setModel(m => m || d.default_model || "");
      setEffort(e => e || d.default_effort || "");
      setHeartbeat(h => h || String(d.default_heartbeat || 3600));
      setScope(s => s || d.default_memory_scope || "private");
    })();
    return () => { cancelled = true; };
  }, []);
  const models = options?.models || [];
  const efforts = options?.efforts || [];
  const scopes = options?.memory_scopes || [];

  React.useEffect(() => {
    if (!slug && name) setSlug(name.toLowerCase().replace(/[^a-z0-9]/g, ""));
  }, [name]);

  // voice is required: the template refuses to render without it.
  const valid = name.trim() && slug.trim() && role.trim() && voice.trim();

  const [error, setError] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      // 1. Create the cousin (spawn.create_cousin renders the template)
      const body = { slug, name, role, voice };
      if (roleParagraph.trim()) body.role_paragraph = roleParagraph.trim();
      if (String(port).trim()) body.port = Number(port);
      if (operator.trim()) body.operator = operator.trim();
      if (model) body.model = model;
      if (effort) body.effort = effort;
      if (String(heartbeat).trim()) body.heartbeat = Number(heartbeat);
      if (scope) body.memory_scope = scope;
      const { r, d } = await apiSend("POST", "/api/cousins", body);
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      // 2. Start it so the cousin is immediately usable (create and start stay separate)
      const { r: r2, d: d2 } = await apiSend("POST", `/api/cousins/${slug}/start`);
      if (!r2.ok || !d2.ok) throw new Error("created but start failed: " + (d2.error || `HTTP ${r2.status}`));
      onSpawn({
        slug, name, role, type: "cousin", port: d.port, host: null, home: d.home,
        tmuxSession: slug, operator: operator.trim() || null,
        memoryScope: scope || "private", heartbeat: Number(heartbeat) || 3600,
        model: model || null, effort: effort || null, pid: null, uptime_seconds: null,
        flipAt: null, hidden: false, status: "running", chat: "ok", active: false,
        activity: "", lastMsgTs: 0, tokensSpent: 0,
      });
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="hdr">
          <span>spawn cousin</span>
          <span style={{ color: "var(--fg-3)", marginLeft: "auto" }}>console / cousins / new</span>
        </div>
        <div className="body">
          <div className="grid2">
            <FormField label="name" hint="Display name (e.g. Wren)">
              <input className="txt" value={name} onChange={e => setName(e.target.value)} placeholder="Wren" />
            </FormField>
            <FormField label="slug" hint="Lowercase identifier used in paths and the tmux session">
              <input className="txt" value={slug} onChange={e => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9]/g, ""))} placeholder="wren" />
            </FormField>
          </div>
          <div style={{ marginTop: 14 }} />
          <FormField label="role" hint="One line. Shown on cards and in logs.">
            <textarea className="txt" value={role} onChange={e => setRole(e.target.value)} placeholder="example cousin for this framework" />
          </FormField>
          <div style={{ marginTop: 14 }} />
          <FormField label="role paragraph (optional)" hint="A paragraph of purpose for the identity template.">
            <textarea className="txt" value={roleParagraph} onChange={e => setRoleParagraph(e.target.value)} placeholder="You are Wren, the example cousin that ships with this framework." />
          </FormField>
          <div style={{ marginTop: 14 }} />
          <FormField label="voice" hint="Required: how the cousin writes. The template refuses to render without it.">
            <textarea className="txt" value={voice} onChange={e => setVoice(e.target.value)} placeholder="Plain, short, and honest; says what it does not know." />
          </FormField>
          <div style={{ marginTop: 14 }} />
          <div className="grid2">
            <FormField label="chat port (optional)" hint="Blank picks the next free one.">
              <input className="txt" type="number" value={port} onChange={e => setPort(e.target.value)} placeholder="auto" />
            </FormField>
            <FormField label="operator (optional)" hint="The person this cousin answers to; blank is a real state.">
              <input className="txt" value={operator} onChange={e => setOperator(e.target.value)} placeholder="none" />
            </FormField>
          </div>
          <div style={{ marginTop: 14 }} />
          <div className="grid2">
            <FormField label="model" hint="Rendered into the agent command's {model} placeholder.">
              <select className="sel" value={model} onChange={e => setModel(e.target.value)} disabled={!options}>
                {!options && <option value="">loading...</option>}
                {models.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </FormField>
            <FormField label="effort" hint="Rendered into the {effort} placeholder.">
              <select className="sel" value={effort} onChange={e => setEffort(e.target.value)} disabled={!options}>
                {!options && <option value="">loading...</option>}
                {efforts.map(l => <option key={l} value={l}>{l}</option>)}
              </select>
            </FormField>
          </div>
          <div style={{ marginTop: 14 }} />
          <div className="grid2">
            <FormField label="heartbeat · seconds" hint="The context heartbeat cadence ([heartbeat] context_beat_seconds).">
              <input className="txt" type="number" min="1" value={heartbeat} onChange={e => setHeartbeat(e.target.value)} placeholder="3600" />
            </FormField>
            <FormField label="memory scope" hint="[memory] scope: private, shared with the fleet, or both.">
              <div className="radio-row">
                {scopes.map(s => (
                  <button key={s} type="button" className={scope === s ? "sel" : ""} onClick={() => setScope(s)}>{s}</button>
                ))}
              </div>
            </FormField>
          </div>
        </div>
        {error && (
          <div style={{ padding: "8px 16px", color: "var(--red)", fontFamily: "var(--mono)", fontSize: 11, borderTop: "1px solid var(--line)" }}>
            · {error}
          </div>
        )}
        <div className="foot">
          <button className="btn ghost" onClick={onClose}>cancel</button>
          <button className="btn primary" disabled={!valid || busy} onClick={submit}>
            {I.plus} {busy ? "creating..." : `create cousin ${slug || "-"}`}
          </button>
        </div>
      </div>
    </div>
  );
}

function FormField({ label, hint, children }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

Object.assign(window, { CousinsView, CousinCard, Inspector, RoleEditor, ClaudeMdEditor, LoopsEditor, FlipModal, SpawnModal, SectionLabel, FormField });
