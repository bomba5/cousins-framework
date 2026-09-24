// Cousins view: cards, inspector drawer, editors, spawn / dismiss / flip
function CousinsView({ cousins, setCousins, setActiveCousin }) {
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
      if (r.ok && d.ok && d.status === "closing") {
        // A clean stop: the cousin writes its handoff and memory first
        // (up to five minutes); the row turns stopped when it is done.
        flash(`stopping ${c.slug}: saving handoff and memory first`, 5000);
      } else if (r.ok && d.ok) {
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
  const remoteCount = visibleCousins.filter(c => c.remote).length;

  // Remote cousins (hive nodes on other machines): revoke the token,
  // then forget the row. There is no start/stop/pane: the console does
  // not run them, it is only their queen.
  const actRemote = async (c, action) => {
    try {
      if (action === "revoke") {
        if (!window.confirm(`Revoke @${c.slug}? Its hive token stops working at once (every queen call answers 401). This cannot be undone; a rebuilt archive gets a new token.`)) return;
        const { r, d } = await apiSend("POST", `/api/hive/nodes/${c.slug}/revoke`);
        if (r.ok && d.ok) {
          flash(`revoked @${c.slug}`);
          setCousins(cs => cs.map(x => x.slug === c.slug ? { ...x, revoked: true, remoteState: "revoked", online: false, status: "stopped" } : x));
        } else flash("revoke failed: " + (d.error || `HTTP ${r.status}`), 4000);
        return;
      }
      if (action === "forget") {
        const { r, d } = await apiSend("DELETE", `/api/hive/nodes/${c.slug}`);
        if (r.ok && d.ok) {
          flash(`forgot @${c.slug}`);
          setCousins(cs => cs.filter(x => x.slug !== c.slug));
        } else flash("forget failed: " + (d.error || `HTTP ${r.status}`), 4000);
      }
    } catch (e) {
      flash("error: " + (e.message || e), 4000);
    }
  };

  return (
    <div className="wrap-pad">
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
        <div className="eyebrow">
          {visibleCousins.length} cousins · {visibleCousins.filter(c => c.status === "running").length} running
          {remoteCount > 0 && <span style={{ marginLeft: 8 }}>· {remoteCount} remote</span>}
          {hiddenCount > 0 && !showHidden && <span style={{ marginLeft: 8, color: "var(--fg-3)" }}>· {hiddenCount} hidden</span>}
        </div>
        <div style={{ flex: 1 }} />
        <button className="btn primary" onClick={() => setSpawning(true)}>
          {I.plus} spawn cousin
        </button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 14 }}>
        {visibleCousins.map(c => c.remote ? (
          <RemoteCousinCard
            key={c.slug} c={c}
            onAct={actRemote}
            onChat={() => setActiveCousin(c.slug)}
          />
        ) : (
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
          cousin={cousins.find(c => c.slug === selected && !c.remote)}
          onClose={() => setSelected(null)}
          onAct={act}
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
  const st = window.fleetState ? fleetState(c)
    : { word: c.status === "running" ? (c.active ? "working" : "idle") : "stopped", tone: c.status === "running" ? "green" : "gray", pulse: !!c.active };
  return (
    <div className="cousin-card" onClick={onClick}>
      <div className="name-row">
        <Led state={c.status === "running" ? "running" : "stopped"} pulse={c.status === "running"} />
        <div className="name">{c.name}</div>
        <div className="slug">@{c.slug}</div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          {isWorker && <Pill tone="gray">worker</Pill>}
          {/* the state in words and colour, the same reading as the overview */}
          <Pill tone={st.tone}>{st.word}</Pill>
        </div>
      </div>
      <div className="role">{c.role}</div>
      {c.attention && (
        <div className="card-attention"
             title="the pane shows text config/harness.toml attention_patterns lists: the agent is waiting on a person">
          needs attention · the pane shows "{c.attention}"
        </div>
      )}
      <div style={{ margin: "4px 0" }}>
        <HeartbeatGraph state={c.status === "running" ? (c.active ? "active" : "idle") : "stopped"} width={240} height={22} />
      </div>
      <div className="stats">
        <div>chat · <b>{c.chat === "ok" ? `:${c.port}` : c.chat}</b></div>
        {c.runner && <div>runner · <b>{c.runner.alive ? (c.runner.state || "no state yet") : "(not running)"}</b>{c.runner.unsupported && c.runner.unsupported.length ? ` · unsupported: ${c.runner.unsupported.join(", ")}` : ""}</div>}
        <div>scope · <b>{c.memoryScope}</b></div>
        <div>operator · <b>{c.operator || "-"}</b></div>
        <div>beat · <b>{c.heartbeat}s</b></div>
        <div>flip at · <b>{c.flipAt || "-"}</b></div>
        <div>host · <b>{c.host || "local"}</b></div>
        {/* model is what the next start renders; pid and uptime are the
            agent process tmux reports, "-" when there is none to ask */}
        <div>model · <b>{c.model || "-"}</b></div>
        <div>pid · <b>{c.pid ?? "-"}</b></div>
        <div>uptime · <b>{c.uptime_seconds == null ? "-" : fmtDuration(c.uptime_seconds)}</b></div>
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

// A remote cousin: a hive node on another machine that checks in with
// this console (its queen). The card shows where it is and when it was
// last heard from; online means a checkin within 2.5 checkin periods.
// No start/stop/restart/pane: the console does not run it.
function RemoteCousinCard({ c, onAct, onChat }) {
  const state = c.remoteState || (c.online ? "online" : "offline");
  const tone = { online: "green", offline: "gray", pending: "amber", revoked: "red" }[state] || "gray";
  const label = state === "pending" ? "built, not checked in" : state;
  const seen = c.lastSeen ? fmtAgo(Date.now() / 1000 - c.lastSeen) : "never";
  return (
    <div className="cousin-card remote">
      <div className="name-row">
        <Led state={c.online ? "running" : "stopped"} pulse={c.online} />
        <div className="name">{c.name}</div>
        <div className="slug">@{c.slug}</div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          <Pill tone="cyan">remote</Pill>
          <Pill tone={tone}>{label}</Pill>
        </div>
      </div>
      <div className="role">{c.role}</div>
      <div style={{ margin: "4px 0" }}>
        <HeartbeatGraph state={c.online ? "idle" : "stopped"} width={240} height={22} />
      </div>
      <div className="stats">
        <div>host · <b>{c.host && c.port ? `${c.host}:${c.port}` : "-"}</b></div>
        <div>last seen · <b>{seen}</b></div>
        <div>runtime · <b>{c.version || "-"}</b></div>
      </div>
      <div className="actions" onClick={e => e.stopPropagation()}>
        {c.revoked ? (
          <button className="btn" onClick={() => onAct(c, "forget")} title="remove this revoked node from the console">forget</button>
        ) : (
          <button className="btn danger" onClick={() => onAct(c, "revoke")} title="revoke the node's hive token">revoke</button>
        )}
        <div style={{ flex: 1 }} />
        {c.online && !c.revoked && (
          <button className="btn" onClick={() => onChat(c.slug)}>open chat →</button>
        )}
      </div>
    </div>
  );
}

function Inspector({ cousin: c, onClose, onAct }) {
  // The identity editors' catalogue and limits (scopes, heartbeat
  // bounds, operator length) come from the server, like the spawn
  // dialog's, so the two cannot drift from the routes' validation.
  const [options, setOptions] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    apiGet("/api/spawn/options").then(d => { if (!cancelled && d) setOptions(d); });
    return () => { cancelled = true; };
  }, []);
  React.useEffect(() => {
    if (!c) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [c, onClose]);
  const [filesOpen, setFilesOpen] = React.useState(false);
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
          <dt>operator</dt><dd><IdentityField cousin={c} field="operator" options={options} /></dd>
          <dt>scope</dt><dd><IdentityField cousin={c} field="memory_scope" options={options} /></dd>
          <dt>tmux</dt><dd>{c.tmuxSession}{c.host ? ` @ ${c.host}` : ""}</dd>
          <dt>chat</dt><dd>{c.port ? `:${c.port} · ${c.chat}` : "none"}</dd>
          <dt>heartbeat</dt><dd><IdentityField cousin={c} field="heartbeat" options={options} /></dd>
          <dt>model</dt><dd><IdentityField cousin={c} field="model" options={options} /></dd>
          <dt>effort</dt><dd><IdentityField cousin={c} field="effort" options={options} /></dd>
          <dt>auth</dt><dd><AuthField cousin={c} /></dd>
          <dt>pid</dt><dd>{c.pid ?? <span style={{ color: "var(--fg-3)" }}>-</span>}</dd>
          <dt>uptime</dt><dd>{c.uptime_seconds == null ? <span style={{ color: "var(--fg-3)" }}>-</span> : fmtDuration(c.uptime_seconds)}</dd>
          <dt>flip at</dt><dd>{c.flipAt || <span style={{ color: "var(--fg-3)" }}>-</span>}</dd>
          <dt>activity</dt><dd>{c.activity || <span style={{ color: "var(--fg-3)" }}>-</span>}</dd>
        </dl>

        <SectionLabel style={{ marginTop: 20 }}>tokens · today</SectionLabel>
        <div style={{ fontFamily: "var(--mono)", fontSize: 12, color: "var(--fg-1)", marginTop: 6 }}>
          {fmtTokens(c.tokensSpent)} spent
        </div>

        <SectionLabel style={{ marginTop: 20 }}>files</SectionLabel>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6 }}>
          <button className="btn" data-open-files onClick={() => setFilesOpen(true)}>browse home</button>
          <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" }}>read-only tree of the home; .secrets/ never shown</span>
        </div>
        {filesOpen && <CousinFilesModal slug={c.slug} onClose={() => setFilesOpen(false)} />}

        <PeerMessagePanel cousin={c} />

        <TelegramPanel cousin={c} />

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
          <HideCousinButton cousin={c} />
        </div>
      </div>
    </div>
  );
}

// "36000s (10h)": the raw seconds cousin.toml holds, and a readable form.
function fmtBeat(sec) {
  if (sec == null || sec === "") return "-";
  const n = Number(sec);
  if (!Number.isFinite(n)) return String(sec);
  return `${n}s (${fmtDuration(n).replace(/ 0[smh]$/, "")})`;
}

// The keys the inspector edits in place. Each persists through its own
// route (POST /api/cousins/<slug>/operator, /memory-scope, /heartbeat,
// /model, /effort); the route says whether a restart applies it, and
// the hint follows the chat header's effort select: "restart to apply".
// A select's choices come from /api/spawn/options, so the catalogue is
// the install's (config/harness.toml), never one written here.
const IDENTITY_FIELDS = {
  operator:     { url: slug => `/api/cousins/${slug}/operator`,     row: "operator",    kind: "text",
                  title: "[operator] name" },
  memory_scope: { url: slug => `/api/cousins/${slug}/memory-scope`, row: "memoryScope", kind: "select",
                  title: "[memory] scope", choices: o => o?.memory_scopes },
  heartbeat:    { url: slug => `/api/cousins/${slug}/heartbeat`,    row: "heartbeat",   kind: "seconds",
                  title: "[heartbeat] context_beat_seconds" },
  model:        { url: slug => `/api/cousins/${slug}/model`,        row: "model",       kind: "select",
                  title: "[runtime] model", choices: o => o?.models },
  effort:       { url: slug => `/api/cousins/${slug}/effort`,       row: "effort",      kind: "select",
                  title: "[runtime] effort", choices: o => o?.efforts },
};

function IdentityField({ cousin, field, options }) {
  const spec = IDENTITY_FIELDS[field];
  const current = cousin[spec.row];
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [hint, setHint] = React.useState(null);
  // A saved value shows at once; the fleet refresh the route emits
  // replaces it with the row's own, which then wins.
  const [saved, setSaved] = React.useState(null);
  React.useEffect(() => { setSaved(null); }, [current]);
  React.useEffect(() => { setEditing(false); setErr(null); setHint(null); setSaved(null); }, [cousin.slug]);
  const shown = saved != null ? saved : current;

  const choices = (spec.choices && spec.choices(options)) || [];
  const bounds = options?.heartbeat_bounds || [60, 2592000];
  const maxChars = options?.operator_max_chars || 64;

  const start = () => {
    setDraft(shown == null ? "" : String(shown));
    setErr(null); setHint(null); setEditing(true);
  };
  const cancel = () => { setEditing(false); setErr(null); };

  // Client-side check mirrors the route; the route stays the authority.
  const problem = (() => {
    if (spec.kind === "text") {
      if (!draft.trim()) return "a name is required";
      if (draft !== draft.trim()) return "no leading or trailing spaces";
      if (draft.length > maxChars) return `at most ${maxChars} characters`;
    }
    if (spec.kind === "seconds") {
      const n = Number(draft);
      if (!/^\d+$/.test(draft.trim()) || n < bounds[0] || n > bounds[1])
        return `whole seconds from ${bounds[0]} to ${bounds[1]}`;
    }
    if (spec.kind === "select" && !choices.includes(draft)) return "pick one";
    return null;
  })();

  const save = async () => {
    if (busy || problem) return;
    const value = spec.kind === "seconds" ? Number(draft.trim()) : draft;
    setBusy(true); setErr(null);
    try {
      const { r, d } = await apiSend("POST", spec.url(cousin.slug), { [field]: value });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setSaved(d[field]);
      setEditing(false);
      setHint(d.restart_required ? "restart to apply" : null);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  if (!editing) {
    const text = spec.kind === "seconds" ? fmtBeat(shown)
      : (shown || <span style={{ color: "var(--fg-3)" }}>none</span>);
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span>{text}</span>
        <button className="btn ghost" onClick={start} style={small}
          title={`edit cousin.toml ${spec.title}`}>edit</button>
        {hint && <span style={{ color: "var(--fg-2)", fontSize: 11 }}>{hint}</span>}
      </span>
    );
  }
  const onKey = (e) => {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.stopPropagation(); cancel(); }
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        {spec.kind === "select" ? (
          <select className="sel" value={draft} onChange={e => setDraft(e.target.value)}
                  autoFocus disabled={!choices.length} style={{ flex: 1, minWidth: 0 }}>
            {!choices.includes(draft) && <option value={draft}>{draft || "..."}</option>}
            {choices.map(ch => <option key={ch} value={ch}>{ch}</option>)}
          </select>
        ) : (
          <input className="txt" value={draft} onChange={e => setDraft(e.target.value)}
                 onKeyDown={onKey} autoFocus style={{ flex: 1 }}
                 inputMode={spec.kind === "seconds" ? "numeric" : undefined}
                 maxLength={spec.kind === "text" ? maxChars : undefined} />
        )}
        <button className="btn" onClick={cancel} disabled={busy} style={small}>cancel</button>
        <button className="btn primary" onClick={save} disabled={busy || !!problem} style={small}>
          {busy ? "saving..." : "save"}
        </button>
      </div>
      {spec.kind === "seconds" && !problem && (
        <span style={{ fontSize: 10, color: "var(--fg-3)" }}>{fmtBeat(draft.trim())}</span>
      )}
      {problem && draft !== "" && <span style={{ fontSize: 10, color: "var(--fg-3)" }}>{problem}</span>}
      {err && <div style={{ padding: "5px 9px", fontSize: 11, fontFamily: "var(--mono)",
                            color: "var(--red)", background: "oklch(from var(--red) l c h / 0.08)", borderRadius: 3 }}>{err}</div>}
    </div>
  );
}

// How the cousin's agent authenticates (GET/POST /api/cousins/<slug>/auth).
// The mode names come from the server, never from this file. The key is
// pasted into a password field, sent once to POST .../auth/key and
// dropped from state; the page only ever shows "set" and the last four.
function AuthField({ cousin }) {
  const [st, setSt] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [needForce, setNeedForce] = React.useState(null);
  const [keyOpen, setKeyOpen] = React.useState(false);
  const [keyDraft, setKeyDraft] = React.useState("");
  const [note, setNote] = React.useState(null);
  const load = React.useCallback(async () => {
    const d = await apiGet(`/api/cousins/${cousin.slug}/auth`);
    if (d) setSt(d);
  }, [cousin.slug]);
  React.useEffect(() => {
    setSt(null); setErr(null); setNeedForce(null); setKeyOpen(false); setKeyDraft(""); setNote(null);
    load();
  }, [cousin.slug, load]);

  const switchTo = async (mode, force) => {
    if (busy || !st) return;
    setBusy(true); setErr(null); setNote(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/auth`, { mode, force: !!force });
      if (r.status === 409 && d.busy) { setNeedForce(mode); setErr(d.error); return; }
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setNeedForce(null);
      if (d.auth) setSt(d.auth);
      setNote(d.restarted ? "restarted on the same session" : "applies at the next start");
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const sendKey = async () => {
    if (busy || !keyDraft.trim()) return;
    const key = keyDraft;
    setKeyDraft("");
    setBusy(true); setErr(null); setNote(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/auth/key`, { key });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setSt(d);
      setKeyOpen(false);
      setNote("key saved");
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (!st) return <span style={{ color: "var(--fg-3)" }}>{cousin.auth || "-"}</span>;
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  const keyText = st.key?.set
    ? `key set${st.key.last4 ? ` (ends ${st.key.last4})` : ""}`
    : "key not set";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <select className="sel" value={st.mode} disabled={busy}
                title="restarts a running agent on the same session"
                onChange={e => switchTo(e.target.value, false)}>
          {(st.modes || []).map(m => <option key={m} value={m}>{m}</option>)}
        </select>
        {st.configured && (
          <span style={{ color: st.key?.set ? "var(--fg-1)" : "var(--fg-3)", fontSize: 11 }}>{keyText}</span>
        )}
        {st.configured && !keyOpen && (
          <button className="btn ghost" style={small} onClick={() => { setKeyOpen(true); setErr(null); }}>
            {st.key?.set ? "replace key" : "set key"}
          </button>
        )}
        {needForce && (
          <button className="btn danger" style={small} disabled={busy}
                  onClick={() => switchTo(needForce, true)}>restart anyway</button>
        )}
      </div>
      {keyOpen && (
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input className="txt" type="password" autoComplete="off" spellCheck={false}
                 value={keyDraft} placeholder="paste the key" autoFocus style={{ flex: 1 }}
                 onChange={e => setKeyDraft(e.target.value)}
                 onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); sendKey(); }
                                   if (e.key === "Escape") { e.stopPropagation(); setKeyOpen(false); setKeyDraft(""); } }} />
          <button className="btn" style={small} disabled={busy} onClick={() => { setKeyOpen(false); setKeyDraft(""); }}>cancel</button>
          <button className="btn primary" style={small} disabled={busy || !keyDraft.trim()} onClick={sendKey}>save</button>
        </div>
      )}
      {!st.configured && (
        <span style={{ fontSize: 10, color: "var(--fg-3)" }}>key mode not configured (config/harness.toml [auth.api_key])</span>
      )}
      {st.key?.error && <span style={{ fontSize: 10, color: "var(--fg-3)" }}>{st.key.error}</span>}
      {note && <span style={{ fontSize: 11, color: "var(--fg-2)" }}>{note}</span>}
      {err && <div style={{ padding: "5px 9px", fontSize: 11, fontFamily: "var(--mono)",
                            color: "var(--red)", background: "oklch(from var(--red) l c h / 0.08)", borderRadius: 3 }}>{err}</div>}
    </div>
  );
}

// The cousin's Telegram bridge (GET/POST /api/cousins/<slug>/telegram).
// The token is write-only: it lives in the password box until the save
// sends it, then the box is cleared and the page only ever shows
// "token set". Operators are replaced as a whole list on every change;
// people who pressed Start on the bot wait in `pending` until added.
function fmtTgAt(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  if (isNaN(t.getTime())) return String(iso);
  const hm = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return t.toDateString() === new Date().toDateString()
    ? hm : `${t.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`;
}

function TelegramPanel({ cousin }) {
  const [st, setSt] = React.useState(null);
  const [loadErr, setLoadErr] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const [bot, setBot] = React.useState(null);
  const [tokenDraft, setTokenDraft] = React.useState("");
  const [newId, setNewId] = React.useState("");
  const [newName, setNewName] = React.useState("");
  const url = `/api/cousins/${cousin.slug}/telegram`;

  const load = React.useCallback(async () => {
    const d = await apiGet(url);
    if (d) { setSt(d); setLoadErr(false); } else setLoadErr(true);
  }, [url]);
  React.useEffect(() => {
    setSt(null); setLoadErr(false); setErr(null); setNote(null); setBot(null);
    setTokenDraft(""); setNewId(""); setNewName("");
    load();
    // Poll so a person who just pressed Start shows up without a click.
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [cousin.slug, load]);

  // One POST; the answer carries the whole status, which replaces ours.
  const post = async (path, body) => {
    setBusy(true); setErr(null); setNote(null);
    try {
      const { r, d } = await apiSend("POST", url + path, body);
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      if ("enabled" in d) setSt(d);
      return d;
    } catch (e) {
      setErr(String(e.message || e));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const applyCheck = (c) => {
    if (!c) return;
    if (c.ok) { setBot(c.bot || ""); setNote(c.bot ? `token works: @${c.bot}` : "token works"); }
    else { setBot(null); setErr("check failed: " + (c.error || "unknown error")); }
  };

  const saveToken = async () => {
    if (busy || !tokenDraft.trim()) return;
    const token = tokenDraft;
    setTokenDraft("");
    const d = await post("/token", { token });
    if (d) applyCheck(d.check);
  };

  const check = async () => {
    const d = await post("/check");
    applyCheck(d);
  };

  const setEnabled = async (enabled) => {
    const d = await post("/enabled", { enabled });
    if (d && d.bridge) setNote(`bridge: ${d.bridge}`);
  };

  const saveOperators = async (ops, msg) => {
    const d = await post("/operators", { operators: ops.map(o => ({ user_id: o.user_id, name: o.name })) });
    if (d) setNote(msg);
    return !!d;
  };

  const ops = st?.operators || [];
  const pending = st?.pending || [];
  const idText = newId.trim();
  const idProblem = idText === "" ? null
    : !/^\d+$/.test(idText) || Number(idText) <= 0 ? "a numeric user id"
    : ops.some(o => String(o.user_id) === idText) ? "already an operator" : null;
  const nameProblem = newName.length > 64 ? "at most 64 characters" : null;

  const addOperator = async () => {
    if (busy || !idText || idProblem || nameProblem) return;
    const ok = await saveOperators([...ops, { user_id: Number(idText), name: newName.trim() }],
                                   `added ${newName.trim() || idText}`);
    if (ok) { setNewId(""); setNewName(""); }
  };
  const removeOperator = (uid) => {
    const gone = ops.find(o => o.user_id === uid);
    saveOperators(ops.filter(o => o.user_id !== uid), `removed ${gone?.name || uid}`);
  };
  const addPending = (p) => {
    const name = (p.first_name || p.username || "").slice(0, 64);
    saveOperators([...ops, { user_id: p.user_id, name }], `added ${name || p.user_id}`);
  };

  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  const mono = { fontFamily: "var(--mono)", fontSize: 11 };
  const hintStyle = { fontSize: 10, color: "var(--fg-3)" };

  if (!st) {
    return (
      <>
        <SectionLabel style={{ marginTop: 20 }}>telegram</SectionLabel>
        <div style={{ ...mono, color: "var(--fg-3)" }}>{loadErr ? "telegram status unavailable" : "loading..."}</div>
      </>
    );
  }

  // `ready` says "disabled" first while the bridge is off, so the enable
  // gate reads the token and operators directly.
  const blocker = !st.token_set ? "save a token first"
    : ops.length === 0 ? "add an operator first" : null;
  const empty = !st.token_set && ops.length === 0 && pending.length === 0;

  return (
    <>
      <SectionLabel style={{ marginTop: 20 }}>telegram</SectionLabel>
      <div data-telegram-panel style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ ...mono, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <Pill tone={st.enabled ? "green" : "gray"}>{st.enabled ? "enabled" : "disabled"}</Pill>
          <Pill tone={st.running ? "green" : "gray"}>{st.running ? "bridge running" : "bridge not running"}</Pill>
          <span style={{ color: st.token_set ? "var(--fg-1)" : "var(--fg-3)" }}>{st.token_set ? "token set" : "no token"}</span>
          {bot && <span style={{ color: "var(--fg-1)" }}>@{bot}</span>}
          <button className="btn ghost" style={small} disabled={busy || !st.token_set} onClick={check}
                  title="ask Telegram (getMe) whether the stored token works">check</button>
        </div>
        {st.ready && st.ready !== "disabled" && (
          <div style={{ ...mono, fontSize: 10, color: "var(--amber)" }}>{st.ready}</div>
        )}

        {empty && (
          <div style={{ ...hintStyle, lineHeight: 1.6 }}>
            Setup: 1) create the bot with @BotFather, 2) paste the token below,
            3) open the bot in Telegram and press Start, 4) add yourself from
            "waiting to be added", 5) enable.
          </div>
        )}

        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          {st.enabled ? (
            <button className="btn" disabled={busy} onClick={() => setEnabled(false)}>disable</button>
          ) : (
            <button className="btn primary" disabled={busy || !!blocker} onClick={() => setEnabled(true)}>enable</button>
          )}
          {!st.enabled && blocker && <span style={hintStyle}>{blocker}</span>}
          {st.enabled && !st.running && cousin.status !== "running" && (
            <span style={hintStyle}>starts with the cousin</span>
          )}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <input className="txt" type="password" autoComplete="off" spellCheck={false}
                   value={tokenDraft} placeholder={st.token_set ? "paste a new bot token" : "paste the bot token"}
                   style={{ flex: "1 1 180px", minWidth: 0 }}
                   onChange={e => setTokenDraft(e.target.value)}
                   onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); saveToken(); }
                                     if (e.key === "Escape") { e.stopPropagation(); setTokenDraft(""); } }} />
            <button className="btn primary" style={small} disabled={busy || !tokenDraft.trim()} onClick={saveToken}>
              save token
            </button>
          </div>
          <span style={hintStyle}>From @BotFather: /newbot or /mybots &gt; API Token. Stored on the server only (0600); never shown again.</span>
        </div>

        <div>
          <div style={{ ...mono, fontSize: 10, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>
            operators ({ops.length})
          </div>
          <div style={{ border: "1px solid var(--line)", borderRadius: 3, background: "var(--bg-0)", padding: 8,
                        display: "flex", flexDirection: "column", gap: 6 }}>
            {ops.length === 0 && (
              <div style={{ ...mono, color: "var(--fg-3)" }}>nobody yet: the bot answers no one.</div>
            )}
            {ops.map(o => (
              <div key={o.user_id} style={{ ...mono, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ color: "var(--fg-0)" }}>{o.name || <span style={{ color: "var(--fg-3)" }}>(no name)</span>}</span>
                <span style={{ color: "var(--fg-3)" }}>{o.user_id}</span>
                <span style={{ flex: 1 }} />
                <button className="btn danger" style={small} disabled={busy}
                        onClick={() => removeOperator(o.user_id)} title="remove from the allowed list">remove</button>
              </div>
            ))}
            <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
              <input className="txt" value={newId} placeholder="user id" inputMode="numeric"
                     style={{ flex: "1 1 90px", minWidth: 0 }} onChange={e => setNewId(e.target.value)}
                     onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addOperator(); } }} />
              <input className="txt" value={newName} placeholder="name" maxLength={64}
                     style={{ flex: "2 1 120px", minWidth: 0 }} onChange={e => setNewName(e.target.value)}
                     onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addOperator(); } }} />
              <button className="btn" style={small} disabled={busy || !idText || !!idProblem || !!nameProblem}
                      onClick={addOperator}>add</button>
            </div>
            {(idProblem || nameProblem) && <span style={hintStyle}>{idProblem || nameProblem}</span>}
          </div>
        </div>

        <div>
          <div style={{ ...mono, fontSize: 10, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>
            waiting to be added ({pending.length})
          </div>
          {pending.length === 0 ? (
            <div style={{ ...mono, color: "var(--fg-3)" }}>no one waiting.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {pending.map(p => (
                <div key={p.user_id} style={{ ...mono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", color: "var(--fg-1)" }}>
                  {p.username && <span>@{p.username}</span>}
                  {p.first_name && <span>{p.username ? `(${p.first_name})` : p.first_name}</span>}
                  <span style={{ color: "var(--fg-3)" }}>{p.user_id}</span>
                  <span style={{ color: "var(--fg-3)" }}>pressed Start {fmtTgAt(p.at)}</span>
                  <span style={{ flex: 1 }} />
                  <button className="btn" style={small} disabled={busy} onClick={() => addPending(p)}
                          title="append to the operators">add</button>
                </div>
              ))}
            </div>
          )}
          <div style={{ ...hintStyle, marginTop: 4 }}>A person presses Start on the bot; they appear here.</div>
        </div>

        {note && <span style={{ fontSize: 11, color: "var(--fg-2)" }}>{note}</span>}
        {err && <div style={{ padding: "5px 9px", fontSize: 11, fontFamily: "var(--mono)",
                              color: "var(--red)", background: "oklch(from var(--red) l c h / 0.08)", borderRadius: 3 }}>{err}</div>}
        <div>
          <button className="btn ghost" style={small} disabled={busy} onClick={load}>refresh</button>
        </div>
      </div>
    </>
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
      const others = all.filter(c => c.slug !== cousin.slug && c.type !== "worker" && !c.remote);
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
      <textarea className="txt code" value={draft} onChange={e => setDraft(e.target.value)}
        autoFocus style={{ minHeight: 120 }} />
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
              <textarea className="txt code" value={content} onChange={e => setContent(e.target.value)}
                style={{ minHeight: 320 }} />
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
  fontFamily: "var(--sans)", fontSize: 12,
  background: "var(--bg-0)", color: "var(--fg-0)",
  border: "1px solid var(--line)", borderRadius: 6,
  padding: "4px 8px", outline: "none",
};

function SectionLabel({ children, style }) {
  return (
    <div className="eyebrow" style={{
      borderTop: "1px solid var(--hair)", paddingTop: 14, marginBottom: 10,
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

  // Where the cousin runs. "Remote" is offered only when the hive is on
  // (config/hive.toml); it builds a node archive instead of a local home.
  const [hive, setHive] = React.useState(null);
  const [mode, setMode] = React.useState("local");
  React.useEffect(() => {
    let cancelled = false;
    apiGet("/api/hive").then(d => { if (!cancelled && d) setHive(d); });
    return () => { cancelled = true; };
  }, []);

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
        {hive?.enabled && (
          <div style={{ padding: "12px 18px 0" }}>
            <div className="radio-row">
              <button type="button" className={mode === "local" ? "sel" : ""} onClick={() => setMode("local")}>This machine</button>
              <button type="button" className={mode === "remote" ? "sel" : ""} onClick={() => setMode("remote")}>Remote (another machine)</button>
            </div>
          </div>
        )}
        {mode === "remote" && hive?.enabled ? (
          <RemoteSpawnForm hive={hive} onClose={onClose} />
        ) : (<>
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
        </>)}
      </div>
    </div>
  );
}

// Copy a command: the Clipboard API where the page is a secure context,
// else a hidden textarea and execCommand (a console on plain http over
// the LAN is not a secure context).
function CopyButton({ text }) {
  const [done, setDone] = React.useState(false);
  const copy = async () => {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (_e) { ok = false; }
    if (!ok) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch (_e) { ok = false; }
      document.body.removeChild(ta);
    }
    setDone(ok ? "copied" : "select and copy by hand");
    setTimeout(() => setDone(false), 1800);
  };
  return <button className="btn" onClick={copy} style={{ fontSize: 11 }}>{done || "copy"}</button>;
}

// Build a remote cousin: POST /api/hive/nodes mints the node's token in
// this console's queen store and builds the archive; the answer is a
// one-time download URL (15 minutes or first download) and the install
// commands. Nothing is pushed anywhere: the operator runs the command on
// the other machine. The card appears as "built, not checked in" and
// goes online at the node's first checkin.
function RemoteSpawnForm({ hive, onClose }) {
  const [name, setName] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [role, setRole] = React.useState("");
  const [port, setPort] = React.useState(String(hive.default_port || 8210));
  const [brain, setBrain] = React.useState("placeholder");
  const [agentCmd, setAgentCmd] = React.useState("");
  const [homeChat, setHomeChat] = React.useState(false);
  const [reachable, setReachable] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [built, setBuilt] = React.useState(null);

  React.useEffect(() => {
    if (!slug && name) setSlug(name.toLowerCase().replace(/[^a-z0-9]/g, ""));
  }, [name]);

  const valid = slug.trim() && name.trim() && role.trim()
    && (brain === "placeholder" || agentCmd.trim());

  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true); setError(null);
    try {
      const body = { slug, name: name.trim(), role: role.trim(), brain,
                     home_chat: homeChat, reachable };
      if (String(port).trim()) body.port = Number(port);
      if (brain === "agent") body.agent_cmd = agentCmd.trim();
      const { r, d } = await apiSend("POST", "/api/hive/nodes", body);
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setBuilt(d);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (built) {
    const expires = new Date(built.expires_at * 1000).toLocaleTimeString();
    return (
      <>
        <div className="body">
          <p style={{ color: "var(--fg-1)", fontSize: 12, lineHeight: 1.5, marginTop: 0 }}>
            Built <b>@{built.slug}</b>. On the other machine (python3 and outbound
            network to <code>{hive.public_url}</code> are all it needs), run one of:
          </p>
          <SectionLabel>download and install</SectionLabel>
          <div className="copy-row"><code>{built.curl}</code><CopyButton text={built.curl} /></div>
          <SectionLabel style={{ marginTop: 14 }}>install an archive you already copied over</SectionLabel>
          <div className="copy-row"><code>{built.install}</code><CopyButton text={built.install} /></div>
          <p style={{ color: "var(--amber)", fontSize: 12, marginTop: 14 }}>
            The archive carries the node's bearer token. The link works once and
            expires at {expires}; after that, build again (the slug keeps its token).
          </p>
          <p style={{ color: "var(--fg-3)", fontSize: 11 }}>
            The card shows "built, not checked in" until the node's first checkin.
          </p>
        </div>
        <div className="foot">
          <button className="btn primary" onClick={onClose}>done</button>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="body">
        <div className="grid2">
          <FormField label="name" hint="Display name (e.g. Kestrel)">
            <input className="txt" value={name} onChange={e => setName(e.target.value)} placeholder="Kestrel" />
          </FormField>
          <FormField label="slug" hint="The node's identity on the queen; its token is minted for it">
            <input className="txt" value={slug} onChange={e => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""))} placeholder="kestrel" />
          </FormField>
        </div>
        <div style={{ marginTop: 14 }} />
        <FormField label="role" hint="One line: rendered into the node's CLAUDE.md and shown on its card.">
          <textarea className="txt" value={role} onChange={e => setRole(e.target.value)} placeholder="watches the greenhouse" />
        </FormField>
        <div style={{ marginTop: 14 }} />
        <div className="grid2">
          <FormField label="node port" hint="The node's own chat port on its machine.">
            <input className="txt" type="number" value={port} onChange={e => setPort(e.target.value)} placeholder="8210" />
          </FormField>
          <FormField label="brain" hint="Placeholder greets and echoes; an agent command reads the prompt on stdin.">
            <div className="radio-row">
              <button type="button" className={brain === "placeholder" ? "sel" : ""} onClick={() => setBrain("placeholder")}>placeholder</button>
              <button type="button" className={brain === "agent" ? "sel" : ""} onClick={() => setBrain("agent")}>agent command</button>
            </div>
          </FormField>
        </div>
        {brain === "agent" && (
          <>
            <div style={{ marginTop: 14 }} />
            <FormField label="agent command" hint="One command line as it runs ON THE NODE: prompt on stdin, reply on stdout (AGENT_CMD in node.env).">
              <input className="txt" value={agentCmd} onChange={e => setAgentCmd(e.target.value)} placeholder="/usr/local/bin/my-agent --plain" />
            </FormField>
          </>
        )}
        <div style={{ marginTop: 14 }} />
        <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: "var(--fg-1)" }}>
          <input type="checkbox" checked={homeChat} disabled={!hive.home_chat_url}
                 onChange={e => setHomeChat(e.target.checked)} />
          home chat{hive.home_chat_url ? ` (${hive.home_chat_url})` : " (set home_chat_url in config/hive.toml)"}
        </label>
        <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: "var(--fg-1)", marginTop: 8 }}>
          <input type="checkbox" checked={reachable} onChange={e => setReachable(e.target.checked)} />
          chat from this console (the node listens on its network and answers only its own token off loopback)
        </label>
      </div>
      {error && (
        <div style={{ padding: "8px 16px", color: "var(--red)", fontFamily: "var(--mono)", fontSize: 11, borderTop: "1px solid var(--line)" }}>
          · {error}
        </div>
      )}
      <div className="foot">
        <button className="btn ghost" onClick={onClose}>cancel</button>
        <button className="btn primary" disabled={!valid || busy} onClick={submit}>
          {I.plus} {busy ? "building..." : `build node ${slug || "-"}`}
        </button>
      </div>
    </>
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

Object.assign(window, { CousinsView, CousinCard, RemoteCousinCard, RemoteSpawnForm, CopyButton, Inspector, IdentityField, AuthField, TelegramPanel, RoleEditor, ClaudeMdEditor, LoopsEditor, FlipModal, SpawnModal, SectionLabel, FormField });
