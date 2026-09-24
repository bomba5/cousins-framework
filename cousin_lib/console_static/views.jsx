// Jobs, Memory, Loops, Tokens, Tracker, Settings, Host views

function CousinTag({ slug }) {
  // The fleet-wide tag reads in the accent colour, as the source console
  // did; the per-slug hash hue is kept for log lines only, where telling
  // cousins apart at a glance is the point.
  return <span style={{ color: "var(--accent)", fontWeight: 500 }}>@{slug}</span>;
}

// ============ JOBS ============
// Sub-agent runs and long-running shell jobs. Live last-N-lines per running
// job, click to expand to the full log. Backed by /api/jobs (jobs.db).
function JobsView() {
  const [jobs, setJobs] = React.useState([]);
  // Default to "last24h" so an idle dashboard shows recent activity
  // instead of an empty 'active' tab when nothing is running NOW.
  // Order: active | last24h | done | failed | all.
  const [filter, setFilter] = React.useState("last24h");
  const [spawnedBy, setSpawnedBy] = React.useState("all");  // all | <slug>
  const [kind, setKind] = React.useState("all");            // all | <kind>
  const [openJob, setOpenJob] = React.useState(null);

  // Poll /api/jobs every 2s.
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      try {
        const params = new URLSearchParams();
        if (filter === "active") params.set("active_only", "1");
        else if (filter === "done") params.set("status", "done");
        else if (filter === "failed") params.set("status", "failed");
        else if (filter === "last24h") params.set("since_hours", "24");
        if (spawnedBy && spawnedBy !== "all") params.set("spawned_by", spawnedBy);
        if (kind && kind !== "all") params.set("kind", kind);
        const qs = params.toString();
        const r = await fetch("/api/jobs" + (qs ? "?" + qs : ""), { cache: "no-store" });
        const d = await r.json();
        if (!cancelled) setJobs(d.jobs || []);
      } catch (e) { /* keep last */ }
    };
    pull();
    const id = setInterval(pull, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [filter, spawnedBy, kind]);

  // Distinct spawned_by values across the current job list, for dropdown options.
  const spawnedByOptions = React.useMemo(() => {
    const set = new Set();
    for (const j of jobs) if (j.spawned_by) set.add(j.spawned_by);
    if (spawnedBy !== "all") set.add(spawnedBy);
    return ["all", ...[...set].sort()];
  }, [jobs, spawnedBy]);

  const kindOptions = React.useMemo(() => {
    const set = new Set();
    for (const j of jobs) if (j.kind) set.add(j.kind);
    if (kind !== "all") set.add(kind);  // keep the active selection listed
    return ["all", ...[...set].sort()];
  }, [jobs, kind]);

  const counts = {
    running: jobs.filter(j => j.status === "running").length,
    done: jobs.filter(j => j.status === "done").length,
    failed: jobs.filter(j => j.status === "failed").length,
  };

  const fmtAge = (started, ended) => {
    if (!started) return "-";
    const s = new Date(started).getTime();
    const e = ended ? new Date(ended).getTime() : Date.now();
    const sec = Math.max(0, Math.round((e - s) / 1000));
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.round(sec/60)}m`;
    return `${(sec/3600).toFixed(1)}h`;
  };

  const fmtStart = (started) => {
    if (!started) return "-";
    const d = new Date(started);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    if (sameDay) return `${hh}:${mm}`;
    // Older than today: show MM-DD HH:MM
    const mo = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${mo}-${dd} ${hh}:${mm}`;
  };

  const cancelJob = async (id) => {
    if (!confirm(`Cancel job #${id}? This will SIGTERM its process if known.`)) return;
    try {
      const { r, d } = await apiSend("POST", `/api/jobs/${id}`, { status: "cancelled" });
      if (!r.ok) alert("cancel failed: " + (d.error || r.status));
    } catch (e) { alert("cancel error: " + e.message); }
  };

  const deleteJob = async (id) => {
    if (!confirm(`Delete job #${id}? This removes the row and its console-minted log file.`)) return;
    try {
      const { r, d } = await apiSend("DELETE", `/api/jobs/${id}`);
      if (!r.ok) alert("delete failed: " + (d.error || r.status));
    } catch (e) { alert("delete error: " + e.message); }
  };

  // Live last-10-lines per running job (cheap inline tail).
  const [tails, setTails] = React.useState({});  // {jobId: tailString}
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      const running = jobs.filter(j => j.status === "running" && j.log_path);
      const next = {};
      for (const j of running) {
        try {
          const r = await fetch(`/api/jobs/${j.id}/log?lines=10`, { cache: "no-store" });
          const d = await r.json();
          if (d.ok) next[j.id] = d.log || "";
        } catch (_e) { /* skip */ }
      }
      if (!cancelled) setTails(next);
    };
    pull();
    const id = setInterval(pull, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, [jobs.map(j => j.id + j.status).join(",")]);

  return (
    <div className="wrap-pad">
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 14, fontFamily: "var(--mono)", fontSize: 12, color: "var(--fg-2)" }}
             title="counts reflect the current filters, not all history">
          <span style={{ color: "var(--accent)" }}><b>{counts.running}</b> running</span>
          <span><b>{counts.done}</b> done</span>
          <span style={{ color: "var(--red, #d24)" }}><b>{counts.failed}</b> failed</span>
          <span style={{ color: "var(--fg-3)" }}>(in view)</span>
        </div>
        <div style={{ flex: 1 }} />
        <div className="radio-row" style={{ display: "flex", gap: 4 }}>
          {["active", "last24h", "done", "failed", "all"].map(f => (
            <button key={f} className={filter === f ? "sel" : ""} onClick={() => setFilter(f)}>
              {f}
            </button>
          ))}
        </div>
        <select className="sel-inline" value={spawnedBy} onChange={e => setSpawnedBy(e.target.value)} title="filter by cousin">
          {spawnedByOptions.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select className="sel-inline" value={kind} onChange={e => setKind(e.target.value)} title="filter by job kind">
          {kindOptions.map(k => <option key={k} value={k}>{k}</option>)}
        </select>
      </div>

      {jobs.length === 0 && (
        <div style={{ padding: 30, textAlign: "center", color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 12 }}>
          no {filter} jobs
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {jobs.map(j => (
          <div key={j.id} className="panel"
               onClick={() => setOpenJob({ id: j.id, title: j.title, log: "loading...", spawned_by: j.spawned_by, status: j.status, kind: j.kind })}
               style={{ cursor: "pointer", padding: "12px 16px", borderLeft: `3px solid ${
                 j.status === "running" ? "var(--accent)" :
                 j.status === "done" ? "var(--green, #4a7)" :
                 j.status === "failed" ? "var(--red, #d24)" : "var(--fg-3)"
               }` }}>
            <div className="job-head" style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
              <CousinTag slug={j.spawned_by} />
              <span style={{ color: "var(--fg-3)", fontSize: 10, fontFamily: "var(--mono)" }}>#{j.id}</span>
              <span style={{ color: "var(--fg-3)", fontSize: 10, fontFamily: "var(--mono)" }}>[{j.kind}]</span>
              <span className={"pill " + ({ running: "cyan", done: "green", failed: "red" }[j.status] || "")}>{j.status}</span>
              <span className="job-title" style={{ flex: 1, fontWeight: 500 }}>{j.title}</span>
              <span className="job-time" style={{ color: "var(--fg-3)", fontSize: 10, fontFamily: "var(--mono)" }}
                    title={j.started_at || ""}>
                {fmtStart(j.started_at)} · {fmtAge(j.started_at, j.finished_at)}
              </span>
              {j.status === "running" && (
                <button className="btn ghost" onClick={(e) => { e.stopPropagation(); cancelJob(j.id); }}
                        style={{ fontSize: 10, padding: "2px 8px", minHeight: 20 }}>cancel</button>
              )}
              {j.status !== "running" && (
                <button className="btn ghost" onClick={(e) => { e.stopPropagation(); deleteJob(j.id); }}
                        style={{ fontSize: 10, padding: "2px 8px", minHeight: 20, color: "var(--fg-3)" }}>×</button>
              )}
            </div>
            {j.description && (
              <div style={{ fontSize: 11, color: "var(--fg-2)", marginBottom: 4 }}>{j.description}</div>
            )}
            {j.command && (
              <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)", marginBottom: 4, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                $ {j.command}
              </div>
            )}
            {!j.log_path && (
              <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)", marginBottom: 4 }}>no log attached</div>
            )}
            {j.status === "running" && tails[j.id] && (
              <pre style={{ margin: 0, padding: 6, background: "var(--bg-0)", border: "1px solid var(--line)",
                            borderRadius: 3, fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-2)",
                            maxHeight: 120, overflow: "auto", whiteSpace: "pre-wrap" }}>
                {tails[j.id]}
              </pre>
            )}
            {j.status === "failed" && j.exit_code !== null && j.exit_code !== undefined && (
              <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--red, #d24)" }}>
                exit {j.exit_code}{j.result_summary ? ` · ${j.result_summary}` : ""}
              </div>
            )}
            {j.status !== "failed" && j.result_summary && (
              <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)" }}>
                {j.result_summary}
              </div>
            )}
          </div>
        ))}
      </div>

      {openJob && (
        <div className="modal-bg" onClick={() => setOpenJob(null)}>
          <div className="modal" onClick={e => e.stopPropagation()}
               style={{ width: "min(960px, 95vw)", height: "85vh", maxHeight: "85vh", display: "flex", flexDirection: "column" }}>
            <div className="hdr">
              <span>job #{openJob.id} · @{openJob.spawned_by} · {openJob.title}</span>
              <button className="close" style={{ marginLeft: "auto" }} onClick={() => setOpenJob(null)}>×</button>
            </div>
            <div className="body" style={{ padding: 0, overflow: "hidden", flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
              <JobLogPanel jobId={openJob.id} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// The open job's log: loads the tail, then follows with ?from=<next>
// every 2 s and appends (docs/reference/console-api.md, jobs log). The box
// scrolls; it follows the end while the reader sits at the bottom,
// holds still once they scroll up, and resumes when they return to the
// bottom or press "follow". The browser keeps the last 2 MB.
const JOB_LOG_KEEP = 2 * 1024 * 1024;
function JobLogPanel({ jobId }) {
  const [text, setText] = React.useState("");
  const [meta, setMeta] = React.useState({ loaded: false, hasLog: true, path: null, size: 0 });
  const [following, setFollowing] = React.useState(true);
  const boxRef = React.useRef(null);
  const offsetRef = React.useRef(null);
  const followRef = React.useRef(true);

  React.useEffect(() => {
    let cancelled = false;
    offsetRef.current = null;
    setText("");
    const pull = async () => {
      for (let round = 0; round < 16 && !cancelled; round++) {
        let d;
        try {
          const url = offsetRef.current === null
            ? `/api/jobs/${jobId}/log?lines=2000`
            : `/api/jobs/${jobId}/log?from=${offsetRef.current}`;
          const r = await fetch(url, { cache: "no-store" });
          d = await r.json();
        } catch (_e) { return; }
        if (cancelled || !d || !d.ok) return;
        const hasLog = d.has_log !== false && !!d.log_path;
        setMeta({ loaded: true, hasLog, path: d.log_path, size: d.size || 0 });
        if (!hasLog) return;
        if (!d.size) { setText(d.log || ""); offsetRef.current = null; return; }
        if (offsetRef.current === null) {
          setText(d.log || "");
        } else if (d.size < offsetRef.current) {
          offsetRef.current = null;  // truncated or replaced: re-tail
          continue;
        } else if (d.log) {
          setText(t => {
            const n = t + d.log;
            return n.length > JOB_LOG_KEEP ? n.slice(n.length - JOB_LOG_KEEP) : n;
          });
        }
        const next = d.next !== undefined ? d.next : d.size;
        const caughtUp = next >= d.size;
        offsetRef.current = next;
        if (caughtUp) return;
      }
    };
    pull();
    const id = setInterval(pull, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [jobId]);

  React.useLayoutEffect(() => {
    const box = boxRef.current;
    if (box && followRef.current) box.scrollTop = box.scrollHeight;
  }, [text]);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    if (atBottom !== followRef.current) {
      followRef.current = atBottom;
      setFollowing(atBottom);
    }
  };
  const jump = () => {
    followRef.current = true;
    setFollowing(true);
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  };

  if (meta.loaded && !meta.hasLog) {
    return (
      <div className="joblog-hint" data-job-log-none>
        no log attached; start the job with <code>cousin-job start shell TITLE -- CMD</code> or <code>--log PATH</code> to stream one
      </div>
    );
  }
  return (
    <>
      <div className="joblog-bar">
        <span className="joblog-path">{meta.path || ""}</span>
        <span style={{ flex: 1 }} />
        <span>{meta.size ? `${(meta.size / 1024).toFixed(1)} kB · ` : ""}polls every 2s</span>
        <button className={"btn ghost" + (following ? " active" : "")} onClick={jump} data-job-log-follow={following ? "on" : "off"}
                title={following ? "following the end of the log" : "scrolled up: press to jump to the end and follow again"}>
          {following ? "following" : "follow"}
        </button>
      </div>
      <pre ref={boxRef} onScroll={onScroll} className="joblog" data-job-log>{text || (meta.loaded ? "(empty)" : "loading...")}</pre>
    </>
  );
}

// ============ MEMORY ============
// Unified view: the shared tier (with the review workflow) + each cousin's
// private memory as the layers it is built from (MemoryExplorer, in
// explorer.jsx): raw entries by truth level, digests and archive,
// distilled views, decisions, memory and note files, indexes, recall,
// trash.
function MemoryView() {
  const showHidden = (window.useSetting && window.useSetting("showHidden")) || false;
  const [scope, setScope] = React.useState("shared"); // "shared" | "<slug>"
  const [allCousins, setAllCousins] = React.useState([]);
  const cousins = React.useMemo(
    () => (allCousins || []).filter(c => showHidden || !c.hidden),
    [allCousins, showHidden]);
  const [shared, setShared] = React.useState({ canonical: [], pending: [] });
  const [audit, setAudit] = React.useState([]);
  const [selected, setSelected] = React.useState(null);
  const [content, setContent] = React.useState("");
  const [diff, setDiff] = React.useState("");
  const [status, setStatus] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [me, setMe] = React.useState(null);
  const [reviewers, setReviewers] = React.useState(null);

  const refresh = React.useCallback(async () => {
    const [cs, sl, au, auth] = await Promise.all([
      fetchCousins(),
      apiGet("/api/shared/list"),
      apiGet("/api/shared/audit?n=50"),
      fetchAuthMe(),
    ]);
    setAllCousins(cs);
    if (sl) setShared({ canonical: sl.canonical || [], pending: sl.pending || [] });
    if (au) setAudit(au.entries || []);
    if (auth) setMe(auth);
  }, []);

  React.useEffect(() => {
    refresh();
    const id = setInterval(refresh, 8000);
    return () => clearInterval(id);
  }, [refresh]);

  const openShared = async (kind, entry) => {
    setSelected({ scope: "shared", kind, ...entry });
    setDiff("");
    setContent("loading...");
    const r = await apiGet(`/api/shared/content?scope=${kind}&name=${encodeURIComponent(entry.name)}`);
    setContent(r?.content ?? "");
    if (kind === "pending") {
      const d = await apiGet(`/api/shared/diff?file=${encodeURIComponent(entry.origin)}&slug=${encodeURIComponent(entry.slug)}`);
      setDiff(d?.diff || "");
    }
  };

  const act = async (kind) => {
    if (!selected || selected.scope !== "shared" || selected.kind !== "pending" || busy) return;
    setBusy(true);
    setStatus(null);
    try {
      const body = { slug: selected.slug, file: selected.origin };
      // With auth not configured the tier still needs a reviewer name.
      if (!(me && me.configured)) {
        const by = window.prompt("reviewer name (auth is not configured, so the tier cannot infer it):");
        if (!by || !by.trim()) { setBusy(false); return; }
        body.by = by.trim();
      }
      const path = kind === "approve" ? "/api/shared/approve" : "/api/shared/reject";
      const { r, d } = await apiSend("POST", path, body);
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setStatus({ ok: true, msg: `${kind}d ${selected.origin}` });
      setSelected(null);
      setContent("");
      setDiff("");
      await refresh();
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  const scopes = [
    { id: "shared", label: "shared", count: shared.canonical.length + shared.pending.length },
    ...cousins.filter(c => !c.remote).map(c => ({ id: c.slug, label: `@${c.slug}` })),
  ];
  const scopeBar = (
    <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }} data-memory-scopes>
      {scopes.map(s => (
        <button key={s.id}
          className={scope === s.id ? "btn active" : "btn"}
          onClick={() => { setScope(s.id); setSelected(null); setContent(""); setDiff(""); }}
          style={{ fontSize: 11 }}>
          {s.label} {s.count !== undefined && <span style={{ color: "var(--fg-3)" }}>{s.count}</span>}
        </button>
      ))}
    </div>
  );

  if (scope !== "shared") {
    return (
      <div className="wrap-pad mx-page">
        {scopeBar}
        <MemoryExplorer slug={scope} />
      </div>
    );
  }

  return (
    <div className="wrap-pad" data-memory-grid style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 18, height: "calc(100dvh - 94px)" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, minHeight: 0 }}>
        {scopeBar}

        <>
            <div className="panel" style={{ flex: "0 0 auto" }}>
              <div className="panel-hdr"><span className="title">pending</span>
                <span style={{ color: "var(--fg-3)" }}>{shared.pending.length}</span></div>
              <div className="panel-body" style={{ padding: 0, maxHeight: 220, overflowY: "auto" }}>
                {shared.pending.length === 0 && <div style={{ padding: 10, color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 11 }}>no pending proposals</div>}
                {shared.pending.map(p => (
                  <div key={p.name} onClick={() => openShared("pending", p)}
                    style={{ padding: "8px 12px", borderBottom: "1px solid var(--line)", cursor: "pointer",
                             fontFamily: "var(--mono)", fontSize: 11,
                             background: selected?.name === p.name ? "var(--bg-2)" : "transparent" }}>
                    <div style={{ color: "var(--accent)" }}>@{p.slug} → {p.origin}</div>
                    <div style={{ color: "var(--fg-3)" }}>{fmtAgo(Math.floor(Date.now()/1000 - p.mtime))} · {p.sha}</div>
                  </div>
                ))}
              </div>
            </div>
            <div className="panel" style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
              <div className="panel-hdr"><span className="title">canonical</span>
                <span style={{ color: "var(--fg-3)" }}>{shared.canonical.length}</span></div>
              <div className="panel-body mem-list-body" style={{ padding: 0, overflowY: "auto", flex: 1 }}>
                {shared.canonical.map(c => (
                  <div key={c.name} onClick={() => openShared("canonical", { ...c, origin: c.name })}
                    style={{ padding: "8px 12px", borderBottom: "1px solid var(--line)", cursor: "pointer",
                             fontFamily: "var(--mono)", fontSize: 11,
                             background: selected?.name === c.name ? "var(--bg-2)" : "transparent" }}>
                    <div style={{ color: "var(--fg-1)" }}>{c.name}</div>
                    <div style={{ color: "var(--fg-3)" }}>{(c.size/1024).toFixed(1)}kb · {c.sha}</div>
                  </div>
                ))}
                {shared.canonical.length === 0 && <div style={{ padding: 10, color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 11 }}>no canonical files yet</div>}
              </div>
            </div>
            <SharedReviewersPanel onChange={setReviewers} />
          </>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 14, minHeight: 0 }}>
        <div className="panel mem-viewer" style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
          <div className="panel-hdr">
            <span className="title">{selected
              ? (selected.scope === "shared"
                  ? `${selected.kind === "pending" ? "proposal" : "canonical"}: ${selected.origin || selected.name}`
                  : selected.name)
              : "select a file"}</span>
            {selected?.scope === "shared" && selected.kind === "pending" && (
              <>
                <span style={{ flex: 1 }} />
                {reviewers && reviewers.you_review === false && (
                  <span className="muted" data-not-reviewer style={{ marginRight: 8 }}>you are not in the reviewer list</span>
                )}
                <button className="btn" disabled={busy} onClick={() => act("approve")}>approve</button>
                <button className="btn danger" disabled={busy} onClick={() => act("reject")} style={{ marginLeft: 6 }}>reject</button>
              </>
            )}
          </div>
          <div className="panel-body" style={{ padding: 0, overflow: "auto", flex: 1 }}>
            {status && (
              <div style={{ padding: "6px 12px", fontFamily: "var(--mono)", fontSize: 11,
                            color: status.ok ? "var(--green)" : "var(--red)",
                            borderBottom: "1px solid var(--line)" }}>{status.msg}</div>
            )}
            {selected?.scope === "shared" && selected.kind === "pending" && diff && (
              <pre style={{ margin: 0, padding: 12, fontSize: 11, color: "var(--fg-1)",
                            background: "var(--bg-0)", borderBottom: "1px solid var(--line)",
                            whiteSpace: "pre-wrap" }}>{diff}</pre>
            )}
            {content && <div style={{ padding: 12 }}><MarkdownDoc text={content} /></div>}
          </div>
        </div>
        {scope === "shared" && (
          <div className="panel" style={{ flex: "0 0 auto", maxHeight: 220 }}>
            <div className="panel-hdr"><span className="title">audit</span>
              <span style={{ color: "var(--fg-3)" }}>last {audit.length}</span></div>
            <div className="panel-body" style={{ padding: 0, overflowY: "auto", maxHeight: 180 }}>
              <table className="data audit-table">
                <tbody>
                  {audit.map((e, i) => (
                    <tr key={i}>
                      <td className="muted" style={{ width: 110 }}>{(e.ts || "").slice(11, 19)}</td>
                      <td style={{ width: 80 }}><StatePill state={e.kind === "promote" ? "succeeded" : e.kind === "reject" ? "failed" : "queued"} /></td>
                      <td style={{ width: 90 }} className="accent">@{e.proposer || e.actor}</td>
                      <td className="wrap-any" style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-1)" }}>{e.file}{e.reason ? <span className="muted"> · {e.reason}</span> : null}</td>
                    </tr>
                  ))}
                  {audit.length === 0 && <tr><td colSpan={4} className="muted" style={{ padding: 14, textAlign: "center" }}>no audit entries yet</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ============ LOOPS ============
function LoopsView({ loops }) {
  const showHidden = (window.useSetting && window.useSetting("showHidden")) || false;
  const [adding, setAdding] = React.useState(false);
  const [allCousins, setAllCousins] = React.useState([]);
  const cousins = React.useMemo(
    () => (allCousins || []).filter(c => showHidden || !c.hidden),
    [allCousins, showHidden]);
  const [slug, setSlug] = React.useState("");
  const [form, setForm] = React.useState({ name: "", interval: 600, prompt: "", enabled: true });
  const [status, setStatus] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [daemon, setDaemon] = React.useState(null);
  const [errors, setErrors] = React.useState([]);

  // The daemon status and any malformed cousin.toml are part of every
  // loops response; the shell's SSE rows do not carry them.
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      const d = await fetchLoopsFull();
      if (cancelled) return;
      setDaemon(d.daemon || null);
      setErrors(d.errors || []);
    };
    pull();
    const id = setInterval(pull, 15000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  React.useEffect(() => {
    fetchCousins().then(cs => {
      setAllCousins(cs);
      const eligible = cs.filter(c => showHidden || !c.hidden);
      if (!slug && eligible.length) setSlug(eligible[0].slug);
    });
  }, [adding]);

  const submit = async () => {
    if (!slug || !form.name.trim() || busy) return;
    setBusy(true);
    setStatus(null);
    try {
      const r = await fetch(`/api/cousins/${slug}/loops`, { cache: "no-store" });
      const existing = (await r.json()).loops || [];
      const next = [...existing, {
        name: form.name.trim(),
        interval_seconds: Number(form.interval) || 600,
        prompt: form.prompt || `Loop ${form.name} fired.`,
        enabled: form.enabled,
      }];
      const { r: r2, d } = await apiSend("POST", `/api/cousins/${slug}/loops`, { loops: next });
      if (!r2.ok || !d.ok) throw new Error(d.error || `HTTP ${r2.status}`);
      setStatus({ ok: true, msg: `saved ${form.name} on @${slug}` });
      setForm({ name: "", interval: 600, prompt: "", enabled: true });
      setAdding(false);
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="wrap-pad">
      {daemon && daemon.ok === false && (
        <div style={{ marginBottom: 10, padding: "8px 12px", fontSize: 11, fontFamily: "var(--mono)",
                      color: "var(--amber)", background: "oklch(from var(--amber) l c h / 0.10)",
                      border: "1px solid var(--amber)", borderRadius: 3 }}>
          {daemon.message || "loops daemon down"}
        </div>
      )}
      {errors.length > 0 && (
        <div style={{ marginBottom: 10, padding: "8px 12px", fontSize: 11, fontFamily: "var(--mono)",
                      color: "var(--red)", background: "oklch(from var(--red) l c h / 0.08)",
                      border: "1px solid var(--red)", borderRadius: 3 }}>
          {errors.map((e, i) => <div key={i}>{e}</div>)}
        </div>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" }}>{loops.length} loops</span>
        {daemon && daemon.ok && daemon.last_tick && (
          <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" }}>· daemon tick {fmtAgo(Math.floor(Date.now() / 1000 - daemon.last_tick))}</span>
        )}
        <span style={{ flex: 1 }} />
        {status && (
          <span style={{ fontSize: 11, fontFamily: "var(--mono)", color: status.ok ? "var(--green)" : "var(--red)" }}>{status.msg}</span>
        )}
        {!adding && (
          <button className="btn primary" onClick={() => setAdding(true)}>{I.plus} new loop</button>
        )}
      </div>
      {adding && (
        <div className="panel" style={{ marginBottom: 10, padding: 10 }}>
          <div style={{ display: "grid", gridTemplateColumns: "120px 140px 90px 1fr 60px 80px 80px", gap: 6, alignItems: "center" }}>
            <select className="sel" value={slug} onChange={e => setSlug(e.target.value)}>
              {cousins.filter(c => !c.remote).map(c => <option key={c.slug} value={c.slug}>@{c.slug}</option>)}
              {cousins.length === 0 && <option value="">(no cousin)</option>}
            </select>
            <input className="txt" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="loop name" />
            <input className="txt" type="number" value={form.interval} onChange={e => setForm({ ...form, interval: e.target.value })} placeholder="sec" />
            <input className="txt" value={form.prompt} onChange={e => setForm({ ...form, prompt: e.target.value })} placeholder="prompt to inject" />
            <label style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-2)" }}>
              <input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} /> on
            </label>
            <button className="btn" onClick={() => { setAdding(false); setStatus(null); }}>cancel</button>
            <button className="btn primary" disabled={!slug || !form.name.trim() || busy} onClick={submit}>{busy ? "saving..." : "save"}</button>
          </div>
        </div>
      )}
      <LoopsTable loops={loops} allCousins={allCousins} />
    </div>
  );
}

function LoopDriftModal({ cousin, name, onClose }) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    fetch(`/api/loops/drift/${cousin}/${name}`, { cache: "no-store" })
      .then(r => r.json())
      .then(d => { if (!cancelled) (d.ok ? setData(d) : setErr(d.error || "load failed")); })
      .catch(e => { if (!cancelled) setErr(String(e.message || e)); });
    const onKey = e => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => { cancelled = true; window.removeEventListener("keydown", onKey); };
  }, [cousin, name, onClose]);

  const drifts = data ? data.points.map(p => p.drift) : [];
  const intervals = data ? data.points.map(p => p.interval) : [];
  const avgDrift = drifts.length ? Math.round(drifts.reduce((a, b) => a + b, 0) / drifts.length) : 0;
  const maxDrift = drifts.length ? Math.max(...drifts) : 0;
  const minDrift = drifts.length ? Math.min(...drifts) : 0;

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{ width: "min(720px, 94vw)" }}>
        <div className="hdr">
          <span>loop drift · {cousin}/{name}</span>
          <span style={{ color: "var(--fg-3)", marginLeft: "auto" }}>
            {data ? `${data.n} fires · configured ${data.interval}s` : ""}
          </span>
          <button className="close" onClick={onClose}>×</button>
        </div>
        <div className="body" style={{ fontFamily: "var(--mono)", fontSize: 12 }}>
          {err && <div style={{ color: "var(--red)" }}>{err}</div>}
          {!err && !data && <div style={{ color: "var(--fg-3)" }}>loading...</div>}
          {data && data.n === 0 && <div style={{ color: "var(--fg-3)" }}>not enough fires yet (need at least 2 in the daemon's fire log)</div>}
          {data && data.n > 0 && (
            <>
              <div style={{ marginBottom: 10, color: "var(--fg-2)" }}>
                actual interval between fires (s)
              </div>
              <Spark data={intervals} width={680} height={80} />
              <div style={{ marginTop: 18, marginBottom: 10, color: "var(--fg-2)" }}>
                drift vs configured (s) · avg {avgDrift} · min {minDrift} · max {maxDrift}
              </div>
              <Spark data={drifts} width={680} height={60} tone={maxDrift > 60 ? "warn" : ""} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function LoopEditModal({ cousin, loop, onClose, onSaved }) {
  const [name, setName] = React.useState(loop.name || "");
  const [intervalSec, setIntervalSec] = React.useState(String(loop.schedule?.interval_seconds || ""));
  const [dailyAt, setDailyAt] = React.useState(loop.schedule?.daily_at || "");
  const [days, setDays] = React.useState((loop.schedule?.days || []).join(","));
  const [cron, setCron] = React.useState(loop.schedule?.cron || "");
  const [prompt, setPrompt] = React.useState(loop.prompt || "");
  const [enabled, setEnabled] = React.useState(loop.enabled !== false);
  const [hidden, setHidden] = React.useState(!!loop.hidden);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);

  React.useEffect(() => {
    const onKey = e => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const save = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await fetch(`/api/cousins/${cousin}/loops`, { cache: "no-store" });
      const existing = (await r.json()).loops || [];
      const updated = existing.map(l => {
        if ((l.name || "") !== loop.name) return l;
        // Exactly one schedule form is written; the others are dropped so
        // the daemon's validation does not see two.
        const entry = { ...l, name: name.trim(), prompt, enabled, hidden };
        delete entry.interval_seconds; delete entry.daily_at; delete entry.cron; delete entry.days;
        if (cron.trim()) entry.cron = cron.trim();
        else if (dailyAt.trim()) {
          entry.daily_at = dailyAt.trim();
          const dl = days.split(",").map(s => s.trim()).filter(Boolean);
          if (dl.length) entry.days = dl;
        } else entry.interval_seconds = Number(intervalSec) || 0;
        return entry;
      });
      const { r: r2, d } = await apiSend("POST", `/api/cousins/${cousin}/loops`, { loops: updated });
      if (!r2.ok || !d.ok) throw new Error(d.error || `HTTP ${r2.status}`);
      onSaved && onSaved();
      onClose();
    } catch (e) {
      setErr(String(e.message || e));
    } finally { setBusy(false); }
  };

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{ width: "min(720px, 94vw)" }}>
        <div className="hdr">
          <span>edit loop · {cousin}/{loop.name}</span>
          <button className="close" onClick={onClose}>×</button>
        </div>
        <div className="body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <Field label="name"><input className="txt" value={name} onChange={e => setName(e.target.value)} style={{ width: "100%" }} /></Field>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 2fr", gap: 8 }}>
            <Field label="interval_seconds"><input className="txt" type="number" value={intervalSec} onChange={e => setIntervalSec(e.target.value)} style={{ width: "100%" }} placeholder="0 = no interval" /></Field>
            <Field label="daily_at"><input className="txt" value={dailyAt} onChange={e => setDailyAt(e.target.value)} style={{ width: "100%" }} placeholder="HH:MM" /></Field>
            <Field label="days (mon,tue...)"><input className="txt" value={days} onChange={e => setDays(e.target.value)} style={{ width: "100%" }} placeholder="all" /></Field>
          </div>
          <Field label="cron (5-field, optional; wins over the other forms)"><input className="txt" value={cron} onChange={e => setCron(e.target.value)} style={{ width: "100%" }} placeholder='e.g. "*/15 * * * *"' /></Field>
          <Field label="prompt"><textarea className="txt" value={prompt} onChange={e => setPrompt(e.target.value)} style={{ width: "100%", minHeight: 200, resize: "vertical", fontFamily: "var(--mono)", fontSize: 12 }} /></Field>
          <div style={{ display: "flex", gap: 14 }}>
            <label style={{ fontSize: 12, color: "var(--fg-1)", display: "inline-flex", alignItems: "center", gap: 6 }}>
              <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} /> enabled
            </label>
            <label style={{ fontSize: 12, color: "var(--fg-1)", display: "inline-flex", alignItems: "center", gap: 6 }}
              title="hidden loops disappear from the Loops view unless Settings: show hidden is on">
              <input type="checkbox" checked={hidden} onChange={e => setHidden(e.target.checked)} /> hidden
            </label>
          </div>
          {err && <div style={{ padding: "5px 9px", color: "var(--red)", fontSize: 11, fontFamily: "var(--mono)", background: "oklch(from var(--red) l c h / 0.08)", borderRadius: 3 }}>{err}</div>}
          <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
            <span style={{ flex: 1 }} />
            <button className="btn" onClick={onClose}>cancel</button>
            <button className="btn primary" onClick={save} disabled={busy}>{busy ? "saving..." : "save"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 4 }}>{label}</div>
      {children}
    </div>
  );
}

function fmtCountdown(secs) {
  if (secs <= 0) return "due";
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  return `${Math.floor(secs / 86400)}d ${Math.floor((secs % 86400) / 3600)}h`;
}

function LoopsTable({ loops, allCousins }) {
  const [confirm, setConfirm] = React.useState(null);
  const [firing, setFiring] = React.useState(null);
  const [fireToast, setFireToast] = React.useState(null);
  const [driftOpen, setDriftOpen] = React.useState(null);
  const [editOpen, setEditOpen] = React.useState(null);
  // Tick every second to keep the next-fire countdown live.
  const [, setNow] = React.useState(Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  React.useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(null), 3500);
    return () => clearTimeout(t);
  }, [confirm]);
  React.useEffect(() => {
    if (!fireToast) return;
    const t = setTimeout(() => setFireToast(null), 4000);
    return () => clearTimeout(t);
  }, [fireToast]);

  // A fire is a request row the daemon consumes; the toast says
  // "requested", never "fired" (docs/reference/console-api.md).
  const fireNow = async (cousin, name) => {
    setFiring(`${cousin}/${name}`);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin}/loops/${encodeURIComponent(name)}/fire`);
      if (r.ok && d.ok) setFireToast({ ok: true, msg: `fire requested: ${name} on @${cousin} (request #${d.request_id})` });
      else setFireToast({ ok: false, msg: `${name}: ${d.error || "HTTP " + r.status}` });
    } catch (e) {
      setFireToast({ ok: false, msg: String(e.message || e) });
    } finally {
      setFiring(null);
    }
  };

  const remove = async (cousin, name) => {
    const key = `${cousin}/${name}`;
    if (confirm !== key) { setConfirm(key); return; }
    setConfirm(null);
    try {
      const r = await fetch(`/api/cousins/${cousin}/loops`, { cache: "no-store" });
      const existing = (await r.json()).loops || [];
      const next = existing.filter(l => (l.name || "") !== name);
      const { r: r2, d } = await apiSend("POST", `/api/cousins/${cousin}/loops`, { loops: next });
      if (!r2.ok || !d.ok) setFireToast({ ok: false, msg: `remove ${name}: ${d.error || "HTTP " + r2.status}` });
    } catch (_e) {}
  };

  // The synthetic context-heartbeat row is not a [[loops]] entry: it can
  // be fired but not edited, hidden or removed from this view.
  const removable = (l) => l.name !== "context-heartbeat";
  const hideable = (l) => l.name !== "context-heartbeat";

  const showHidden = (window.useSetting && window.useSetting("showHidden")) || false;
  const hiddenCousinSlugs = React.useMemo(
    () => new Set((allCousins || []).filter(c => c.hidden).map(c => c.slug)),
    [allCousins]);
  const visibleLoops = loops.filter(l =>
    (showHidden || !l.hidden) &&
    (showHidden || !hiddenCousinSlugs.has(l.cousin)));
  const hiddenCount = loops.filter(l => l.hidden || hiddenCousinSlugs.has(l.cousin)).length;

  const toggleHidden = async (cousin, name, currentlyHidden) => {
    try {
      await apiSend("POST", `/api/cousins/${cousin}/loops/${encodeURIComponent(name)}/hidden`, { hidden: !currentlyHidden });
    } catch (_e) {}
  };

  const renderRow = (l, i) => {
    const key = `${l.cousin}/${l.name}`;
    const editable = l.name !== "context-heartbeat" && l.schedule;
    const nowSec = Math.floor(Date.now() / 1000);
    const nextIn = l.nextFireTs ? l.nextFireTs - nowSec : 0;
    return (
      <tr key={key + i}>
        <td data-label="cousin"><CousinTag slug={l.cousin} /></td>
        <td data-label="loop" style={{ color: "var(--fg-0)" }}>
          {l.name}
          {l.hidden && <span style={{ marginLeft: 6, fontSize: 9, padding: "1px 5px", borderRadius: 2, background: "oklch(from var(--fg-3) l c h / 0.18)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.05em" }}>hidden</span>}
        </td>
        <td data-label="state"><StatePill state={l.state} /></td>
        <td data-label="interval" className="muted">{
          l.interval ? l.interval + "s"
          : l.schedule?.cron ? `cron: ${l.schedule.cron}`
          : l.schedule?.daily_at ? `daily ${l.schedule.daily_at}${l.schedule.days?.length ? ` (${l.schedule.days.join(",")})` : ""}`
          : "-"
        }</td>
        <td data-label="last fire" className="muted">{l.lastFireTs ? fmtAgo(l.lastTick) : "never"}</td>
        <td data-label="next in" className="muted" style={{ color: l.nextFireTs && nextIn <= 0 ? "var(--accent)" : "var(--fg-2)" }}>
          {l.nextFireTs ? fmtCountdown(nextIn) : <span style={{opacity:0.4}}>-</span>}
        </td>
        <td data-label="drift" className="num" style={{ color: l.drift > 300 ? "var(--red)" : l.drift > 60 ? "var(--amber)" : "var(--fg-2)" }}>
          {l.drift ? l.drift + "s" : "-"}
        </td>
        <td data-label="note" className="muted">{l.note || <span style={{opacity:0.4}}>-</span>}</td>
        <td data-label="actions" style={{ textAlign: "right", whiteSpace: "nowrap" }}>
          {editable && (
            <button className="btn ghost" style={{ fontSize: 10, padding: "2px 6px", marginRight: 4 }}
              onClick={() => setEditOpen(l)}
              title="edit loop schedule + prompt">
              edit
            </button>
          )}
          <button className="btn ghost" style={{ fontSize: 10, padding: "2px 6px", marginRight: 4 }}
            onClick={() => setDriftOpen({ cousin: l.cousin, name: l.name })}
            title="show drift series">
            drift
          </button>
          <button className="btn" style={{ fontSize: 10, padding: "2px 6px", marginRight: 4 }}
            onClick={() => fireNow(l.cousin, l.name)}
            disabled={firing === key}
            title="request a fire now (a request row the daemon consumes)">
            {firing === key ? "..." : "fire"}
          </button>
          {hideable(l) && (
            <button className="btn ghost" style={{ fontSize: 10, padding: "2px 6px", marginRight: 4 }}
              onClick={() => toggleHidden(l.cousin, l.name, l.hidden)}
              title={l.hidden ? "loop is hidden - click to unhide" : "hide this loop from the default view"}>
              {l.hidden ? "unhide" : "hide"}
            </button>
          )}
          {removable(l) && (
            <button className="btn danger" style={{ fontSize: 10, padding: "2px 6px", minWidth: 28 }}
              onClick={() => remove(l.cousin, l.name)}
              title={confirm === key ? "click again to confirm" : "remove loop"}>
              {confirm === key ? "sure?" : "×"}
            </button>
          )}
        </td>
      </tr>
    );
  };

  return (
    <div className="panel">
      {driftOpen && <LoopDriftModal {...driftOpen} onClose={() => setDriftOpen(null)} />}
      {editOpen && <LoopEditModal cousin={editOpen.cousin} loop={editOpen} onClose={() => setEditOpen(null)} />}
      {fireToast && (
        <div style={{ padding: "8px 12px", fontSize: 11, fontFamily: "var(--mono)",
                      borderBottom: "1px solid var(--line)",
                      color: fireToast.ok ? "var(--green)" : "var(--red)",
                      background: fireToast.ok ? "oklch(from var(--green) l c h / 0.08)" : "oklch(from var(--red) l c h / 0.08)" }}>
          {fireToast.msg}
        </div>
      )}
      {hiddenCount > 0 && !showHidden && (
        <div style={{ padding: "6px 12px", fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)", borderBottom: "1px solid var(--line)" }}>
          {hiddenCount} loop{hiddenCount === 1 ? "" : "s"} hidden · Settings: show hidden to reveal
        </div>
      )}
      <table className="data loops-table">
        <thead>
          <tr>
            <th>cousin</th>
            <th>loop</th>
            <th>state</th>
            <th>interval</th>
            <th>last fire</th>
            <th>next in</th>
            <th className="num">drift</th>
            <th>note</th>
            <th style={{ width: 180 }}></th>
          </tr>
        </thead>
        <tbody>
          {visibleLoops.map((l, i) => renderRow(l, i))}
          {visibleLoops.length === 0 && (
            <tr><td colSpan={9} className="muted" style={{ textAlign: "center", padding: 20 }}>no loops configured</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// ============ TOKENS ============
// Counts come from the harness transcripts through the seam; when the
// seam is not configured the view says so instead of showing zeros.
function TokensView({ cousins: allCousins }) {
  const showHidden = (window.useSetting && window.useSetting("showHidden")) || false;
  const cousins = React.useMemo(
    () => (allCousins || []).filter(c => showHidden || !c.hidden),
    [allCousins, showHidden]);
  const [tok, setTok] = React.useState(null);

  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => { const d = await fetchTokens(); if (!cancelled) setTok(d); };
    pull();
    const id = setInterval(pull, 30000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const totalSpent = cousins.reduce((s, c) => s + (Number(c.tokensSpent) || 0), 0);
  const seriesFor = (slug) => {
    const row = (tok?.cousins || []).find(c => c.slug === slug);
    return row ? row.series || [] : [];
  };
  // The prompt-cache hit rate the route measured (cache_read over everything
  // cacheable); null when the window held no usage, shown as "-".
  const cacheFor = (slug) => {
    const row = (tok?.cousins || []).find(c => c.slug === slug);
    return row && row.cache ? row.cache : { rate: null, days: [] };
  };
  const fmtRate = (r) => (r == null ? "-" : Math.round(r * 100) + "%");
  const fleetSeries = React.useMemo(() => {
    const byDay = {};
    for (const c of (tok?.cousins || [])) {
      if (!cousins.some(x => x.slug === c.slug)) continue;
      for (const p of c.series || []) byDay[p.day] = (byDay[p.day] || 0) + (p.total || 0);
    }
    return Object.keys(byDay).sort().map(day => ({ day, total: byDay[day] }));
  }, [tok, cousins]);
  const fleetTwoWeeks = fleetSeries.reduce((s, p) => s + p.total, 0);

  return (
    <div className="wrap-pad">
      {tok && tok.available === false && (
        <div style={{ marginBottom: 14, padding: "8px 12px", fontSize: 11, fontFamily: "var(--mono)",
                      color: "var(--amber)", background: "oklch(from var(--amber) l c h / 0.10)",
                      border: "1px solid var(--amber)", borderRadius: 3 }}>
          token counts unavailable: {tok.reason || "no transcripts seam configured"}
        </div>
      )}
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-hdr">
          <span className="title">fleet</span>
          <span style={{ color: "var(--fg-3)" }}>today and the last 14 days</span>
        </div>
        <div className="panel-body">
          <div data-token-row style={{ display: "flex", gap: 32, alignItems: "center", marginBottom: 16 }}>
            <Stat label="spent today" value={fmtTokens(totalSpent)} />
            <Stat label="last 14 days" value={fmtTokens(fleetTwoWeeks)} />
            <div style={{ flex: 1 }} />
            {fleetSeries.length > 1 && <Spark data={fleetSeries.map(p => p.total)} width={220} height={40} />}
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 14 }}>
        {cousins.map(c => {
          const series = seriesFor(c.slug);
          const twoWeeks = series.reduce((s, p) => s + (p.total || 0), 0);
          const output = series.reduce((s, p) => s + (p.output || 0), 0);
          const cache = cacheFor(c.slug);
          const cacheToday = cache.days.length ? cache.days[cache.days.length - 1].rate : null;
          return (
            <div className="panel" key={c.slug}>
              <div className="panel-hdr">
                <CousinTag slug={c.slug} />
                <span className="title">{c.name}</span>
                <span style={{ flex: 1 }} />
                <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" }}>{c.type}</span>
              </div>
              <div className="panel-body">
                <div className="tok-grid">
                  <Stat small label="today" value={fmtTokens(c.tokensSpent)} />
                  <Stat small label="14 days" value={fmtTokens(twoWeeks)} />
                  <Stat small label="output" value={fmtTokens(output)} />
                  <Stat small label="cache today" value={fmtRate(cacheToday)} />
                  <Stat small label="cache 14 days" value={fmtRate(cache.rate)} />
                  <div className="tok-spark">
                    {series.length > 1
                      ? <Spark data={series.map(p => p.total)} width={100} height={28} />
                      : <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" }}>no series</span>}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
        {cousins.length === 0 && (
          <div style={{ padding: 20, color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 12 }}>no cousins registered</div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, small }) {
  return (
    <div className="stat-cell">
      <div className="eyebrow">{label}</div>
      <div className="stat-value" style={{ fontSize: small ? 15 : 22 }}>{value}</div>
    </div>
  );
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

// ============ TRACKER ============
// The framework-wide ledger of in-flight work (docs/jobs-and-loops.md). The
// store is the library's; this view is one caller of the same routes the
// CLI's --json output mirrors. Written fresh: the source had no view.
const TRACKER_STATES = ["open", "active", "blocked", "done", "dropped"];
const TRACKER_CLOSED = new Set(["done", "dropped"]);

function TrackerView({ cousins }) {
  const [items, setItems] = React.useState([]);
  const [filters, setFilters] = React.useState({ owner: "", state: "", domain: "" });
  const [showClosed, setShowClosed] = React.useState(false);
  const [adding, setAdding] = React.useState(false);
  const [form, setForm] = React.useState({ title: "", domain: "", state: "open", owner: "", tags: "", notes: "" });
  const [editing, setEditing] = React.useState(null); // item being edited
  const [status, setStatus] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  const pull = React.useCallback(async () => {
    const params = new URLSearchParams();
    for (const k of ["owner", "state", "domain"]) if (filters[k]) params.set(k, filters[k]);
    const qs = params.toString();
    const d = await apiGet("/api/tracker" + (qs ? "?" + qs : ""));
    if (d) setItems(d.items || []);
  }, [filters]);

  React.useEffect(() => {
    pull();
    const id = setInterval(pull, 5000);
    return () => clearInterval(id);
  }, [pull]);

  React.useEffect(() => {
    if (!status) return;
    const t = setTimeout(() => setStatus(null), 4000);
    return () => clearTimeout(t);
  }, [status]);

  const parseTags = (s) => s.split(",").map(t => t.trim()).filter(Boolean);
  const itemNotes = (it) => it.notes ?? it.summary ?? "";
  const itemUpdated = (it) => it.updated_at || it.updated || "";
  const itemTags = (it) => Array.isArray(it.tags) ? it.tags : [];

  const create = async () => {
    if (!form.title.trim() || busy) return;
    setBusy(true);
    try {
      const body = {
        title: form.title.trim(), domain: form.domain.trim(), state: form.state,
        owner: form.owner.trim(), tags: parseTags(form.tags), notes: form.notes,
      };
      const { r, d } = await apiSend("POST", "/api/tracker", body);
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
      setStatus({ ok: true, msg: `added #${d.item?.id ?? "?"}` });
      setForm({ title: "", domain: "", state: "open", owner: "", tags: "", notes: "" });
      setAdding(false);
      await pull();
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally { setBusy(false); }
  };

  const patch = async (id, fields) => {
    setBusy(true);
    try {
      const { r, d } = await apiSend("POST", `/api/tracker/${id}`, fields);
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
      setStatus({ ok: true, msg: `updated #${id}` });
      await pull();
      return true;
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
      return false;
    } finally { setBusy(false); }
  };

  const remove = async (id) => {
    if (!confirm(`Delete tracker item #${id}? The id is never reused.`)) return;
    setBusy(true);
    try {
      const { r, d } = await apiSend("DELETE", `/api/tracker/${id}`);
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
      setStatus({ ok: true, msg: `deleted #${id}` });
      await pull();
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally { setBusy(false); }
  };

  const visible = items.filter(it => showClosed || !TRACKER_CLOSED.has(it.state));
  const closedCount = items.filter(it => TRACKER_CLOSED.has(it.state)).length;
  const owners = React.useMemo(() => {
    const set = new Set((cousins || []).map(c => c.slug));
    for (const it of items) if (it.owner) set.add(it.owner);
    return [...set].sort();
  }, [items, cousins]);
  const domains = React.useMemo(() => {
    const set = new Set();
    for (const it of items) if (it.domain) set.add(it.domain);
    return [...set].sort();
  }, [items]);

  const stateTone = (s) => s === "active" ? "green" : s === "blocked" ? "red" : s === "open" ? "amber" : "gray";

  return (
    <div className="wrap-pad">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap" }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" }}>
          {visible.length} items{closedCount > 0 && !showClosed ? ` · ${closedCount} closed hidden` : ""}
        </span>
        <select className="sel-inline" value={filters.owner} onChange={e => setFilters({ ...filters, owner: e.target.value })} title="owner">
          <option value="">any owner</option>
          {owners.map(o => <option key={o} value={o}>@{o}</option>)}
        </select>
        <select className="sel-inline" value={filters.state} onChange={e => setFilters({ ...filters, state: e.target.value })} title="state">
          <option value="">any state</option>
          {TRACKER_STATES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select className="sel-inline" value={filters.domain} onChange={e => setFilters({ ...filters, domain: e.target.value })} title="domain">
          <option value="">any domain</option>
          {domains.map(d => <option key={d} value={d}>{d}</option>)}
        </select>
        <label style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-2)", display: "inline-flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={showClosed} onChange={e => setShowClosed(e.target.checked)} /> show closed
        </label>
        <span style={{ flex: 1 }} />
        {status && (
          <span style={{ fontSize: 11, fontFamily: "var(--mono)", color: status.ok ? "var(--green)" : "var(--red)" }}>{status.msg}</span>
        )}
        {!adding && <button className="btn primary" onClick={() => setAdding(true)}>{I.plus} new item</button>}
      </div>

      {adding && (
        <div className="panel" style={{ marginBottom: 12, padding: 10 }}>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr 1fr", gap: 6, alignItems: "center", marginBottom: 6 }}>
            <input className="txt" autoFocus value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="title (required)" />
            <input className="txt" value={form.domain} onChange={e => setForm({ ...form, domain: e.target.value })} placeholder="domain" />
            <select className="sel" value={form.state} onChange={e => setForm({ ...form, state: e.target.value })}>
              {TRACKER_STATES.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            <input className="txt" value={form.owner} onChange={e => setForm({ ...form, owner: e.target.value })} placeholder="owner (slug)" list="tracker-owners" />
            <input className="txt" value={form.tags} onChange={e => setForm({ ...form, tags: e.target.value })} placeholder="tags, comma separated" />
          </div>
          <datalist id="tracker-owners">{owners.map(o => <option key={o} value={o} />)}</datalist>
          <textarea className="txt" value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })} placeholder="notes" style={{ width: "100%", minHeight: 60, boxSizing: "border-box" }} />
          <div style={{ display: "flex", gap: 6, marginTop: 6, justifyContent: "flex-end" }}>
            <button className="btn" onClick={() => setAdding(false)}>cancel</button>
            <button className="btn primary" disabled={!form.title.trim() || busy} onClick={create}>{busy ? "saving..." : "add"}</button>
          </div>
        </div>
      )}

      <div className="panel">
        <table className="data tracker-table">
          <thead>
            <tr>
              <th style={{ width: 50 }}>id</th>
              <th>title</th>
              <th>owner</th>
              <th>domain</th>
              <th>state</th>
              <th>tags</th>
              <th>updated</th>
              <th style={{ width: 120 }}></th>
            </tr>
          </thead>
          <tbody>
            {visible.map(it => (
              <React.Fragment key={it.id}>
                <tr style={{ opacity: TRACKER_CLOSED.has(it.state) ? 0.6 : 1 }}>
                  <td className="muted" data-label="id">#{it.id}</td>
                  <td data-label="title" style={{ color: "var(--fg-0)" }}>
                    {it.title}
                    {itemNotes(it) && <div style={{ fontSize: 10, color: "var(--fg-3)", whiteSpace: "pre-wrap" }}>{itemNotes(it)}</div>}
                  </td>
                  <td data-label="owner">{it.owner ? <CousinTag slug={it.owner} /> : <span className="muted">-</span>}</td>
                  <td className="muted" data-label="domain">{it.domain || "-"}</td>
                  <td data-label="state">
                    <select className="sel-inline" value={it.state} onChange={e => patch(it.id, { state: e.target.value })} style={{ color: `var(--${stateTone(it.state)}, var(--fg-1))` }} disabled={busy}>
                      {TRACKER_STATES.map(s => <option key={s} value={s}>{s}</option>)}
                    </select>
                  </td>
                  <td className="muted" data-label="tags" style={{ fontFamily: "var(--mono)", fontSize: 10 }}>{itemTags(it).join(", ") || "-"}</td>
                  <td className="muted" data-label="updated" title={itemUpdated(it)}>{itemUpdated(it).slice(0, 16).replace("T", " ") || "-"}</td>
                  <td data-label="actions" style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button className="btn ghost" style={{ fontSize: 10, padding: "2px 6px", marginRight: 4 }}
                      onClick={() => setEditing(editing && editing.id === it.id ? null : { ...it, tagsText: itemTags(it).join(", "), notesText: itemNotes(it) })}>
                      {editing && editing.id === it.id ? "close" : "edit"}
                    </button>
                    <button className="btn danger" style={{ fontSize: 10, padding: "2px 6px" }} onClick={() => remove(it.id)} disabled={busy}>×</button>
                  </td>
                </tr>
                {editing && editing.id === it.id && (
                  <tr>
                    <td colSpan={8} data-label="edit" style={{ background: "var(--bg-0)", padding: 10 }}>
                      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr", gap: 6, marginBottom: 6 }}>
                        <input className="txt" value={editing.title} onChange={e => setEditing({ ...editing, title: e.target.value })} placeholder="title" />
                        <input className="txt" value={editing.domain || ""} onChange={e => setEditing({ ...editing, domain: e.target.value })} placeholder="domain" />
                        <input className="txt" value={editing.owner || ""} onChange={e => setEditing({ ...editing, owner: e.target.value })} placeholder="owner" list="tracker-owners" />
                        <input className="txt" value={editing.tagsText} onChange={e => setEditing({ ...editing, tagsText: e.target.value })} placeholder="tags, comma separated" />
                      </div>
                      <textarea className="txt" value={editing.notesText} onChange={e => setEditing({ ...editing, notesText: e.target.value })} placeholder="notes" style={{ width: "100%", minHeight: 60, boxSizing: "border-box" }} />
                      <div style={{ display: "flex", gap: 6, marginTop: 6, justifyContent: "flex-end" }}>
                        <button className="btn" onClick={() => setEditing(null)}>cancel</button>
                        <button className="btn primary" disabled={busy || !editing.title.trim()} onClick={async () => {
                          const ok = await patch(it.id, {
                            title: editing.title.trim(), domain: editing.domain || "", owner: editing.owner || "",
                            tags: parseTags(editing.tagsText), notes: editing.notesText,
                          });
                          if (ok) setEditing(null);
                        }}>{busy ? "saving..." : "save"}</button>
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
            {visible.length === 0 && (
              <tr><td colSpan={8} className="muted" style={{ textAlign: "center", padding: 20 }}>no tracker items{closedCount ? " open" : ""}</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: 8, fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" }}>
        open first, then closed; most recently updated first. The same list as <code>cousin-tracker list --json</code>.
      </div>
    </div>
  );
}

// ============ SETTINGS ============
const SETTINGS_KEY = "console_settings_v1";
const SETTINGS_DEFAULTS = {
  accentHue: 355,
  accentChroma: 0.18,
  backgroundTone: 0.12,
  chatStreamSpeed: "normal",   // off | slow | normal | fast
  chatStreamEffect: "plain",   // plain | glitch | matrix | typewriter | boot
  fontScale: 100,
  showHidden: false,           // reveal cousins + loops marked hidden=true in cousin.toml
};

function loadSettings() {
  try { return { ...SETTINGS_DEFAULTS, ...(JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}) }; }
  catch (e) { return { ...SETTINGS_DEFAULTS }; }
}

function saveSettings(v) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(v)); }
  catch (e) { /* ignore - in-memory only */ }
}

function applySettings(s) {
  const r = document.documentElement;
  const hue = Number(s.accentHue) || 355;
  const chroma = Number(s.accentChroma) || 0.18;
  r.style.setProperty("--accent", `oklch(0.70 ${chroma} ${hue})`);
  r.style.setProperty("--accent-bright", `oklch(0.78 ${chroma} ${hue})`);
  r.style.fontSize = (Number(s.fontScale) || 100) + "%";
  window.__fwSettings = s; // exposed for chat streaming + visibility filters
  // Notify any subscribed component (App, CousinsView, LoopsTable) so the
  // 'show hidden' toggle applies without a polling round-trip.
  window.dispatchEvent(new CustomEvent("fw-settings-changed", { detail: s }));
}

// React hook: returns the current value of a settings key and re-renders
// the component when the operator updates Settings.
function useSetting(key) {
  const [v, setV] = React.useState(() => (window.__fwSettings && window.__fwSettings[key]));
  React.useEffect(() => {
    const h = (e) => setV(e.detail && e.detail[key]);
    window.addEventListener("fw-settings-changed", h);
    return () => window.removeEventListener("fw-settings-changed", h);
  }, [key]);
  return v;
}
window.useSetting = useSetting;
window.applySettings = applySettings;
window.SETTINGS_KEY = SETTINGS_KEY;

// Apply stored settings immediately on load
applySettings(loadSettings());

function AccountPanel({ auth, setAuth }) {
  const [me, setMe] = React.useState(auth || null);
  const [oldPw, setOldPw] = React.useState("");
  const [newPw, setNewPw] = React.useState("");
  const [newPw2, setNewPw2] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  React.useEffect(() => {
    fetchAuthMe().then(setMe);
  }, []);
  const submit = async () => {
    setMsg(null);
    if (newPw !== newPw2) { setMsg({ kind: "err", text: "new passwords differ" }); return; }
    if (newPw.length < 8) { setMsg({ kind: "err", text: "new password must be at least 8 chars" }); return; }
    setBusy(true);
    try {
      const { r, d } = await apiSend("POST", "/api/auth/change-password", { old_password: oldPw, new_password: newPw });
      if (r.ok && d.ok) {
        setMsg({ kind: "ok", text: `password updated for ${d.user}; this session stays valid.` });
        setOldPw(""); setNewPw(""); setNewPw2("");
      } else {
        setMsg({ kind: "err", text: d.error || `HTTP ${r.status}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };
  const logout = async () => {
    try { await apiSend("POST", "/api/auth/logout"); } catch (_e) {}
    const next = await fetchAuthMe();
    setMe(next);
    if (setAuth) setAuth(next);
  };
  const user = me?.user;
  const configured = !!(me && me.configured);
  const inputStyle = { padding: 6, border: "1px solid var(--line)", borderRadius: 3, background: "var(--bg-0)", color: "var(--fg-0)" };
  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="panel-hdr"><span className="title">account</span></div>
      <div className="panel-body" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
        {!configured ? (
          <div style={{ fontSize: 12, color: "var(--fg-3)", fontFamily: "var(--mono)" }}>
            auth is not configured: the network guard is the only boundary. Provision a user with
            <code style={{ marginLeft: 6 }}>cousin-console adduser &lt;name&gt;</code>
          </div>
        ) : (
          <>
            <div style={{ fontSize: 12, color: "var(--fg-3)", fontFamily: "var(--mono)", display: "flex", gap: 10, alignItems: "center" }}>
              <span>logged in as <strong style={{ color: "var(--fg-0)" }}>{user || "..."}</strong>
              {me?.users?.length > 1 && <span> · other users: {me.users.filter(u => u !== user).join(", ")}</span>}</span>
              <button className="btn ghost" onClick={logout} style={{ fontSize: 10, padding: "2px 8px", minHeight: 18 }}>log out</button>
            </div>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" }}>
              current password
              <input type="password" value={oldPw} onChange={e => setOldPw(e.target.value)} style={inputStyle} autoComplete="current-password" />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" }}>
              new password (min 8 chars)
              <input type="password" value={newPw} onChange={e => setNewPw(e.target.value)} style={inputStyle} autoComplete="new-password" />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" }}>
              retype new password
              <input type="password" value={newPw2} onChange={e => setNewPw2(e.target.value)} style={inputStyle} autoComplete="new-password" />
            </label>
            <div>
              <button className="btn" disabled={busy || !oldPw || !newPw || !newPw2} onClick={submit}>
                {busy ? "..." : "change password"}
              </button>
            </div>
            {msg && (
              <div style={{
                fontSize: 12, padding: 8, borderRadius: 3,
                background: msg.kind === "err" ? "oklch(from var(--err, #ef4444) l c h / 0.18)" : "oklch(from var(--ok, #22c55e) l c h / 0.18)",
                color: msg.kind === "err" ? "var(--err, #ef4444)" : "var(--ok, #22c55e)",
              }}>{msg.text}</div>
            )}
          </>
        )}
      </div>
    </div>
  );
}


function SettingsView({ auth, setAuth }) {
  const [s, setS] = React.useState(loadSettings);
  const update = (patch) => {
    const next = { ...s, ...patch };
    setS(next);
    saveSettings(next);
    applySettings(next);
  };
  const reset = () => {
    try { localStorage.removeItem(SETTINGS_KEY); } catch (_e) {}
    setS({ ...SETTINGS_DEFAULTS });
    applySettings(SETTINGS_DEFAULTS);
  };
  const hues = [0, 30, 60, 120, 180, 210, 240, 270, 300, 330, 355];
  return (
    <div className="wrap-pad" style={{ maxWidth: 720 }}>
      <div className="panel">
        <div className="panel-hdr"><span className="title">cosmetic</span>
          <span style={{ flex: 1 }} />
          <button className="btn" onClick={reset}>reset defaults</button></div>
        <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 18, padding: 18 }}>
          <div>
            <div className="eyebrow" style={{ marginBottom: 8 }}>accent hue</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
              {hues.map(h => (
                <button key={h}
                  onClick={() => update({ accentHue: h })}
                  title={`hue ${h}`}
                  style={{
                    width: 28, height: 28, borderRadius: 3,
                    border: s.accentHue === h ? "2px solid var(--fg-0)" : "1px solid var(--line)",
                    background: `oklch(0.70 ${s.accentChroma} ${h})`,
                    cursor: "pointer",
                  }} />
              ))}
            </div>
            <input type="range" min="0" max="360" value={s.accentHue}
              onChange={e => update({ accentHue: Number(e.target.value) })}
              style={{ width: "100%" }} />
            <div style={{ fontSize: 11, fontFamily: "var(--mono)", color: "var(--fg-3)" }}>{s.accentHue}</div>
          </div>

          <div>
            <div className="eyebrow" style={{ marginBottom: 8 }}>accent saturation</div>
            <input type="range" min="0" max="0.28" step="0.01" value={s.accentChroma}
              onChange={e => update({ accentChroma: Number(e.target.value) })}
              style={{ width: "100%" }} />
            <div style={{ fontSize: 11, fontFamily: "var(--mono)", color: "var(--fg-3)" }}>{s.accentChroma.toFixed(2)}</div>
          </div>

          <div>
            <div className="eyebrow" style={{ marginBottom: 8 }}>chat stream speed</div>
            <div className="radio-row">
              {["off", "slow", "normal", "fast"].map(v => (
                <button key={v} className={s.chatStreamSpeed === v ? "sel" : ""}
                  onClick={() => update({ chatStreamSpeed: v })}>{v}</button>
              ))}
            </div>
          </div>

          <div>
            <div className="eyebrow" style={{ marginBottom: 8 }}>chat stream effect</div>
            <div className="radio-row">
              {[
                ["plain", "word-by-word"],
                ["glitch", "block-glyph scramble"],
                ["matrix", "cascading katakana"],
                ["typewriter", "char-by-char"],
                ["boot", "bios-style sparse"],
              ].map(([v, desc]) => (
                <button key={v} className={s.chatStreamEffect === v ? "sel" : ""}
                  title={desc}
                  onClick={() => update({ chatStreamEffect: v })}>{v}</button>
              ))}
            </div>
            <div style={{ fontSize: 10, color: "var(--fg-3)", fontFamily: "var(--mono)", marginTop: 4 }}>
              plain = word reveal · glitch = block-glyph scramble · matrix = katakana rain · typewriter = char-by-char · boot = sparse chunks with dots.
            </div>
          </div>

          <div>
            <div className="eyebrow" style={{ marginBottom: 8 }}>font scale · {s.fontScale}%</div>
            <input type="range" min="85" max="140" step="5" value={s.fontScale}
              onChange={e => update({ fontScale: Number(e.target.value) })}
              style={{ width: "100%" }} />
          </div>
        </div>
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-hdr"><span className="title">visibility</span></div>
        <div className="panel-body" style={{ padding: 18 }}>
          <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", fontSize: 13 }}>
            <input type="checkbox" checked={!!s.showHidden}
              onChange={e => update({ showHidden: e.target.checked })} />
            <span>show hidden cousins + loops</span>
          </label>
          <div style={{ marginTop: 6, fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--mono)" }}>
            Cousins or loops marked with <code>hidden = true</code> in their <code>cousin.toml</code> are filtered out by default. Toggle this on to reveal them (they get a "hidden" chip when shown). Use the inspector to flip the flag per item.
          </div>
        </div>
      </div>

      <div style={{ marginTop: 12, fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" }}>
        Settings persist in this browser's local storage. Applied live via CSS variables.
      </div>

      <RestartPanel auth={auth} setAuth={setAuth} />

      {/* settings.panels: a package's own settings panels (ui.jsx
          registerSlot from its jsx file), props { auth, setAuth } */}
      <Slot name="settings.panels" auth={auth} setAuth={setAuth} />
    </div>
  );
}

function RestartPanel({ auth, setAuth }) {
  const [confirm, setConfirm] = React.useState(null);  // "console"
  const [busy, setBusy] = React.useState(null);
  const [msg, setMsg] = React.useState(null);

  React.useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(null), 4000);
    return () => clearTimeout(t);
  }, [confirm]);
  React.useEffect(() => {
    if (!msg) return;
    const t = setTimeout(() => setMsg(null), 8000);
    return () => clearTimeout(t);
  }, [msg]);

  const restartConsole = async () => {
    if (confirm !== "console") { setConfirm("console"); return; }
    setConfirm(null);
    setBusy("console");
    try {
      const { r, d } = await apiSend("POST", "/api/admin/restart/framework");
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      if (d.supervised === false) {
        setMsg({ ok: false, text: "the console is not supervised: this restart is a stop. Start it again by hand." });
      } else {
        setMsg({ ok: true, text: "console restart kicked off - reconnecting..." });
      }
      // Broadcast a restart-in-flight event so the shell banner can show.
      window.dispatchEvent(new CustomEvent("fw-restart", { detail: { target: "console", etaSeconds: d.eta_seconds || 4 } }));
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally {
      setBusy(null);
    }
  };

  const btn = (key, label, onClick, tone) => (
    <button className={`btn ${tone || ""}`} onClick={onClick}
            disabled={busy === key} style={{ minWidth: 160 }}>
      {busy === key ? "restarting..." : confirm === key ? "click again to confirm" : label}
    </button>
  );

  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="panel-hdr">
        <span className="title">process control</span>
        <span style={{ color: "var(--fg-3)", fontSize: 11, marginLeft: 8 }}>
          restart the console process; cousins restart from their inspector
        </span>
      </div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10, padding: 14 }}>
        {msg && (
          <div style={{ padding: "6px 10px", fontSize: 11, fontFamily: "var(--mono)",
                        color: msg.ok ? "var(--green)" : "var(--red)",
                        background: msg.ok ? "oklch(from var(--green) l c h / 0.08)"
                                           : "oklch(from var(--red) l c h / 0.08)",
                        borderRadius: 3 }}>
            {msg.text}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          {btn("console", "restart console", restartConsole, "danger")}
          <span style={{ fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--mono)" }}>
            ~4s downtime · sessions do not survive it (you will log in again) · needs a supervisor that restarts the unit
          </span>
        </div>
        <div style={{ fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--mono)",
                       padding: "8px 10px", borderTop: "1px solid var(--line-soft)",
                       marginTop: 4 }}>
          Per-cousin restart lives in the Cousins tab: open a cousin's inspector
          drawer; the <code>restart</code> button sits next to start/stop.
        </div>
      </div>
      <AccountPanel auth={auth} setAuth={setAuth} />
    </div>
  );
}


// ============ HOST (overview) ============
// The fleet at a glance: one sentence of health, the numbers that are
// compared (running, tokens, jobs, loops), every cousin in one table with
// what needs the operator first, and a rail of what happened (jobs, loop
// fires, flips seen while the page is open) beside the host's meters.
// Every value comes from a route the console already serves; a column
// the fleet rows do not carry is left out rather than guessed.

// ---- fleet helpers (pure: no React, no fetch; tests run them in node) ----
const FLEET_RUNNER_WORDS = {
  idle: "idle", running: "working", waiting_permission: "needs you",
  rate_limited: "rate limited", rolling_over: "rolling over",
  errored: "errored", stopped: "stopped",
};
const FLIP_NEVER = ["never", "off", "none", "no"];

// The lane a row runs on, read from the row: the runner's own kind (the
// head of its stream names it), a hive node, a worker, or the tmux lane
// when the row names a tmux session and has no runner.
function fleetRunnerKind(c) {
  if (!c) return "-";
  if (c.remote || c.type === "remote") return "remote";
  if (c.runner) return c.runner.kind || "runner";
  if (c.type === "worker") return "worker";
  return c.tmuxSession ? "tmux" : "-";
}

// What asks for the operator: null, or {level, why}. "needs" is a cousin
// waiting on a person; "warn" is worth a look but not blocked. A stopped
// cousin is never flagged: stopping is the operator's decision.
function fleetAttention(c) {
  // a runner the supervisor left down `failing` was not stopped by anyone:
  // it waits on a person, with the supervisor's reason
  if (c && c.supervisor && c.supervisor.state === "failing" && !c.remote)
    return { level: "needs", why: "failing: " + (c.supervisor.reason || "left down by the supervisor") };
  if (!c || c.status !== "running") return null;
  if (c.loginRequired && !c.remote) return { level: "needs", why: fleetLoginWhy(c.loginRequired) };
  if (c.attention) return { level: "needs", why: "the pane shows \"" + c.attention + "\"" };
  const r = c.runner;
  if (r && r.alive) {
    if (r.state === "waiting_permission") return { level: "needs", why: "waiting for a permission" };
    if (r.state === "errored") return { level: "needs", why: "the runner errored" };
    if (r.state === "rate_limited") return { level: "warn", why: "rate limited" };
  }
  if (c.chat === "down") return { level: "warn", why: "chat server down" };
  return null;
}

// A runner waiting on data/login-required.json: what it waits for and the
// line that fixes it (the row carries reason, action and since only).
function fleetLoginWhy(l) {
  const what = l && l.reason === "billing" ? "billing stopped" : "login required";
  return l && l.action ? what + ": " + l.action : what;
}

// The local cousins waiting for a login or billing fix, running or not: a
// stopped runner still waits for it at its next start.
function fleetLoginWaits(cousins) {
  return (cousins || []).filter(c => !c.remote && c.loginRequired);
}

// The row's state in words, with the colour that goes with them.
function fleetState(c) {
  if (!c) return { word: "-", tone: "gray", pulse: false };
  if (c.remote) {
    const s = c.remoteState || (c.online ? "online" : "offline");
    return { word: s, tone: s === "online" ? "green" : s === "revoked" ? "red" : s === "pending" ? "amber" : "gray", pulse: false };
  }
  if (c.status !== "running") return { word: "stopped", tone: "gray", pulse: false };
  const att = fleetAttention(c);
  if (att && att.level === "needs") {
    return { word: c.runner && c.runner.state === "errored" ? "errored" : "needs you",
             tone: c.runner && c.runner.state === "errored" ? "red" : "amber", pulse: false };
  }
  if (c.runner) {
    const st = c.runner.state;
    return { word: FLEET_RUNNER_WORDS[st] || st || "starting",
             tone: st === "rate_limited" ? "amber" : "green",
             pulse: st === "running" || st === "rolling_over" };
  }
  if (c.type === "worker") return { word: "enrolled", tone: "green", pulse: false };
  return c.active ? { word: "working", tone: "green", pulse: true } : { word: "idle", tone: "green", pulse: false };
}

// Needs-you first, then warnings, then the running, then the rest; the
// server's order within each group.
function fleetRank(c) {
  const a = fleetAttention(c);
  if (a) return a.level === "needs" ? 0 : 1;
  return c && c.status === "running" ? 2 : 3;
}
function fleetOrder(cousins) {
  return (cousins || []).map((c, i) => [c, i])
    .sort((x, y) => fleetRank(x[0]) - fleetRank(y[0]) || x[1] - y[1])
    .map(p => p[0]);
}

function fleetPad2(n) { return String(n).padStart(2, "0"); }

// "7h 24m", "12m", "40s": how far away a moment is.
function fleetIn(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return m ? h + "h " + m + "m" : h + "h";
}

// "30m", "1h", "2h 30m": a cadence in seconds, read at a glance.
function fleetEvery(sec) {
  const n = Number(sec);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 60) return n + "s";
  if (n < 3600) return Math.round(n / 60) + "m";
  const h = Math.floor(n / 3600), m = Math.round((n % 3600) / 60);
  return m ? h + "h " + m + "m" : h + "h";
}

// A row's flip_at ("HH:MM", the cousin's own [lifecycle] value) as the
// next time it comes round on this browser's clock: {at, ts, inSec};
// {never: true} for an explicit opt-out; null when the row carries none
// (the cousin then flips at the install default, which no route serves).
function nextFlip(flipAt, now) {
  if (flipAt == null) return null;
  const v = String(flipAt).trim().toLowerCase();
  if (!v) return null;
  if (FLIP_NEVER.includes(v)) return { never: true };
  const m = /^(\d{1,2}):(\d{2})$/.exec(v);
  if (!m) return null;
  const h = Number(m[1]), mi = Number(m[2]);
  if (h > 23 || mi > 59) return null;
  const d = new Date(now);
  d.setHours(h, mi, 0, 0);
  if (d.getTime() <= now) d.setDate(d.getDate() + 1);
  return { at: fleetPad2(h) + ":" + fleetPad2(mi), ts: d.getTime(), inSec: Math.round((d.getTime() - now) / 1000) };
}

// The fleet's next flip among the running cousins that set one:
// {at, inSec, slugs, onDefault}, or null when none sets a time.
// onDefault counts the running cousins that take the install default.
function fleetNextFlip(cousins, now) {
  let best = null, onDefault = 0;
  for (const c of cousins || []) {
    if (c.remote || c.type === "worker" || c.status !== "running") continue;
    const f = nextFlip(c.flipAt, now);
    if (!f) { if (c.flipAt == null || String(c.flipAt).trim() === "") onDefault += 1; continue; }
    if (f.never) continue;
    if (!best || f.ts < best.ts) best = { at: f.at, ts: f.ts, inSec: f.inSec, slugs: [c.slug] };
    else if (f.ts === best.ts) best.slugs.push(c.slug);
  }
  if (best) best.onDefault = onDefault;
  return best;
}

// One sentence of fleet health, and the counts behind it.
function fleetHealth(cousins, now) {
  const local = (cousins || []).filter(c => !c.remote);
  const running = local.filter(c => c.status === "running");
  const needs = running.filter(c => (fleetAttention(c) || {}).level === "needs");
  const warn = running.filter(c => (fleetAttention(c) || {}).level === "warn");
  const parts = [running.length + " of " + local.length + " running"];
  parts.push(needs.length ? needs.length + (needs.length === 1 ? " needs" : " need") + " you" : "nothing needs you");
  if (warn.length) parts.push(warn.length + (warn.length === 1 ? " warning" : " warnings"));
  const flip = fleetNextFlip(local, now);
  if (flip) parts.push("next flip " + flip.at + ", in " + fleetIn(flip.inSec));
  return { text: parts.join(" · "), running: running.length, total: local.length,
           needs: needs.length, warn: warn.length, nextFlip: flip };
}
// ---- end fleet helpers ----

// Flips seen while the page is open: the shell re-dispatches the SSE
// `cousin-flip` event on window; the overview keeps the last few for its
// activity rail. Nothing is stored anywhere else.
const FLEET_FLIPS = [];
window.addEventListener("fw-cousin-flip", (e) => {
  const d = e.detail || {};
  if (!d.slug || !["scheduled", "started", "complete", "failed", "cancelled"].includes(d.phase)) return;
  FLEET_FLIPS.unshift({ ts: Date.now(), slug: d.slug, phase: d.phase, gen: d.new_generation, error: d.error });
  FLEET_FLIPS.length = Math.min(FLEET_FLIPS.length, 20);
});

function fleetEvents(jobs, fires, flips, now) {
  const out = [];
  for (const j of jobs || []) {
    const at = Date.parse(j.finished_at || j.started_at || "");
    if (!Number.isFinite(at)) continue;
    const tone = j.status === "failed" ? "red" : j.status === "running" ? "accent" : j.status === "done" ? "green" : "gray";
    out.push({ key: "j" + j.id, ts: at, who: j.spawned_by, text: j.title, word: j.status === "running" ? "job started" : "job " + j.status, tone });
  }
  for (const [i, f] of (fires || []).entries()) {
    out.push({ key: "f" + i + f.cousin + f.loop, ts: now - (Number(f.ago) || 0) * 1000, who: f.cousin,
               text: f.loop === "context-heartbeat" ? "context heartbeat" : f.loop, word: "fired", tone: "gray" });
  }
  for (const [i, f] of (flips || []).entries()) {
    out.push({ key: "p" + i + f.ts, ts: f.ts, who: f.slug,
               text: f.phase === "complete" && f.gen != null ? "flipped to generation " + f.gen : "flip " + f.phase,
               word: "flip", tone: f.phase === "failed" ? "red" : f.phase === "complete" ? "green" : "accent" });
  }
  return out.sort((a, b) => b.ts - a.ts).slice(0, 16);
}

function fmtClock(ts) {
  const d = new Date(ts);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function HostView({ onOpen }) {
  const showHidden = (window.useSetting && window.useSetting("showHidden")) || false;
  const [h, setH] = React.useState(HOST_SEED);
  const [allCousins, setAllCousins] = React.useState([]);
  const [allFires, setAllFires] = React.useState([]);
  const [allLoops, setAllLoops] = React.useState([]);
  const [daemon, setDaemon] = React.useState(null);
  const [jobs, setJobs] = React.useState([]);
  const [now, setNow] = React.useState(Date.now());
  const cousins = React.useMemo(
    () => (allCousins || []).filter(c => showHidden || !c.hidden),
    [allCousins, showHidden]);
  const hiddenCousinSlugs = React.useMemo(
    () => new Set((allCousins || []).filter(c => c.hidden).map(c => c.slug)),
    [allCousins]);
  const loops = React.useMemo(
    () => (allLoops || []).filter(l => showHidden || !hiddenCousinSlugs.has(l.cousin)),
    [allLoops, showHidden, hiddenCousinSlugs]);
  const fires = React.useMemo(
    () => (allFires || []).filter(f => showHidden || !hiddenCousinSlugs.has(f.cousin)),
    [allFires, showHidden, hiddenCousinSlugs]);
  const visibleJobs = React.useMemo(
    () => (jobs || []).filter(j => showHidden || !hiddenCousinSlugs.has(j.spawned_by)),
    [jobs, showHidden, hiddenCousinSlugs]);

  React.useEffect(() => {
    let cancelled = false;
    async function pull() {
      const [host, cs, loopsData, recent, jobsData] = await Promise.all([
        fetchHost(),
        fetchCousins(),
        fetchLoopsFull(),
        apiGet("/api/loops/recent"),
        apiGet("/api/jobs?since_hours=24"),
      ]);
      if (cancelled) return;
      if (host) setH(host);
      setAllCousins(cs || []);
      setAllLoops(loopsData.loops || []);
      setDaemon(loopsData.daemon || (recent && recent.daemon) || null);
      setAllFires((recent && recent.fires) || []);
      if (jobsData) setJobs(jobsData.jobs || []);
      setNow(Date.now());
    }
    pull();
    const id = setInterval(pull, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const cpuPct = h.cpu?.pct ?? 0;
  const memPct = h.mem?.total ? (h.mem.used / h.mem.total) * 100 : 0;
  const diskPct = h.disk?.total ? (h.disk.used / h.disk.total) * 100 : 0;
  const running = cousins.filter(c => c.status === "running");
  const totalTokensToday = cousins.reduce((s, c) => s + (c.tokensSpent || 0), 0);
  const health = fleetHealth(cousins, now);
  const ordered = fleetOrder(cousins);
  const jobsRunning = visibleJobs.filter(j => j.status === "running").length;
  const jobsFailed = visibleJobs.filter(j => j.status === "failed").length;
  const events = fleetEvents(visibleJobs, fires, FLEET_FLIPS, now);

  return (
    <div className="wrap-pad ov">
      <div className="ov-health" data-fleet-health>
        <span className={"led " + (health.needs ? "amber" : health.warn ? "amber" : "green")} />
        <span>{health.text}</span>
      </div>

      {fleetLoginWaits(cousins).length > 0 && (
        <div className="ov-login" data-login-required>
          {fleetLoginWaits(cousins).map(c => (
            <div key={c.slug} className="ov-login-row" title={c.loginRequired.since ? `since ${c.loginRequired.since}` : undefined}>
              <span className="led amber" />
              <span><b>@{c.slug}</b> {c.loginRequired.reason === "billing" ? "billing stopped" : "waits for a login"}{c.account ? ` on account ${c.account}` : ""}</span>
              {c.loginRequired.action && <code className="ov-login-action">{c.loginRequired.action}</code>}
            </div>
          ))}
        </div>
      )}

      <div className="ov-stats" data-fleet-stats>
        <div className="ov-stat">
          <div className="eyebrow">running</div>
          <div className="ov-stat-v"><span className="ov-big">{running.length}</span><span className="ov-of">of {cousins.length} cousins</span></div>
          <div className={"ov-stat-s" + (health.needs ? " amber" : "")}>{health.needs ? `${health.needs} need${health.needs === 1 ? "s" : ""} you` : "nothing needs you"}</div>
        </div>
        <div className="ov-stat">
          <div className="eyebrow">tokens today</div>
          <div className="ov-stat-v"><span className="ov-big">{fmtTokens(totalTokensToday)}</span></div>
          <div className="ov-stat-s">all cousins</div>
        </div>
        <div className="ov-stat">
          <div className="eyebrow">jobs, 24h</div>
          <div className="ov-stat-v"><span className="ov-big">{jobsRunning}</span><span className="ov-of">running</span></div>
          <div className={"ov-stat-s" + (jobsFailed ? " red" : "")}>{jobsFailed} failed · {visibleJobs.length} in all</div>
        </div>
        <div className="ov-stat">
          <div className="eyebrow">loops</div>
          <div className="ov-stat-v"><span className="ov-big">{loops.length}</span><span className="ov-of">loops</span></div>
          <div className={"ov-stat-s" + (daemon && daemon.ok === false ? " amber" : "")}>
            {daemon && daemon.ok === false ? daemon.message : `${fires.length} recent fires`}
          </div>
        </div>
      </div>

      <div className="ov-grid">
        <section className="panel ov-fleet">
          <div className="panel-hdr">
            <span className="title">Fleet</span>
            <span>{running.length}/{cousins.length} running</span>
            <span style={{ flex: 1 }} />
            <span title="the order: needs you, warnings, running, stopped">needs you first</span>
          </div>
          <div className="table-scroll">
            <table className="data fleet-table">
              <thead>
                <tr>
                  <th>state</th>
                  <th>cousin</th>
                  <th>runner</th>
                  <th>model</th>
                  <th>next flip</th>
                  <th>beat</th>
                  <th>operator</th>
                  <th className="num">tokens today</th>
                </tr>
              </thead>
              <tbody>
                {ordered.map(c => {
                  const st = fleetState(c);
                  const att = fleetAttention(c);
                  const kind = fleetRunnerKind(c);
                  const flip = nextFlip(c.flipAt, now);
                  const openable = onOpen && c.status === "running" && c.type !== "worker" && !c.remote;
                  return (
                    <tr key={c.slug} data-fleet-row={c.slug}
                        className={"fleet-row" + (c.status !== "running" ? " off" : "") + (openable ? " link" : "")}
                        onClick={openable ? () => onOpen(c.slug) : undefined}
                        title={openable ? `open the chat with @${c.slug}` : undefined}>
                      <td data-label="state">
                        <span className={"fleet-state tone-" + st.tone}>
                          <span className={"led " + st.tone + (st.pulse ? " pulse" : "")} />
                          {st.word}
                        </span>
                      </td>
                      <td data-label="cousin" className="fleet-who">
                        <div><span className="fleet-name">{c.name || c.slug}</span> <span className="fleet-slug">@{c.slug}</span>{c.hidden ? <span className="fleet-slug"> · hidden</span> : null}</div>
                        {att ? <div className="fleet-why" title={att.why}>{att.why}</div>
                          : c.role ? <div className="fleet-role" title={c.role}>{c.role}</div> : null}
                      </td>
                      <td data-label="runner">
                        <div className="mono">{kind}</div>
                        <div className="fleet-sub">{c.runner ? (c.runner.alive ? "" : "(not running)") : c.chat === "ok" ? `chat :${c.port}` : c.chat === "down" ? "chat down" : c.chat === "none" ? "no chat server" : (c.chat || "")}</div>
                      </td>
                      <td data-label="model">
                        <div className="mono">{c.model || "-"}</div>
                        {c.effort && <div className="fleet-sub">{c.effort}</div>}
                      </td>
                      <td data-label="next flip" title={flip ? (flip.never ? "flip_at = never" : `[lifecycle] flip_at = ${c.flipAt}`) : "no flip_at of its own: the install default applies (config/harness.toml default_flip_at)"}>
                        {flip && !flip.never ? (
                          <>
                            <div className="mono">{flip.at}</div>
                            <div className="fleet-sub">in {fleetIn(flip.inSec)}</div>
                          </>
                        ) : flip && flip.never ? <span className="muted">never</span>
                          : <span className="muted">{c.type === "worker" || c.remote ? "-" : "default"}</span>}
                      </td>
                      <td data-label="beat" className="mono" title={c.heartbeat != null ? `${c.heartbeat}s` : ""}>{c.heartbeat != null ? fleetEvery(c.heartbeat) : "-"}</td>
                      <td data-label="operator" className="muted">{c.operator || "-"}</td>
                      <td data-label="tokens today" className="num">{fmtTokens(c.tokensSpent || 0)}</td>
                    </tr>
                  );
                })}
                {cousins.length === 0 && (
                  <tr><td colSpan={8} className="muted" style={{ textAlign: "center", padding: 24 }}>no cousins registered</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="ov-rail">
          <section className="panel">
            <div className="panel-hdr">
              <span className="title">{h.host || "host"}</span>
              <span className="mono">{h.kernel}</span>
              <span style={{ flex: 1 }} />
              <span className="fleet-state tone-green" title="host uptime"><span className="led green" /> up {fmtDuration(h.uptime || 0)}</span>
            </div>
            <div className="panel-body ov-meters">
              <Meter label="cpu" value={num(cpuPct).toFixed(1) + "%"}
                     sub={`load ${num(h.cpu?.load1).toFixed(2)} ${num(h.cpu?.load5).toFixed(2)} ${num(h.cpu?.load15).toFixed(2)}`}
                     pct={num(cpuPct)} />
              <Meter label="memory" value={h.mem?.total ? (num(h.mem.used).toFixed(1) + " / " + h.mem.total + " GB") : "-"}
                     sub={h.mem ? `cached ${num(h.mem.cached).toFixed(1)} GB` : "-"} pct={num(memPct)} />
              <Meter label="disk /" value={h.disk?.total ? (num(h.disk.used).toFixed(0) + " / " + h.disk.total + " GB") : "-"}
                     sub={h.disk?.total ? `free ${(num(h.disk.total) - num(h.disk.used)).toFixed(0)} GB` : "-"} pct={num(diskPct)} />
              <Meter label="net" value={num(h.net?.rx).toFixed(2) + " MB/s rx"}
                     sub={num(h.net?.tx).toFixed(2) + " MB/s tx" + (h.net?.rx_total_gb != null ? ` · ${num(h.net.rx_total_gb).toFixed(1)}/${num(h.net.tx_total_gb).toFixed(1)} GB life` : "")}
                     pct={Math.min(100, Math.max(num(h.net?.rx), num(h.net?.tx)) * 10)} />
              {h.console_uptime != null && <div className="ov-note">console up {fmtDuration(h.console_uptime)}</div>}
            </div>
          </section>

          <section className="panel ov-activity">
            <div className="panel-hdr">
              <span className="title">Activity</span>
              <span>jobs, loop fires, flips</span>
            </div>
            <div className="ov-events" data-fleet-events>
              {events.map(ev => (
                <div key={ev.key} className="ov-event">
                  <span className="ov-event-t mono" title={new Date(ev.ts).toLocaleString()}>{fmtClock(ev.ts)}</span>
                  <span className={"ov-event-dot tone-" + ev.tone} />
                  <div className="ov-event-b">
                    <span className="ov-event-who">{ev.who}</span> <span className="ov-event-w">{ev.word}</span>
                    <div className="ov-event-x" title={ev.text}>{ev.text}</div>
                  </div>
                </div>
              ))}
              {events.length === 0 && (
                <div className="ov-note" style={{ padding: "14px 16px" }}>nothing yet: no jobs in the last 24 hours and no loop fires; the loops daemon fires loops at their configured schedule</div>
              )}
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}

// A host meter: the label and number on one line, the sub-line and a
// thin bar under them. No box of its own: it sits on the panel.
function Meter({ label, value, sub, pct }) {
  const tone = pct > 88 ? "crit" : pct > 70 ? "warn" : "";
  return (
    <div className="ov-meter">
      <div className="ov-meter-h"><span className="eyebrow">{label}</span><span className="ov-meter-v">{value}</span></div>
      <Bar pct={pct} tone={tone} />
      <div className="ov-meter-s">{sub}</div>
    </div>
  );
}

Object.assign(window, {
  JobsView, MemoryView, LoopsView, TokensView, TrackerView, SettingsView, AccountPanel, RestartPanel,
  HostView, CousinTag, Field, Stat, TRACKER_STATES,
  fleetRunnerKind, fleetAttention, fleetState, fleetOrder, fleetHealth, fleetNextFlip, nextFlip, fleetIn,
  fleetLoginWhy, fleetLoginWaits,
});
