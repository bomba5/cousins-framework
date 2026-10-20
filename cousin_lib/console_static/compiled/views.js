function CousinTag({ slug }) {
  return /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)", fontWeight: 500 } }, "@", slug);
}
const ARTIFACT_LIST_CAP = 100;
function JobsView() {
  const [tab, setTab] = React.useState("jobs");
  const [artifactRows, setArtifactRows] = React.useState(null);
  const [hiddenSlugs, setHiddenSlugs] = React.useState(null);
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  React.useEffect(() => {
    let cancelled = false;
    apiGet("/api/artifacts").then((d) => {
      if (!cancelled) setArtifactRows(d && d.artifacts ? d.artifacts : []);
    });
    return () => {
      cancelled = true;
    };
  }, [tab]);
  React.useEffect(() => {
    let cancelled = false;
    const pull = () => apiGet("/api/cousins").then((d) => {
      if (cancelled || !d || !Array.isArray(d.cousins)) return;
      const next = d.cousins.filter((c) => c.hidden).map((c) => c.slug).sort();
      setHiddenSlugs((cur) => cur && [...cur].sort().join() === next.join() ? cur : new Set(next));
    });
    pull();
    const id = setInterval(pull, 1e4);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);
  const artifactCount = artifactRows && hiddenSlugs ? visibleArtifacts(artifactRows, hiddenSlugs, showHidden).length : null;
  const tabs = /* @__PURE__ */ React.createElement("div", { className: "pane-tabs", role: "tablist", "data-jobs-tabs": true, style: { marginBottom: 12 } }, [["jobs", "jobs"], ["artifacts", "artifacts" + (artifactCount ? ` (${artifactRows.length >= ARTIFACT_LIST_CAP ? artifactCount + "+" : artifactCount})` : "")]].map(([key, label]) => /* @__PURE__ */ React.createElement(
    "button",
    {
      key,
      className: "btn " + (tab === key ? "active" : "ghost"),
      role: "tab",
      "aria-selected": tab === key,
      "data-jobs-tab": key,
      onClick: () => setTab(key)
    },
    label
  )));
  if (tab === "artifacts") {
    return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad" }, tabs, /* @__PURE__ */ React.createElement(ArtifactsPanel, { hiddenSlugs, showHidden }));
  }
  return /* @__PURE__ */ React.createElement(JobsList, { tabs, hiddenSlugs, showHidden });
}
function visibleArtifacts(rows, hiddenSlugs, showHidden) {
  if (showHidden) return rows;
  return hiddenSlugs ? rows.filter((r) => !hiddenSlugs.has(r.created_by)) : [];
}
function visibleJobs(rows, hiddenSlugs, showHidden) {
  if (showHidden) return rows;
  return hiddenSlugs ? rows.filter((j) => !hiddenSlugs.has(j.spawned_by)) : [];
}
function JobsList({ tabs, hiddenSlugs, showHidden }) {
  const [allJobs, setJobs] = React.useState([]);
  const jobs = React.useMemo(
    () => visibleJobs(allJobs, hiddenSlugs, showHidden),
    [allJobs, hiddenSlugs, showHidden]
  );
  const hiddenJobs = hiddenSlugs ? allJobs.length - jobs.length : 0;
  const [filter, setFilter] = React.useState("last24h");
  const [spawnedBy, setSpawnedBy] = React.useState("all");
  const [kind, setKind] = React.useState("all");
  const [openJob, setOpenJob] = React.useState(null);
  React.useEffect(() => {
    if (showHidden || !hiddenSlugs) return;
    if (hiddenSlugs.has(spawnedBy)) setSpawnedBy("all");
    setOpenJob((o) => o && hiddenSlugs.has(o.spawned_by) ? null : o);
  }, [hiddenSlugs, showHidden, spawnedBy]);
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      try {
        const params = new URLSearchParams();
        if (filter === "active") params.set("active_only", "1");
        else if (filter === "done") params.set("status", "done");
        else if (filter === "failed") params.set("status", "failed");
        else if (filter === "lost") params.set("status", "lost");
        else if (filter === "last24h") params.set("since_hours", "24");
        if (spawnedBy && spawnedBy !== "all") params.set("spawned_by", spawnedBy);
        if (kind && kind !== "all") params.set("kind", kind);
        const qs = params.toString();
        const r = await fetch("/api/jobs" + (qs ? "?" + qs : ""), { cache: "no-store" });
        const d = await r.json();
        if (!cancelled) setJobs(d.jobs || []);
      } catch (e) {
      }
    };
    pull();
    const id = setInterval(pull, 2e3);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [filter, spawnedBy, kind]);
  const spawnedByOptions = React.useMemo(() => {
    const set = /* @__PURE__ */ new Set();
    for (const j of jobs) if (j.spawned_by) set.add(j.spawned_by);
    if (spawnedBy !== "all") set.add(spawnedBy);
    return ["all", ...[...set].sort()];
  }, [jobs, spawnedBy]);
  const kindOptions = React.useMemo(() => {
    const set = /* @__PURE__ */ new Set();
    for (const j of jobs) if (j.kind) set.add(j.kind);
    if (kind !== "all") set.add(kind);
    return ["all", ...[...set].sort()];
  }, [jobs, kind]);
  const counts = {
    running: jobs.filter((j) => j.status === "running").length,
    done: jobs.filter((j) => j.status === "done").length,
    failed: jobs.filter((j) => j.status === "failed").length,
    lost: jobs.filter((j) => j.status === "lost").length
  };
  const fmtAge = (started, ended) => {
    if (!started) return "-";
    const s = new Date(started).getTime();
    const e = ended ? new Date(ended).getTime() : Date.now();
    const sec = Math.max(0, Math.round((e - s) / 1e3));
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.round(sec / 60)}m`;
    return `${(sec / 3600).toFixed(1)}h`;
  };
  const fmtStart = (started) => {
    if (!started) return "-";
    const d = new Date(started);
    const now = /* @__PURE__ */ new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    if (sameDay) return `${hh}:${mm}`;
    const mo = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${mo}-${dd} ${hh}:${mm}`;
  };
  const cancelJob = async (id) => {
    if (!confirm(`Cancel job #${id}? This will SIGTERM its process if known.`)) return;
    try {
      const { r, d } = await apiSend("POST", `/api/jobs/${id}`, { status: "cancelled" });
      if (!r.ok) alert("cancel failed: " + (d.error || r.status));
    } catch (e) {
      alert("cancel error: " + e.message);
    }
  };
  const deleteJob = async (id) => {
    if (!confirm(`Delete job #${id}? This removes the row and its console-minted log file.`)) return;
    try {
      const { r, d } = await apiSend("DELETE", `/api/jobs/${id}`);
      if (!r.ok) alert("delete failed: " + (d.error || r.status));
    } catch (e) {
      alert("delete error: " + e.message);
    }
  };
  const [tails, setTails] = React.useState({});
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      const running = jobs.filter((j) => j.status === "running" && j.log_path);
      const next = {};
      for (const j of running) {
        try {
          const r = await fetch(`/api/jobs/${j.id}/log?lines=10`, { cache: "no-store" });
          const d = await r.json();
          if (d.ok) next[j.id] = d.log || "";
        } catch (_e) {
        }
      }
      if (!cancelled) setTails(next);
    };
    pull();
    const id = setInterval(pull, 3e3);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [jobs.map((j) => j.id + j.status).join(",")]);
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad" }, tabs, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 14, marginBottom: 16 } }, /* @__PURE__ */ React.createElement(
    "div",
    {
      style: { display: "flex", gap: 14, fontFamily: "var(--mono)", fontSize: 12, color: "var(--fg-2)" },
      title: "counts reflect the current filters, not all history"
    },
    /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, /* @__PURE__ */ React.createElement("b", null, counts.running), " running"),
    /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("b", null, counts.done), " done"),
    /* @__PURE__ */ React.createElement("span", { style: { color: "var(--red, #d24)" } }, /* @__PURE__ */ React.createElement("b", null, counts.failed), " failed"),
    counts.lost > 0 && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--amber, #c90)" } }, /* @__PURE__ */ React.createElement("b", null, counts.lost), " lost"),
    /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "(in view)"),
    hiddenJobs > 0 && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" }, "data-jobs-hidden": true }, "· ", hiddenJobs, " from hidden cousins")
  ), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("div", { className: "radio-row", style: { display: "flex", gap: 4 } }, ["active", "last24h", "done", "failed", "lost", "all"].map((f) => /* @__PURE__ */ React.createElement("button", { key: f, className: filter === f ? "sel" : "", onClick: () => setFilter(f) }, f))), /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: spawnedBy, onChange: (e) => setSpawnedBy(e.target.value), title: "filter by cousin" }, spawnedByOptions.map((s) => /* @__PURE__ */ React.createElement("option", { key: s, value: s }, s))), /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: kind, onChange: (e) => setKind(e.target.value), title: "filter by job kind" }, kindOptions.map((k) => /* @__PURE__ */ React.createElement("option", { key: k, value: k }, k)))), jobs.length === 0 && /* @__PURE__ */ React.createElement("div", { style: { padding: 30, textAlign: "center", color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 12 } }, "no ", filter, " jobs"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8 } }, jobs.map((j) => /* @__PURE__ */ React.createElement(
    "div",
    {
      key: j.id,
      className: "panel",
      onClick: () => setOpenJob({ id: j.id, title: j.title, log: "loading...", spawned_by: j.spawned_by, status: j.status, kind: j.kind }),
      style: { cursor: "pointer", padding: "12px 16px", borderLeft: `3px solid ${j.status === "running" ? "var(--accent)" : j.status === "done" ? "var(--green, #4a7)" : j.status === "failed" ? "var(--red, #d24)" : j.status === "lost" ? "var(--amber, #c90)" : "var(--fg-3)"}` }
    },
    /* @__PURE__ */ React.createElement("div", { className: "job-head", style: { display: "flex", alignItems: "center", gap: 10, marginBottom: 4 } }, /* @__PURE__ */ React.createElement(CousinTag, { slug: j.spawned_by }), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", fontSize: 10, fontFamily: "var(--mono)" } }, "#", j.id), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", fontSize: 10, fontFamily: "var(--mono)" } }, "[", j.kind, "]"), /* @__PURE__ */ React.createElement(
      "span",
      {
        className: "pill " + ({ running: "cyan", done: "green", failed: "red", lost: "amber" }[j.status] || ""),
        title: j.status === "lost" ? "its process died without closing the job" : void 0
      },
      j.status
    ), /* @__PURE__ */ React.createElement("span", { className: "job-title", style: { flex: 1, fontWeight: 500 } }, j.title), /* @__PURE__ */ React.createElement(
      "span",
      {
        className: "job-time",
        style: { color: "var(--fg-3)", fontSize: 10, fontFamily: "var(--mono)" },
        title: j.started_at || ""
      },
      fmtStart(j.started_at),
      " · ",
      fmtAge(j.started_at, j.finished_at)
    ), j.status === "running" && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: (e) => {
          e.stopPropagation();
          cancelJob(j.id);
        },
        style: { fontSize: 10, padding: "2px 8px", minHeight: 20 }
      },
      "cancel"
    ), j.status !== "running" && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: (e) => {
          e.stopPropagation();
          deleteJob(j.id);
        },
        style: { fontSize: 10, padding: "2px 8px", minHeight: 20, color: "var(--fg-3)" }
      },
      "×"
    )),
    j.description && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 11, color: "var(--fg-2)", marginBottom: 4 } }, j.description),
    j.command && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)", marginBottom: 4, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, "$ ", j.command),
    j.git_commit && /* @__PURE__ */ React.createElement(
      "div",
      {
        style: { fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)", marginBottom: 4 },
        title: j.git_commit,
        "data-job-commit": true
      },
      "at commit ",
      j.git_commit.slice(0, 12)
    ),
    !j.log_path && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)", marginBottom: 4 } }, "no log attached"),
    j.status === "running" && tails[j.id] && /* @__PURE__ */ React.createElement("pre", { style: {
      margin: 0,
      padding: 6,
      background: "var(--bg-0)",
      border: "1px solid var(--line)",
      borderRadius: 3,
      fontSize: 10,
      fontFamily: "var(--mono)",
      color: "var(--fg-2)",
      maxHeight: 120,
      overflow: "auto",
      whiteSpace: "pre-wrap"
    } }, tails[j.id]),
    j.status === "failed" && j.exit_code !== null && j.exit_code !== void 0 && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, fontFamily: "var(--mono)", color: "var(--red, #d24)" } }, "exit ", j.exit_code, j.result_summary ? ` · ${j.result_summary}` : ""),
    j.status !== "failed" && j.result_summary && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)" } }, j.result_summary)
  ))), openJob && /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: () => setOpenJob(null) }, /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "modal",
      onClick: (e) => e.stopPropagation(),
      style: { width: "min(960px, 95vw)", height: "85vh", maxHeight: "85vh", display: "flex", flexDirection: "column" }
    },
    /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "job #", openJob.id, " · @", openJob.spawned_by, " · ", openJob.title), /* @__PURE__ */ React.createElement("button", { className: "close", style: { marginLeft: "auto" }, onClick: () => setOpenJob(null) }, "×")),
    /* @__PURE__ */ React.createElement("div", { className: "body", style: { padding: 0, overflow: "hidden", flex: 1, minHeight: 0, display: "flex", flexDirection: "column" } }, /* @__PURE__ */ React.createElement(JobLogPanel, { jobId: openJob.id }))
  )));
}
function ArtifactsPanel({ hiddenSlugs, showHidden }) {
  const [allRows, setRows] = React.useState(null);
  const rows = allRows && visibleArtifacts(allRows, hiddenSlugs, showHidden);
  const hiddenCount = allRows && hiddenSlugs ? allRows.length - rows.length : 0;
  const [checked, setChecked] = React.useState(false);
  const [hashed, setHashed] = React.useState({});
  const load = React.useCallback(async (verify) => {
    const d = await apiGet("/api/artifacts" + (verify ? "?verify=1" : ""));
    setRows(d ? d.artifacts || [] : []);
    setChecked(!!verify);
    setHashed({});
  }, []);
  React.useEffect(() => {
    load(false);
  }, [load]);
  const hash = async (id) => {
    setHashed((h) => ({ ...h, [id]: "hashing" }));
    const d = await apiGet(`/api/artifacts/${id}/verify`);
    setHashed((h) => ({ ...h, [id]: d ? d.state : "error" }));
  };
  const drop = async (id) => {
    if (!confirm(`Remove artifact #${id}? Only the row goes; the file stays.`)) return;
    const { r, d } = await apiSend("DELETE", `/api/artifacts/${id}`);
    if (!r.ok) {
      alert("remove failed: " + (d.error || r.status));
      return;
    }
    load(checked);
  };
  if (!rows || !hiddenSlugs && !showHidden) return null;
  const tone = {
    ok: "green",
    unchanged: "green",
    touched: "amber",
    changed: "amber",
    missing: "red",
    unreadable: "red"
  };
  const place = (r) => r.private ? `[private] ${r.label}` : (r.host ? `${r.host}:${r.path}` : r.path) + (r.label ? ` (${r.label})` : "");
  return /* @__PURE__ */ React.createElement("div", { className: "panel", "data-artifacts": true, style: { marginTop: 16, padding: "12px 16px" } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8 } }, /* @__PURE__ */ React.createElement("span", { className: "eyebrow" }, "artifacts"), /* @__PURE__ */ React.createElement("span", { className: "muted", style: { fontSize: 11 } }, "build outputs recorded with cousin-artifact"), hiddenCount > 0 && /* @__PURE__ */ React.createElement("span", { className: "muted", style: { fontSize: 11 }, "data-artifacts-hidden": true }, "· ", hiddenCount, " from hidden cousins"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), rows.length > 0 && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      onClick: () => load(true),
      title: "size and mtime of each file, no hashing"
    },
    "check files"
  )), rows.length === 0 && hiddenCount > 0 ? /* @__PURE__ */ React.createElement("div", { className: "muted", style: { fontSize: 12, marginTop: 6 }, "data-artifacts-all-hidden": true }, "every recorded artifact is a hidden cousin's; show hidden cousins to list them.") : rows.length === 0 ? /* @__PURE__ */ React.createElement("div", { className: "muted", style: { fontSize: 12, marginTop: 6 }, "data-artifacts-empty": true }, "none recorded yet. A job records what it builds when it is started with artifacts (the job tool's run, or cousin-job start --artifact); a media render records its file; cousin-artifact add records one by hand.") : /* @__PURE__ */ React.createElement("div", { className: "table-scroll" }, /* @__PURE__ */ React.createElement("table", { className: "data" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", { className: "num" }, "#"), /* @__PURE__ */ React.createElement("th", null, "by"), /* @__PURE__ */ React.createElement("th", null, "where"), /* @__PURE__ */ React.createElement("th", null, "sha256"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "size"), /* @__PURE__ */ React.createElement("th", null, "job"), /* @__PURE__ */ React.createElement("th", null, "commit"), /* @__PURE__ */ React.createElement("th", null, "now"), /* @__PURE__ */ React.createElement("th", null, "note"), /* @__PURE__ */ React.createElement("th", null))), /* @__PURE__ */ React.createElement("tbody", null, rows.map((r) => {
    const state = hashed[r.id] || (checked ? r.state : null);
    return /* @__PURE__ */ React.createElement("tr", { key: r.id }, /* @__PURE__ */ React.createElement("td", { className: "num" }, r.id), /* @__PURE__ */ React.createElement("td", { className: "mono" }, r.created_by), /* @__PURE__ */ React.createElement("td", { className: "mono", style: { wordBreak: "break-all" } }, place(r)), /* @__PURE__ */ React.createElement("td", { className: "mono", title: r.sha256 }, r.sha256.slice(0, 12)), /* @__PURE__ */ React.createElement("td", { className: "num" }, r.size), /* @__PURE__ */ React.createElement("td", { className: "mono" }, r.job_id ? "#" + r.job_id : ""), /* @__PURE__ */ React.createElement("td", { className: "mono" }, r.git_commit ? r.git_commit.slice(0, 12) : ""), /* @__PURE__ */ React.createElement("td", null, state && /* @__PURE__ */ React.createElement("span", { className: "pill " + (tone[state] || "") }, state)), /* @__PURE__ */ React.createElement("td", null, r.note || ""), /* @__PURE__ */ React.createElement("td", { style: { whiteSpace: "nowrap" } }, !r.private && !r.host && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: () => hash(r.id),
        title: "hash the file now and compare"
      },
      "hash"
    ), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: () => drop(r.id) }, "remove")));
  })))));
}
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
          const url = offsetRef.current === null ? `/api/jobs/${jobId}/log?lines=2000` : `/api/jobs/${jobId}/log?from=${offsetRef.current}`;
          const r = await fetch(url, { cache: "no-store" });
          d = await r.json();
        } catch (_e) {
          return;
        }
        if (cancelled || !d || !d.ok) return;
        const hasLog = d.has_log !== false && !!d.log_path;
        setMeta({ loaded: true, hasLog, path: d.log_path, size: d.size || 0 });
        if (!hasLog) return;
        if (!d.size) {
          setText(d.log || "");
          offsetRef.current = null;
          return;
        }
        if (offsetRef.current === null) {
          setText(d.log || "");
        } else if (d.size < offsetRef.current) {
          offsetRef.current = null;
          continue;
        } else if (d.log) {
          setText((t) => {
            const n = t + d.log;
            return n.length > JOB_LOG_KEEP ? n.slice(n.length - JOB_LOG_KEEP) : n;
          });
        }
        const next = d.next !== void 0 ? d.next : d.size;
        const caughtUp = next >= d.size;
        offsetRef.current = next;
        if (caughtUp) return;
      }
    };
    pull();
    const id = setInterval(pull, 2e3);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
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
    return /* @__PURE__ */ React.createElement("div", { className: "joblog-hint", "data-job-log-none": true }, "no log attached; start the job with ", /* @__PURE__ */ React.createElement("code", null, "cousin-job start shell TITLE -- CMD"), " or ", /* @__PURE__ */ React.createElement("code", null, "--log PATH"), " to stream one");
  }
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "joblog-bar" }, /* @__PURE__ */ React.createElement("span", { className: "joblog-path" }, meta.path || ""), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", null, meta.size ? `${(meta.size / 1024).toFixed(1)} kB · ` : "", "polls every 2s"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost" + (following ? " active" : ""),
      onClick: jump,
      "data-job-log-follow": following ? "on" : "off",
      title: following ? "following the end of the log" : "scrolled up: press to jump to the end and follow again"
    },
    following ? "following" : "follow"
  )), /* @__PURE__ */ React.createElement("pre", { ref: boxRef, onScroll, className: "joblog", "data-job-log": true }, text || (meta.loaded ? "(empty)" : "loading...")));
}
function MemoryView() {
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  const [scope, setScope] = React.useState("shared");
  const [allCousins, setAllCousins] = React.useState([]);
  const cousins = React.useMemo(
    () => (allCousins || []).filter((c) => showHidden || !c.hidden),
    [allCousins, showHidden]
  );
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
      fetchAuthMe()
    ]);
    setAllCousins(cs);
    if (sl) setShared({ canonical: sl.canonical || [], pending: sl.pending || [] });
    if (au) setAudit(au.entries || []);
    if (auth) setMe(auth);
  }, []);
  React.useEffect(() => {
    refresh();
    const id = setInterval(refresh, 8e3);
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
      if (!(me && me.configured)) {
        const by = window.prompt("reviewer name (auth is not configured, so the tier cannot infer it):");
        if (!by || !by.trim()) {
          setBusy(false);
          return;
        }
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
    ...cousins.filter((c) => !c.remote).map((c) => ({ id: c.slug, label: `@${c.slug}` }))
  ];
  const scopeBar = /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4, flexWrap: "wrap" }, "data-memory-scopes": true }, scopes.map((s) => /* @__PURE__ */ React.createElement(
    "button",
    {
      key: s.id,
      className: scope === s.id ? "btn active" : "btn",
      onClick: () => {
        setScope(s.id);
        setSelected(null);
        setContent("");
        setDiff("");
      },
      style: { fontSize: 11 }
    },
    s.label,
    " ",
    s.count !== void 0 && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, s.count)
  )));
  if (scope !== "shared") {
    return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad mx-page" }, scopeBar, /* @__PURE__ */ React.createElement(MemoryExplorer, { slug: scope }));
  }
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad", "data-memory-grid": true, style: { display: "grid", gridTemplateColumns: "320px 1fr", gap: 18, height: "calc(100dvh - 94px)" } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 10, minHeight: 0 } }, scopeBar, /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "panel", style: { flex: "0 0 auto" } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "pending"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, shared.pending.length)), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { padding: 0, maxHeight: 220, overflowY: "auto" } }, shared.pending.length === 0 && /* @__PURE__ */ React.createElement("div", { style: { padding: 10, color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 11 } }, "no pending proposals"), shared.pending.map((p) => /* @__PURE__ */ React.createElement(
    "div",
    {
      key: p.name,
      onClick: () => openShared("pending", p),
      style: {
        padding: "8px 12px",
        borderBottom: "1px solid var(--line)",
        cursor: "pointer",
        fontFamily: "var(--mono)",
        fontSize: 11,
        background: selected?.name === p.name ? "var(--bg-2)" : "transparent"
      }
    },
    /* @__PURE__ */ React.createElement("div", { style: { color: "var(--accent)" } }, "@", p.slug, " → ", p.origin),
    /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)" } }, fmtAgo(Math.floor(Date.now() / 1e3 - p.mtime)), " · ", p.sha)
  )))), /* @__PURE__ */ React.createElement("div", { className: "panel", style: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column" } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "canonical"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, shared.canonical.length)), /* @__PURE__ */ React.createElement("div", { className: "panel-body mem-list-body", style: { padding: 0, overflowY: "auto", flex: 1 } }, shared.canonical.map((c) => /* @__PURE__ */ React.createElement(
    "div",
    {
      key: c.name,
      onClick: () => openShared("canonical", { ...c, origin: c.name }),
      style: {
        padding: "8px 12px",
        borderBottom: "1px solid var(--line)",
        cursor: "pointer",
        fontFamily: "var(--mono)",
        fontSize: 11,
        background: selected?.name === c.name ? "var(--bg-2)" : "transparent"
      }
    },
    /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-1)" } }, c.name),
    /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)" } }, (c.size / 1024).toFixed(1), "kb · ", c.sha)
  )), shared.canonical.length === 0 && /* @__PURE__ */ React.createElement("div", { style: { padding: 10, color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 11 } }, "no canonical files yet"))), /* @__PURE__ */ React.createElement(SharedReviewersPanel, { onChange: setReviewers }))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 14, minHeight: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "panel mem-viewer", style: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column" } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, selected ? selected.scope === "shared" ? `${selected.kind === "pending" ? "proposal" : "canonical"}: ${selected.origin || selected.name}` : selected.name : "select a file"), selected?.scope === "shared" && selected.kind === "pending" && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), reviewers && reviewers.you_review === false && /* @__PURE__ */ React.createElement("span", { className: "muted", "data-not-reviewer": true, style: { marginRight: 8 } }, "you are not in the reviewer list"), /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: busy, onClick: () => act("approve") }, "approve"), /* @__PURE__ */ React.createElement("button", { className: "btn danger", disabled: busy, onClick: () => act("reject"), style: { marginLeft: 6 } }, "reject"))), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { padding: 0, overflow: "auto", flex: 1 } }, status && /* @__PURE__ */ React.createElement("div", { style: {
    padding: "6px 12px",
    fontFamily: "var(--mono)",
    fontSize: 11,
    color: status.ok ? "var(--green)" : "var(--red)",
    borderBottom: "1px solid var(--line)"
  } }, status.msg), selected?.scope === "shared" && selected.kind === "pending" && diff && /* @__PURE__ */ React.createElement("pre", { style: {
    margin: 0,
    padding: 12,
    fontSize: 11,
    color: "var(--fg-1)",
    background: "var(--bg-0)",
    borderBottom: "1px solid var(--line)",
    whiteSpace: "pre-wrap"
  } }, diff), content && /* @__PURE__ */ React.createElement("div", { style: { padding: 12 } }, /* @__PURE__ */ React.createElement(MarkdownDoc, { text: content })))), scope === "shared" && /* @__PURE__ */ React.createElement("div", { className: "panel", style: { flex: "0 0 auto", maxHeight: 220 } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "audit"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "last ", audit.length)), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { padding: 0, overflowY: "auto", maxHeight: 180 } }, /* @__PURE__ */ React.createElement("table", { className: "data audit-table" }, /* @__PURE__ */ React.createElement("tbody", null, audit.map((e, i) => /* @__PURE__ */ React.createElement("tr", { key: i }, /* @__PURE__ */ React.createElement("td", { className: "muted", style: { width: 110 } }, (e.ts || "").slice(11, 19)), /* @__PURE__ */ React.createElement("td", { style: { width: 80 } }, /* @__PURE__ */ React.createElement(StatePill, { state: e.kind === "promote" ? "succeeded" : e.kind === "reject" ? "failed" : "queued" })), /* @__PURE__ */ React.createElement("td", { style: { width: 90 }, className: "accent" }, "@", e.proposer || e.actor), /* @__PURE__ */ React.createElement("td", { className: "wrap-any", style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-1)" } }, e.file, e.reason ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, " · ", e.reason) : null))), audit.length === 0 && /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", { colSpan: 4, className: "muted", style: { padding: 14, textAlign: "center" } }, "no audit entries yet"))))))));
}
function LoopsView({ loops }) {
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  const [adding, setAdding] = React.useState(false);
  const [allCousins, setAllCousins] = React.useState([]);
  const cousins = React.useMemo(
    () => (allCousins || []).filter((c) => showHidden || !c.hidden),
    [allCousins, showHidden]
  );
  const [slug, setSlug] = React.useState("");
  const [form, setForm] = React.useState({ name: "", interval: 600, prompt: "", enabled: true });
  const [status, setStatus] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [daemon, setDaemon] = React.useState(null);
  const [errors, setErrors] = React.useState([]);
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      const d = await fetchLoopsFull();
      if (cancelled) return;
      setDaemon(d.daemon || null);
      setErrors(d.errors || []);
    };
    pull();
    const id = setInterval(pull, 15e3);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);
  React.useEffect(() => {
    fetchCousins().then((cs) => {
      setAllCousins(cs);
      const eligible = cs.filter((c) => showHidden || !c.hidden);
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
        enabled: form.enabled
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
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad" }, daemon && daemon.ok === false && /* @__PURE__ */ React.createElement("div", { style: {
    marginBottom: 10,
    padding: "8px 12px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: "var(--amber)",
    background: "oklch(from var(--amber) l c h / 0.10)",
    border: "1px solid var(--amber)",
    borderRadius: 3
  } }, daemon.message || "loops daemon down"), errors.length > 0 && /* @__PURE__ */ React.createElement("div", { style: {
    marginBottom: 10,
    padding: "8px 12px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: "var(--red)",
    background: "oklch(from var(--red) l c h / 0.08)",
    border: "1px solid var(--red)",
    borderRadius: 3
  } }, errors.map((e, i) => /* @__PURE__ */ React.createElement("div", { key: i }, e))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, marginBottom: 10 } }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" } }, loops.length, " loops"), daemon && daemon.ok && daemon.last_tick && /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" } }, "· daemon tick ", fmtAgo(Math.floor(Date.now() / 1e3 - daemon.last_tick))), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), status && /* @__PURE__ */ React.createElement("span", { style: { fontSize: 11, fontFamily: "var(--mono)", color: status.ok ? "var(--green)" : "var(--red)" } }, status.msg), !adding && /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: () => setAdding(true) }, I.plus, " new loop")), adding && /* @__PURE__ */ React.createElement("div", { className: "panel", style: { marginBottom: 10, padding: 10 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "120px 140px 90px 1fr 60px 80px 80px", gap: 6, alignItems: "center" } }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: slug, onChange: (e) => setSlug(e.target.value) }, cousins.filter((c) => !c.remote).map((c) => /* @__PURE__ */ React.createElement("option", { key: c.slug, value: c.slug }, "@", c.slug)), cousins.length === 0 && /* @__PURE__ */ React.createElement("option", { value: "" }, "(no cousin)")), /* @__PURE__ */ React.createElement("input", { className: "txt", value: form.name, onChange: (e) => setForm({ ...form, name: e.target.value }), placeholder: "loop name" }), /* @__PURE__ */ React.createElement("input", { className: "txt", type: "number", value: form.interval, onChange: (e) => setForm({ ...form, interval: e.target.value }), placeholder: "sec" }), /* @__PURE__ */ React.createElement("input", { className: "txt", value: form.prompt, onChange: (e) => setForm({ ...form, prompt: e.target.value }), placeholder: "prompt to inject" }), /* @__PURE__ */ React.createElement("label", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-2)" } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: form.enabled, onChange: (e) => setForm({ ...form, enabled: e.target.checked }) }), " on"), /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => {
    setAdding(false);
    setStatus(null);
  } }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !slug || !form.name.trim() || busy, onClick: submit }, busy ? "saving..." : "save"))), /* @__PURE__ */ React.createElement(LoopsTable, { loops, allCousins }));
}
function LoopDriftModal({ cousin, name, onClose }) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    fetch(`/api/loops/drift/${cousin}/${name}`, { cache: "no-store" }).then((r) => r.json()).then((d) => {
      if (!cancelled) d.ok ? setData(d) : setErr(d.error || "load failed");
    }).catch((e) => {
      if (!cancelled) setErr(String(e.message || e));
    });
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      cancelled = true;
      window.removeEventListener("keydown", onKey);
    };
  }, [cousin, name, onClose]);
  const drifts = data ? data.points.map((p) => p.drift) : [];
  const intervals = data ? data.points.map((p) => p.interval) : [];
  const avgDrift = drifts.length ? Math.round(drifts.reduce((a, b) => a + b, 0) / drifts.length) : 0;
  const maxDrift = drifts.length ? Math.max(...drifts) : 0;
  const minDrift = drifts.length ? Math.min(...drifts) : 0;
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", onClick: (e) => e.stopPropagation(), style: { width: "min(720px, 94vw)" } }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "loop drift · ", cousin, "/", name), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", marginLeft: "auto" } }, data ? `${data.n} fires · configured ${data.interval}s` : ""), /* @__PURE__ */ React.createElement("button", { className: "close", onClick: onClose }, "×")), /* @__PURE__ */ React.createElement("div", { className: "body", style: { fontFamily: "var(--mono)", fontSize: 12 } }, err && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--red)" } }, err), !err && !data && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)" } }, "loading..."), data && data.n === 0 && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)" } }, "not enough fires yet (need at least 2 in the daemon's fire log)"), data && data.n > 0 && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { style: { marginBottom: 10, color: "var(--fg-2)" } }, "actual interval between fires (s)"), /* @__PURE__ */ React.createElement(Spark, { data: intervals, width: 680, height: 80 }), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 18, marginBottom: 10, color: "var(--fg-2)" } }, "drift vs configured (s) · avg ", avgDrift, " · min ", minDrift, " · max ", maxDrift), /* @__PURE__ */ React.createElement(Spark, { data: drifts, width: 680, height: 60, tone: maxDrift > 60 ? "warn" : "" })))));
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
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`/api/cousins/${cousin}/loops`, { cache: "no-store" });
      const existing = (await r.json()).loops || [];
      const updated = existing.map((l) => {
        if ((l.name || "") !== loop.name) return l;
        const entry = { ...l, name: name.trim(), prompt, enabled, hidden };
        delete entry.interval_seconds;
        delete entry.daily_at;
        delete entry.cron;
        delete entry.days;
        if (cron.trim()) entry.cron = cron.trim();
        else if (dailyAt.trim()) {
          entry.daily_at = dailyAt.trim();
          const dl = days.split(",").map((s) => s.trim()).filter(Boolean);
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
    } finally {
      setBusy(false);
    }
  };
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", onClick: (e) => e.stopPropagation(), style: { width: "min(720px, 94vw)" } }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "edit loop · ", cousin, "/", loop.name), /* @__PURE__ */ React.createElement("button", { className: "close", onClick: onClose }, "×")), /* @__PURE__ */ React.createElement("div", { className: "body", style: { display: "flex", flexDirection: "column", gap: 10 } }, /* @__PURE__ */ React.createElement(Field, { label: "name" }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: name, onChange: (e) => setName(e.target.value), style: { width: "100%" } })), /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 2fr", gap: 8 } }, /* @__PURE__ */ React.createElement(Field, { label: "interval_seconds" }, /* @__PURE__ */ React.createElement("input", { className: "txt", type: "number", value: intervalSec, onChange: (e) => setIntervalSec(e.target.value), style: { width: "100%" }, placeholder: "0 = no interval" })), /* @__PURE__ */ React.createElement(Field, { label: "daily_at" }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: dailyAt, onChange: (e) => setDailyAt(e.target.value), style: { width: "100%" }, placeholder: "HH:MM" })), /* @__PURE__ */ React.createElement(Field, { label: "days (mon,tue...)" }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: days, onChange: (e) => setDays(e.target.value), style: { width: "100%" }, placeholder: "all" }))), /* @__PURE__ */ React.createElement(Field, { label: "cron (5-field, optional; wins over the other forms)" }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: cron, onChange: (e) => setCron(e.target.value), style: { width: "100%" }, placeholder: 'e.g. "*/15 * * * *"' })), /* @__PURE__ */ React.createElement(Field, { label: "prompt" }, /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: prompt, onChange: (e) => setPrompt(e.target.value), style: { width: "100%", minHeight: 200, resize: "vertical", fontFamily: "var(--mono)", fontSize: 12 } })), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 14 } }, /* @__PURE__ */ React.createElement("label", { style: { fontSize: 12, color: "var(--fg-1)", display: "inline-flex", alignItems: "center", gap: 6 } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: enabled, onChange: (e) => setEnabled(e.target.checked) }), " enabled"), /* @__PURE__ */ React.createElement(
    "label",
    {
      style: { fontSize: 12, color: "var(--fg-1)", display: "inline-flex", alignItems: "center", gap: 6 },
      title: "hidden loops disappear from the Loops view unless Settings: show hidden is on"
    },
    /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: hidden, onChange: (e) => setHidden(e.target.checked) }),
    " hidden"
  )), err && /* @__PURE__ */ React.createElement("div", { style: { padding: "5px 9px", color: "var(--red)", fontSize: 11, fontFamily: "var(--mono)", background: "oklch(from var(--red) l c h / 0.08)", borderRadius: 3 } }, err), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginTop: 4 } }, /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: onClose }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: save, disabled: busy }, busy ? "saving..." : "save")))));
}
function Field({ label, children }) {
  return /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 4 } }, label), children);
}
function fmtCountdown(secs) {
  if (secs <= 0) return "due";
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ${Math.floor(secs % 3600 / 60)}m`;
  return `${Math.floor(secs / 86400)}d ${Math.floor(secs % 86400 / 3600)}h`;
}
function LoopsTable({ loops, allCousins }) {
  const [confirm2, setConfirm] = React.useState(null);
  const [firing, setFiring] = React.useState(null);
  const [fireToast, setFireToast] = React.useState(null);
  const [driftOpen, setDriftOpen] = React.useState(null);
  const [editOpen, setEditOpen] = React.useState(null);
  const [, setNow] = React.useState(Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1e3);
    return () => clearInterval(id);
  }, []);
  React.useEffect(() => {
    if (!confirm2) return;
    const t = setTimeout(() => setConfirm(null), 3500);
    return () => clearTimeout(t);
  }, [confirm2]);
  React.useEffect(() => {
    if (!fireToast) return;
    const t = setTimeout(() => setFireToast(null), 4e3);
    return () => clearTimeout(t);
  }, [fireToast]);
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
    if (confirm2 !== key) {
      setConfirm(key);
      return;
    }
    setConfirm(null);
    try {
      const r = await fetch(`/api/cousins/${cousin}/loops`, { cache: "no-store" });
      const existing = (await r.json()).loops || [];
      const next = existing.filter((l) => (l.name || "") !== name);
      const { r: r2, d } = await apiSend("POST", `/api/cousins/${cousin}/loops`, { loops: next });
      if (!r2.ok || !d.ok) setFireToast({ ok: false, msg: `remove ${name}: ${d.error || "HTTP " + r2.status}` });
    } catch (_e) {
    }
  };
  const removable = (l) => l.name !== "context-heartbeat";
  const hideable = (l) => l.name !== "context-heartbeat";
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  const hiddenCousinSlugs = React.useMemo(
    () => new Set((allCousins || []).filter((c) => c.hidden).map((c) => c.slug)),
    [allCousins]
  );
  const visibleLoops = loops.filter((l) => (showHidden || !l.hidden) && (showHidden || !hiddenCousinSlugs.has(l.cousin)));
  const hiddenCount = loops.filter((l) => l.hidden || hiddenCousinSlugs.has(l.cousin)).length;
  const toggleHidden = async (cousin, name, currentlyHidden) => {
    try {
      await apiSend("POST", `/api/cousins/${cousin}/loops/${encodeURIComponent(name)}/hidden`, { hidden: !currentlyHidden });
    } catch (_e) {
    }
  };
  const renderRow = (l, i) => {
    const key = `${l.cousin}/${l.name}`;
    const editable = l.name !== "context-heartbeat" && l.schedule;
    const nowSec = Math.floor(Date.now() / 1e3);
    const nextIn = l.nextFireTs ? l.nextFireTs - nowSec : 0;
    return /* @__PURE__ */ React.createElement("tr", { key: key + i }, /* @__PURE__ */ React.createElement("td", { "data-label": "cousin" }, /* @__PURE__ */ React.createElement(CousinTag, { slug: l.cousin })), /* @__PURE__ */ React.createElement("td", { "data-label": "loop", style: { color: "var(--fg-0)" } }, l.name, l.hidden && /* @__PURE__ */ React.createElement("span", { style: { marginLeft: 6, fontSize: 9, padding: "1px 5px", borderRadius: 2, background: "oklch(from var(--fg-3) l c h / 0.18)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.05em" } }, "hidden")), /* @__PURE__ */ React.createElement("td", { "data-label": "state" }, /* @__PURE__ */ React.createElement(StatePill, { state: l.state })), /* @__PURE__ */ React.createElement("td", { "data-label": "interval", className: "muted" }, l.interval ? l.interval + "s" : l.schedule?.cron ? `cron: ${l.schedule.cron}` : l.schedule?.daily_at ? `daily ${l.schedule.daily_at}${l.schedule.days?.length ? ` (${l.schedule.days.join(",")})` : ""}` : "-"), /* @__PURE__ */ React.createElement("td", { "data-label": "last fire", className: "muted" }, l.lastFireTs ? fmtAgo(l.lastTick) : "never"), /* @__PURE__ */ React.createElement("td", { "data-label": "next in", className: "muted", style: { color: l.nextFireTs && nextIn <= 0 ? "var(--accent)" : "var(--fg-2)" } }, l.nextFireTs ? fmtCountdown(nextIn) : /* @__PURE__ */ React.createElement("span", { style: { opacity: 0.4 } }, "-")), /* @__PURE__ */ React.createElement("td", { "data-label": "drift", className: "num", style: { color: l.drift > 300 ? "var(--red)" : l.drift > 60 ? "var(--amber)" : "var(--fg-2)" } }, l.drift ? l.drift + "s" : "-"), /* @__PURE__ */ React.createElement("td", { "data-label": "note", className: "muted" }, l.note || /* @__PURE__ */ React.createElement("span", { style: { opacity: 0.4 } }, "-")), /* @__PURE__ */ React.createElement("td", { "data-label": "actions", style: { textAlign: "right", whiteSpace: "nowrap" } }, editable && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        style: { fontSize: 10, padding: "2px 6px", marginRight: 4 },
        onClick: () => setEditOpen(l),
        title: "edit loop schedule + prompt"
      },
      "edit"
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        style: { fontSize: 10, padding: "2px 6px", marginRight: 4 },
        onClick: () => setDriftOpen({ cousin: l.cousin, name: l.name }),
        title: "show drift series"
      },
      "drift"
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn",
        style: { fontSize: 10, padding: "2px 6px", marginRight: 4 },
        onClick: () => fireNow(l.cousin, l.name),
        disabled: firing === key,
        title: "request a fire now (a request row the daemon consumes)"
      },
      firing === key ? "..." : "fire"
    ), hideable(l) && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        style: { fontSize: 10, padding: "2px 6px", marginRight: 4 },
        onClick: () => toggleHidden(l.cousin, l.name, l.hidden),
        title: l.hidden ? "loop is hidden - click to unhide" : "hide this loop from the default view"
      },
      l.hidden ? "unhide" : "hide"
    ), removable(l) && /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn danger",
        style: { fontSize: 10, padding: "2px 6px", minWidth: 28 },
        onClick: () => remove(l.cousin, l.name),
        title: confirm2 === key ? "click again to confirm" : "remove loop"
      },
      confirm2 === key ? "sure?" : "×"
    )));
  };
  return /* @__PURE__ */ React.createElement("div", { className: "panel" }, driftOpen && /* @__PURE__ */ React.createElement(LoopDriftModal, { ...driftOpen, onClose: () => setDriftOpen(null) }), editOpen && /* @__PURE__ */ React.createElement(LoopEditModal, { cousin: editOpen.cousin, loop: editOpen, onClose: () => setEditOpen(null) }), fireToast && /* @__PURE__ */ React.createElement("div", { style: {
    padding: "8px 12px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    borderBottom: "1px solid var(--line)",
    color: fireToast.ok ? "var(--green)" : "var(--red)",
    background: fireToast.ok ? "oklch(from var(--green) l c h / 0.08)" : "oklch(from var(--red) l c h / 0.08)"
  } }, fireToast.msg), hiddenCount > 0 && !showHidden && /* @__PURE__ */ React.createElement("div", { style: { padding: "6px 12px", fontSize: 10, fontFamily: "var(--mono)", color: "var(--fg-3)", borderBottom: "1px solid var(--line)" } }, hiddenCount, " loop", hiddenCount === 1 ? "" : "s", " hidden · Settings: show hidden to reveal"), /* @__PURE__ */ React.createElement("table", { className: "data loops-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "cousin"), /* @__PURE__ */ React.createElement("th", null, "loop"), /* @__PURE__ */ React.createElement("th", null, "state"), /* @__PURE__ */ React.createElement("th", null, "interval"), /* @__PURE__ */ React.createElement("th", null, "last fire"), /* @__PURE__ */ React.createElement("th", null, "next in"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "drift"), /* @__PURE__ */ React.createElement("th", null, "note"), /* @__PURE__ */ React.createElement("th", { style: { width: 180 } }))), /* @__PURE__ */ React.createElement("tbody", null, visibleLoops.map((l, i) => renderRow(l, i)), visibleLoops.length === 0 && /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", { colSpan: 9, className: "muted", style: { textAlign: "center", padding: 20 } }, "no loops configured")))));
}
function UpkeepPanel() {
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    apiGet("/api/tokens/upkeep?days=7").then((x) => {
      if (!cancelled) setD(x);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  if (!d || !d.cousins) return null;
  const rows = Object.entries(d.cousins).filter(([, m]) => m.turns > 0);
  const usd = (v) => "$" + (Number(v) || 0).toFixed(2);
  const pct = (v) => v == null ? "-" : Math.round(v * 100) + "%";
  const cls = (m, k) => m.classes && m.classes[k] || { turns: 0, cost_usd: 0 };
  const over = (m) => d.days === 7 && !!m.alarm_percent && m.headline_share != null && m.cost_usd >= 1 && m.headline_share * 100 > m.alarm_percent;
  return /* @__PURE__ */ React.createElement("div", { className: "panel", "data-upkeep": true, style: { marginTop: 16, padding: "12px 16px" } }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "upkeep vs work, last ", d.days, " days"), /* @__PURE__ */ React.createElement("div", { className: "muted", style: { fontSize: 11, margin: "4px 0 8px" } }, "upkeep = turns that only answered the heartbeat, a boot digest or a memory proposal; self = prompts a cousin scheduled for itself and job notices it asked for; the headline is upkeep + self, red over the cousin's upkeep_alarm_percent (agent settings); dollars on a login are estimates (cousin-upkeep)"), /* @__PURE__ */ React.createElement("div", { className: "table-scroll" }, /* @__PURE__ */ React.createElement("table", { className: "data" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "cousin"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "upkeep + self"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "framework only"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "alarm"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "upkeep"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "self"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "work"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "other"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "turns"))), /* @__PURE__ */ React.createElement("tbody", null, rows.map(([slug, m]) => /* @__PURE__ */ React.createElement("tr", { key: slug, "data-upkeep-over": over(m) ? "1" : void 0 }, /* @__PURE__ */ React.createElement("td", { className: "mono" }, slug), /* @__PURE__ */ React.createElement("td", { className: "num", style: over(m) ? { color: "var(--bad)" } : void 0 }, /* @__PURE__ */ React.createElement("b", null, pct(m.headline_share))), /* @__PURE__ */ React.createElement("td", { className: "num" }, pct(m.upkeep_share)), /* @__PURE__ */ React.createElement("td", { className: "num" }, m.alarm_percent ? Math.round(m.alarm_percent) + "%" : "off"), /* @__PURE__ */ React.createElement("td", { className: "num" }, usd(cls(m, "upkeep").cost_usd)), /* @__PURE__ */ React.createElement("td", { className: "num" }, usd(cls(m, "self").cost_usd)), /* @__PURE__ */ React.createElement("td", { className: "num" }, usd(cls(m, "work").cost_usd)), /* @__PURE__ */ React.createElement("td", { className: "num" }, usd(cls(m, "other").cost_usd)), /* @__PURE__ */ React.createElement("td", { className: "num" }, m.turns)))))));
}
function TokensView({ cousins: allCousins }) {
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  const cousins = React.useMemo(
    () => (allCousins || []).filter((c) => showHidden || !c.hidden),
    [allCousins, showHidden]
  );
  const [tok, setTok] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      const d = await fetchTokens();
      if (!cancelled) setTok(d);
    };
    pull();
    const id = setInterval(pull, 3e4);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);
  const totalSpent = cousins.reduce((s, c) => s + (Number(c.tokensSpent) || 0), 0);
  const seriesFor = (slug) => {
    const row = (tok?.cousins || []).find((c) => c.slug === slug);
    return row ? row.series || [] : [];
  };
  const cacheFor = (slug) => {
    const row = (tok?.cousins || []).find((c) => c.slug === slug);
    return row && row.cache ? row.cache : { rate: null, days: [] };
  };
  const fmtRate = (r) => r == null ? "-" : Math.round(r * 100) + "%";
  const capFor = (slug) => {
    const row = (tok?.cousins || []).find((c) => c.slug === slug);
    return row && row.cap ? row.cap : { limit: null, spent_today: null };
  };
  const fmtUsd = (v) => v == null ? "-" : "$" + Number(v).toFixed(2);
  const costToday = (series) => {
    const last = series.length ? series[series.length - 1] : null;
    return last ? last.cost_usd : null;
  };
  const fleetSeries = React.useMemo(() => {
    const byDay = {};
    for (const c of tok?.cousins || []) {
      if (!cousins.some((x) => x.slug === c.slug)) continue;
      for (const p of c.series || []) byDay[p.day] = (byDay[p.day] || 0) + (p.total || 0);
    }
    return Object.keys(byDay).sort().map((day) => ({ day, total: byDay[day] }));
  }, [tok, cousins]);
  const fleetTwoWeeks = fleetSeries.reduce((s, p) => s + p.total, 0);
  const usdRows = (tok?.cousins || []).filter((c) => cousins.some((x) => x.slug === c.slug) && costToday(c.series || []) != null);
  const fleetUsdToday = usdRows.length ? usdRows.reduce((s, c) => s + (Number(costToday(c.series)) || 0), 0) : null;
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad" }, tok && tok.available === false && /* @__PURE__ */ React.createElement("div", { style: {
    marginBottom: 14,
    padding: "8px 12px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: "var(--amber)",
    background: "oklch(from var(--amber) l c h / 0.10)",
    border: "1px solid var(--amber)",
    borderRadius: 3
  } }, "token counts unavailable: ", tok.reason || "no transcripts seam configured"), /* @__PURE__ */ React.createElement("div", { className: "panel", style: { marginBottom: 14 } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "fleet"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "today and the last 14 days")), /* @__PURE__ */ React.createElement("div", { className: "panel-body" }, /* @__PURE__ */ React.createElement("div", { "data-token-row": true, style: { display: "flex", gap: 32, alignItems: "center", marginBottom: 16 } }, /* @__PURE__ */ React.createElement(Stat, { label: "spent today", value: fmtTokens(totalSpent) }), /* @__PURE__ */ React.createElement(Stat, { label: "last 14 days", value: fmtTokens(fleetTwoWeeks) }), /* @__PURE__ */ React.createElement(Stat, { label: "$ today", value: fmtUsd(fleetUsdToday) }), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }), fleetSeries.length > 1 && /* @__PURE__ */ React.createElement(Spark, { data: fleetSeries.map((p) => p.total), width: 220, height: 40 })))), /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 14 } }, cousins.map((c) => {
    const series = seriesFor(c.slug);
    const twoWeeks = series.reduce((s, p) => s + (p.total || 0), 0);
    const output = series.reduce((s, p) => s + (p.output || 0), 0);
    const cache = cacheFor(c.slug);
    const cacheToday = cache.days.length ? cache.days[cache.days.length - 1].rate : null;
    const usd = series.some((p) => p.cost_usd != null);
    const usdTwoWeeks = series.reduce((s, p) => s + (Number(p.cost_usd) || 0), 0);
    const cap = capFor(c.slug);
    const over = cap.limit != null && cap.spent_today != null && cap.spent_today >= cap.limit;
    const usdMax = Math.max(...series.map((p) => Number(p.cost_usd) || 0), cap.limit || 0, 0.01);
    return /* @__PURE__ */ React.createElement("div", { className: "panel", key: c.slug }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement(CousinTag, { slug: c.slug }), /* @__PURE__ */ React.createElement("span", { className: "title" }, c.name), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" } }, c.type)), /* @__PURE__ */ React.createElement("div", { className: "panel-body" }, /* @__PURE__ */ React.createElement("div", { className: "tok-grid" }, /* @__PURE__ */ React.createElement(Stat, { small: true, label: "today", value: fmtTokens(c.tokensSpent) }), /* @__PURE__ */ React.createElement(Stat, { small: true, label: "14 days", value: fmtTokens(twoWeeks) }), /* @__PURE__ */ React.createElement(Stat, { small: true, label: "output", value: fmtTokens(output) }), /* @__PURE__ */ React.createElement(Stat, { small: true, label: "cache today", value: fmtRate(cacheToday) }), /* @__PURE__ */ React.createElement(Stat, { small: true, label: "cache 14 days", value: fmtRate(cache.rate) }), /* @__PURE__ */ React.createElement("div", { className: "tok-spark" }, series.length > 1 ? /* @__PURE__ */ React.createElement(Spark, { data: series.map((p) => p.total), width: 100, height: 28 }) : /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" } }, "no series")), usd && /* @__PURE__ */ React.createElement(Stat, { small: true, label: "$ today", value: fmtUsd(costToday(series)) }), usd && /* @__PURE__ */ React.createElement(Stat, { small: true, label: "$ 14 days", value: fmtUsd(usdTwoWeeks) }), cap.limit != null && /* @__PURE__ */ React.createElement("div", { "data-cost-cap": true, className: "stat-cell", title: "[agent] daily_cost_cap_usd: over it only chat from a person runs" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "cap today"), /* @__PURE__ */ React.createElement("div", { className: "stat-value", style: { fontSize: 15, color: over ? "var(--red)" : void 0 } }, fmtUsd(cap.spent_today), " / ", fmtUsd(cap.limit)))), usd && /* @__PURE__ */ React.createElement("div", { "data-cost-days": true, style: { display: "flex", alignItems: "flex-end", gap: 2, height: 24, marginTop: 10 } }, series.map((p) => {
      const v = Number(p.cost_usd) || 0;
      const red = cap.limit != null && v >= cap.limit;
      return /* @__PURE__ */ React.createElement(
        "div",
        {
          key: p.day,
          title: `${p.day}: ${fmtUsd(p.cost_usd)}`,
          style: {
            flex: 1,
            minHeight: 1,
            height: `${Math.max(4, v / usdMax * 100)}%`,
            background: red ? "var(--red)" : "var(--fg-3)",
            opacity: v ? 0.8 : 0.25
          }
        }
      );
    }))));
  }), cousins.length === 0 && /* @__PURE__ */ React.createElement("div", { style: { padding: 20, color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 12 } }, "no cousins registered")), /* @__PURE__ */ React.createElement(UpkeepPanel, null));
}
function Stat({ label, value, small }) {
  return /* @__PURE__ */ React.createElement("div", { className: "stat-cell" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, label), /* @__PURE__ */ React.createElement("div", { className: "stat-value", style: { fontSize: small ? 15 : 22 } }, value));
}
function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}
const TRACKER_STATES = ["open", "active", "blocked", "done", "dropped"];
const TRACKER_CLOSED = /* @__PURE__ */ new Set(["done", "dropped"]);
function trackerChange(c) {
  const head = `${(c.at || "").slice(0, 16).replace("T", " ")} UTC ${c.who || "?"}: `;
  if (c.field === "created") return head + "created " + JSON.stringify(c.new);
  if (c.field === "deleted") return head + "deleted";
  if (c.field === "notes") {
    const o = c.old || "", n = c.new || "";
    if (n.startsWith(o)) return head + "notes += " + n.slice(o.length).replace(/^\n+/, "");
    return head + (n ? "notes replaced; were:\n" : "notes cleared; were:\n") + o;
  }
  return head + `${c.field} ${JSON.stringify(c.old)} -> ${JSON.stringify(c.new)}`;
}
function TrackerView({ cousins }) {
  const [items, setItems] = React.useState([]);
  const [filters, setFilters] = React.useState({ owner: "", state: "", domain: "" });
  const [showClosed, setShowClosed] = React.useState(false);
  const [adding, setAdding] = React.useState(false);
  const [form, setForm] = React.useState({ title: "", domain: "", state: "open", owner: "", tags: "", notes: "" });
  const [editing, setEditing] = React.useState(null);
  const [history, setHistory] = React.useState(null);
  const [noteText, setNoteText] = React.useState("");
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
    const id = setInterval(pull, 5e3);
    return () => clearInterval(id);
  }, [pull]);
  React.useEffect(() => {
    if (!status) return;
    const t = setTimeout(() => setStatus(null), 4e3);
    return () => clearTimeout(t);
  }, [status]);
  const parseTags = (s) => s.split(",").map((t) => t.trim()).filter(Boolean);
  const itemNotes = (it) => it.notes ?? it.summary ?? "";
  const itemUpdated = (it) => it.updated_at || it.updated || "";
  const itemTags = (it) => Array.isArray(it.tags) ? it.tags : [];
  const create = async () => {
    if (!form.title.trim() || busy) return;
    setBusy(true);
    try {
      const body = {
        title: form.title.trim(),
        domain: form.domain.trim(),
        state: form.state,
        owner: form.owner.trim(),
        tags: parseTags(form.tags),
        notes: form.notes
      };
      const { r, d } = await apiSend("POST", "/api/tracker", body);
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
      setStatus({ ok: true, msg: `added #${d.item?.id ?? "?"}` });
      setForm({ title: "", domain: "", state: "open", owner: "", tags: "", notes: "" });
      setAdding(false);
      await pull();
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };
  const loadHistory = React.useCallback(async (id) => {
    const d = await apiGet(`/api/tracker/${id}`);
    setHistory({ id, rows: d && d.history || [] });
  }, []);
  const toggleEdit = (it) => {
    setHistory(null);
    setNoteText("");
    if (editing && editing.id === it.id) {
      setEditing(null);
      return;
    }
    setEditing({ ...it, orig: it, tagsText: itemTags(it).join(", "), notesText: itemNotes(it) });
    loadHistory(it.id);
  };
  const patch = async (id, fields) => {
    setBusy(true);
    try {
      const { r, d } = await apiSend("POST", `/api/tracker/${id}`, fields);
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
      setStatus({ ok: true, msg: `updated #${id}` });
      await pull();
      if (history && history.id === id) loadHistory(id);
      return true;
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
      return false;
    } finally {
      setBusy(false);
    }
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
    } finally {
      setBusy(false);
    }
  };
  const visible = items.filter((it) => showClosed || !TRACKER_CLOSED.has(it.state));
  const closedCount = items.filter((it) => TRACKER_CLOSED.has(it.state)).length;
  const owners = React.useMemo(() => {
    const set = new Set((cousins || []).map((c) => c.slug));
    for (const it of items) if (it.owner) set.add(it.owner);
    return [...set].sort();
  }, [items, cousins]);
  const domains = React.useMemo(() => {
    const set = /* @__PURE__ */ new Set();
    for (const it of items) if (it.domain) set.add(it.domain);
    return [...set].sort();
  }, [items]);
  const stateTone = (s) => s === "active" ? "green" : s === "blocked" ? "red" : s === "open" ? "amber" : "gray";
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad" }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-3)" } }, visible.length, " items", closedCount > 0 && !showClosed ? ` · ${closedCount} closed hidden` : ""), /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: filters.owner, onChange: (e) => setFilters({ ...filters, owner: e.target.value }), title: "owner" }, /* @__PURE__ */ React.createElement("option", { value: "" }, "any owner"), owners.map((o) => /* @__PURE__ */ React.createElement("option", { key: o, value: o }, "@", o))), /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: filters.state, onChange: (e) => setFilters({ ...filters, state: e.target.value }), title: "state" }, /* @__PURE__ */ React.createElement("option", { value: "" }, "any state"), TRACKER_STATES.map((s) => /* @__PURE__ */ React.createElement("option", { key: s, value: s }, s))), /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: filters.domain, onChange: (e) => setFilters({ ...filters, domain: e.target.value }), title: "domain" }, /* @__PURE__ */ React.createElement("option", { value: "" }, "any domain"), domains.map((d) => /* @__PURE__ */ React.createElement("option", { key: d, value: d }, d))), /* @__PURE__ */ React.createElement("label", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-2)", display: "inline-flex", alignItems: "center", gap: 4 } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: showClosed, onChange: (e) => setShowClosed(e.target.checked) }), " show closed"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), status && /* @__PURE__ */ React.createElement("span", { style: { fontSize: 11, fontFamily: "var(--mono)", color: status.ok ? "var(--green)" : "var(--red)" } }, status.msg), !adding && /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: () => setAdding(true) }, I.plus, " new item")), adding && /* @__PURE__ */ React.createElement("div", { className: "panel", style: { marginBottom: 12, padding: 10 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr 1fr", gap: 6, alignItems: "center", marginBottom: 6 } }, /* @__PURE__ */ React.createElement("input", { className: "txt", autoFocus: true, value: form.title, onChange: (e) => setForm({ ...form, title: e.target.value }), placeholder: "title (required)" }), /* @__PURE__ */ React.createElement("input", { className: "txt", value: form.domain, onChange: (e) => setForm({ ...form, domain: e.target.value }), placeholder: "domain" }), /* @__PURE__ */ React.createElement("select", { className: "sel", value: form.state, onChange: (e) => setForm({ ...form, state: e.target.value }) }, TRACKER_STATES.map((s) => /* @__PURE__ */ React.createElement("option", { key: s, value: s }, s))), /* @__PURE__ */ React.createElement("input", { className: "txt", value: form.owner, onChange: (e) => setForm({ ...form, owner: e.target.value }), placeholder: "owner (slug)", list: "tracker-owners" }), /* @__PURE__ */ React.createElement("input", { className: "txt", value: form.tags, onChange: (e) => setForm({ ...form, tags: e.target.value }), placeholder: "tags, comma separated" })), /* @__PURE__ */ React.createElement("datalist", { id: "tracker-owners" }, owners.map((o) => /* @__PURE__ */ React.createElement("option", { key: o, value: o }))), /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: form.notes, onChange: (e) => setForm({ ...form, notes: e.target.value }), placeholder: "notes", style: { width: "100%", minHeight: 60, boxSizing: "border-box" } }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginTop: 6, justifyContent: "flex-end" } }, /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => setAdding(false) }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !form.title.trim() || busy, onClick: create }, busy ? "saving..." : "add"))), /* @__PURE__ */ React.createElement("div", { className: "panel" }, /* @__PURE__ */ React.createElement("table", { className: "data tracker-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", { style: { width: 50 } }, "id"), /* @__PURE__ */ React.createElement("th", null, "title"), /* @__PURE__ */ React.createElement("th", null, "owner"), /* @__PURE__ */ React.createElement("th", null, "domain"), /* @__PURE__ */ React.createElement("th", null, "state"), /* @__PURE__ */ React.createElement("th", null, "tags"), /* @__PURE__ */ React.createElement("th", null, "updated"), /* @__PURE__ */ React.createElement("th", { style: { width: 120 } }))), /* @__PURE__ */ React.createElement("tbody", null, visible.map((it) => /* @__PURE__ */ React.createElement(React.Fragment, { key: it.id }, /* @__PURE__ */ React.createElement("tr", { style: { opacity: TRACKER_CLOSED.has(it.state) ? 0.6 : 1 } }, /* @__PURE__ */ React.createElement("td", { className: "muted", "data-label": "id" }, "#", it.id), /* @__PURE__ */ React.createElement("td", { "data-label": "title", style: { color: "var(--fg-0)" } }, it.title, itemNotes(it) && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, color: "var(--fg-3)", whiteSpace: "pre-wrap" } }, itemNotes(it))), /* @__PURE__ */ React.createElement("td", { "data-label": "owner" }, it.owner ? /* @__PURE__ */ React.createElement(CousinTag, { slug: it.owner }) : /* @__PURE__ */ React.createElement("span", { className: "muted" }, "-")), /* @__PURE__ */ React.createElement("td", { className: "muted", "data-label": "domain" }, it.domain || "-"), /* @__PURE__ */ React.createElement("td", { "data-label": "state" }, /* @__PURE__ */ React.createElement("select", { className: "sel-inline", value: it.state, onChange: (e) => patch(it.id, { state: e.target.value }), style: { color: `var(--${stateTone(it.state)}, var(--fg-1))` }, disabled: busy }, TRACKER_STATES.map((s) => /* @__PURE__ */ React.createElement("option", { key: s, value: s }, s)))), /* @__PURE__ */ React.createElement("td", { className: "muted", "data-label": "tags", style: { fontFamily: "var(--mono)", fontSize: 10 } }, itemTags(it).join(", ") || "-"), /* @__PURE__ */ React.createElement("td", { className: "muted", "data-label": "updated", title: itemUpdated(it) }, itemUpdated(it).slice(0, 16).replace("T", " ") || "-"), /* @__PURE__ */ React.createElement("td", { "data-label": "actions", style: { textAlign: "right", whiteSpace: "nowrap" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: { fontSize: 10, padding: "2px 6px", marginRight: 4 },
      onClick: () => toggleEdit(it)
    },
    editing && editing.id === it.id ? "close" : "edit"
  ), /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: { fontSize: 10, padding: "2px 6px" }, onClick: () => remove(it.id), disabled: busy }, "×"))), editing && editing.id === it.id && /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", { colSpan: 8, "data-label": "edit", style: { background: "var(--bg-0)", padding: 10 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr", gap: 6, marginBottom: 6 } }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: editing.title, onChange: (e) => setEditing({ ...editing, title: e.target.value }), placeholder: "title" }), /* @__PURE__ */ React.createElement("input", { className: "txt", value: editing.domain || "", onChange: (e) => setEditing({ ...editing, domain: e.target.value }), placeholder: "domain" }), /* @__PURE__ */ React.createElement("input", { className: "txt", value: editing.owner || "", onChange: (e) => setEditing({ ...editing, owner: e.target.value }), placeholder: "owner", list: "tracker-owners" }), /* @__PURE__ */ React.createElement("input", { className: "txt", value: editing.tagsText, onChange: (e) => setEditing({ ...editing, tagsText: e.target.value }), placeholder: "tags, comma separated" })), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginBottom: 6 } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      style: { flex: 1 },
      value: noteText,
      onChange: (e) => setNoteText(e.target.value),
      placeholder: "add a note (appended, dated and signed)",
      onKeyDown: async (e) => {
        if (e.key === "Enter" && noteText.trim() && !busy && await patch(it.id, { add_note: noteText.trim() })) setNoteText("");
      }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: busy || !noteText.trim(), onClick: async () => {
    if (await patch(it.id, { add_note: noteText.trim() })) setNoteText("");
  } }, "add note")), /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: editing.notesText, onChange: (e) => setEditing({ ...editing, notesText: e.target.value }), placeholder: "notes (saving a change here replaces them; the old text stays in the history)", title: "saving a change here replaces the notes; the old text stays in the history", style: { width: "100%", minHeight: 60, boxSizing: "border-box" } }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginTop: 6, justifyContent: "flex-end" } }, /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => {
    setEditing(null);
    setHistory(null);
  } }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: busy || !editing.title.trim(), onClick: async () => {
    const o = editing.orig, fields = {};
    const want = { title: editing.title.trim(), domain: editing.domain || "", owner: editing.owner || "" };
    for (const k of Object.keys(want)) if (want[k] !== (o[k] || "")) fields[k] = want[k];
    if (editing.notesText !== itemNotes(o)) fields.notes = editing.notesText;
    const tags = parseTags(editing.tagsText);
    if (JSON.stringify(tags) !== JSON.stringify(itemTags(o))) fields.tags = tags;
    if (Object.keys(fields).length === 0) {
      setEditing(null);
      setHistory(null);
      return;
    }
    const ok = await patch(it.id, fields);
    if (ok) {
      setEditing(null);
      setHistory(null);
    }
  } }, busy ? "saving..." : "save")), /* @__PURE__ */ React.createElement("div", { className: "tracker-history", style: { marginTop: 8, fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-2)", maxHeight: 220, overflowY: "auto" } }, /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)", marginBottom: 2 } }, "history, oldest first"), !history || history.id !== it.id ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, "loading...") : history.rows.length === 0 ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, "none recorded") : history.rows.map((c) => /* @__PURE__ */ React.createElement("div", { key: c.id, style: { whiteSpace: "pre-wrap" } }, trackerChange(c)))))))), visible.length === 0 && /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", { colSpan: 8, className: "muted", style: { textAlign: "center", padding: 20 } }, "no tracker items", closedCount ? " open" : ""))))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 8, fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" } }, "open first, then closed; most recently updated first. The same list as ", /* @__PURE__ */ React.createElement("code", null, "cousin-tracker list --json"), "."));
}
const SETTINGS_KEY = "console_settings_v1";
const SETTINGS_DEFAULTS = {
  accentHue: 355,
  accentChroma: 0.18,
  backgroundTone: 0.12,
  chatStreamSpeed: "normal",
  // off | slow | normal | fast
  chatStreamEffect: "plain",
  // plain | glitch | matrix | typewriter | boot
  fontScale: 100,
  showHidden: false
  // reveal cousins + loops marked hidden=true in cousin.toml
};
function loadSettings() {
  try {
    return { ...SETTINGS_DEFAULTS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {} };
  } catch (e) {
    return { ...SETTINGS_DEFAULTS };
  }
}
function saveSettings(v) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(v));
  } catch (e) {
  }
}
function applySettings(s) {
  const r = document.documentElement;
  const hue = Number(s.accentHue) || 355;
  const chroma = Number(s.accentChroma) || 0.18;
  r.style.setProperty("--accent", `oklch(0.70 ${chroma} ${hue})`);
  r.style.setProperty("--accent-bright", `oklch(0.78 ${chroma} ${hue})`);
  r.style.fontSize = (Number(s.fontScale) || 100) + "%";
  window.__fwSettings = s;
  window.dispatchEvent(new CustomEvent("fw-settings-changed", { detail: s }));
}
function useSetting(key) {
  const [v, setV] = React.useState(() => window.__fwSettings && window.__fwSettings[key]);
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
    if (newPw !== newPw2) {
      setMsg({ kind: "err", text: "new passwords differ" });
      return;
    }
    if (newPw.length < 8) {
      setMsg({ kind: "err", text: "new password must be at least 8 chars" });
      return;
    }
    setBusy(true);
    try {
      const { r, d } = await apiSend("POST", "/api/auth/change-password", { old_password: oldPw, new_password: newPw });
      if (r.ok && d.ok) {
        setMsg({ kind: "ok", text: `password updated for ${d.user}; this session stays valid.` });
        setOldPw("");
        setNewPw("");
        setNewPw2("");
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
    try {
      await apiSend("POST", "/api/auth/logout");
    } catch (_e) {
    }
    const next = await fetchAuthMe();
    setMe(next);
    if (setAuth) setAuth(next);
  };
  const user = me?.user;
  const configured = !!(me && me.configured);
  const inputStyle = { padding: 6, border: "1px solid var(--line)", borderRadius: 3, background: "var(--bg-0)", color: "var(--fg-0)" };
  return /* @__PURE__ */ React.createElement("div", { className: "panel", style: { marginTop: 14 } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "account")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { padding: 18, display: "flex", flexDirection: "column", gap: 12 } }, !configured ? /* @__PURE__ */ React.createElement("div", { style: { fontSize: 12, color: "var(--fg-3)", fontFamily: "var(--mono)" } }, "auth is not configured: the network guard is the only boundary. Provision a user with", /* @__PURE__ */ React.createElement("code", { style: { marginLeft: 6 } }, "cousin-console adduser <name>")) : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { style: { fontSize: 12, color: "var(--fg-3)", fontFamily: "var(--mono)", display: "flex", gap: 10, alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", null, "logged in as ", /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg-0)" } }, user || "..."), me?.users?.length > 1 && /* @__PURE__ */ React.createElement("span", null, " · other users: ", me.users.filter((u) => u !== user).join(", "))), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: logout, style: { fontSize: 10, padding: "2px 8px", minHeight: 18 } }, "log out")), /* @__PURE__ */ React.createElement("label", { style: { display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" } }, "current password", /* @__PURE__ */ React.createElement("input", { type: "password", value: oldPw, onChange: (e) => setOldPw(e.target.value), style: inputStyle, autoComplete: "current-password" })), /* @__PURE__ */ React.createElement("label", { style: { display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" } }, "new password (min 8 chars)", /* @__PURE__ */ React.createElement("input", { type: "password", value: newPw, onChange: (e) => setNewPw(e.target.value), style: inputStyle, autoComplete: "new-password" })), /* @__PURE__ */ React.createElement("label", { style: { display: "flex", flexDirection: "column", gap: 4, fontSize: 11, color: "var(--fg-3)" } }, "retype new password", /* @__PURE__ */ React.createElement("input", { type: "password", value: newPw2, onChange: (e) => setNewPw2(e.target.value), style: inputStyle, autoComplete: "new-password" })), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: busy || !oldPw || !newPw || !newPw2, onClick: submit }, busy ? "..." : "change password")), msg && /* @__PURE__ */ React.createElement("div", { style: {
    fontSize: 12,
    padding: 8,
    borderRadius: 3,
    background: msg.kind === "err" ? "oklch(from var(--err, #ef4444) l c h / 0.18)" : "oklch(from var(--ok, #22c55e) l c h / 0.18)",
    color: msg.kind === "err" ? "var(--err, #ef4444)" : "var(--ok, #22c55e)"
  } }, msg.text))));
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
    try {
      localStorage.removeItem(SETTINGS_KEY);
    } catch (_e) {
    }
    setS({ ...SETTINGS_DEFAULTS });
    applySettings(SETTINGS_DEFAULTS);
  };
  const hues = [0, 30, 60, 120, 180, 210, 240, 270, 300, 330, 355];
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad", style: { maxWidth: 720 } }, /* @__PURE__ */ React.createElement("div", { className: "panel" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "cosmetic"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: reset }, "reset defaults")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 18, padding: 18 } }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 8 } }, "accent hue"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 } }, hues.map((h) => /* @__PURE__ */ React.createElement(
    "button",
    {
      key: h,
      onClick: () => update({ accentHue: h }),
      title: `hue ${h}`,
      style: {
        width: 28,
        height: 28,
        borderRadius: 3,
        border: s.accentHue === h ? "2px solid var(--fg-0)" : "1px solid var(--line)",
        background: `oklch(0.70 ${s.accentChroma} ${h})`,
        cursor: "pointer"
      }
    }
  ))), /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "range",
      min: "0",
      max: "360",
      value: s.accentHue,
      onChange: (e) => update({ accentHue: Number(e.target.value) }),
      style: { width: "100%" }
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 11, fontFamily: "var(--mono)", color: "var(--fg-3)" } }, s.accentHue)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 8 } }, "accent saturation"), /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "range",
      min: "0",
      max: "0.28",
      step: "0.01",
      value: s.accentChroma,
      onChange: (e) => update({ accentChroma: Number(e.target.value) }),
      style: { width: "100%" }
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 11, fontFamily: "var(--mono)", color: "var(--fg-3)" } }, s.accentChroma.toFixed(2))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 8 } }, "chat stream speed"), /* @__PURE__ */ React.createElement("div", { className: "radio-row" }, ["off", "slow", "normal", "fast"].map((v) => /* @__PURE__ */ React.createElement(
    "button",
    {
      key: v,
      className: s.chatStreamSpeed === v ? "sel" : "",
      onClick: () => update({ chatStreamSpeed: v })
    },
    v
  )))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 8 } }, "chat stream effect"), /* @__PURE__ */ React.createElement("div", { className: "radio-row" }, [
    ["plain", "word-by-word"],
    ["glitch", "block-glyph scramble"],
    ["matrix", "cascading katakana"],
    ["typewriter", "char-by-char"],
    ["boot", "bios-style sparse"]
  ].map(([v, desc]) => /* @__PURE__ */ React.createElement(
    "button",
    {
      key: v,
      className: s.chatStreamEffect === v ? "sel" : "",
      title: desc,
      onClick: () => update({ chatStreamEffect: v })
    },
    v
  ))), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, color: "var(--fg-3)", fontFamily: "var(--mono)", marginTop: 4 } }, "plain = word reveal · glitch = block-glyph scramble · matrix = katakana rain · typewriter = char-by-char · boot = sparse chunks with dots.")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 8 } }, "font scale · ", s.fontScale, "%"), /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "range",
      min: "85",
      max: "140",
      step: "5",
      value: s.fontScale,
      onChange: (e) => update({ fontScale: Number(e.target.value) }),
      style: { width: "100%" }
    }
  )))), /* @__PURE__ */ React.createElement("div", { className: "panel", style: { marginTop: 16 } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "visibility")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { padding: 18 } }, /* @__PURE__ */ React.createElement("label", { style: { display: "flex", alignItems: "center", gap: 10, cursor: "pointer", fontSize: 13 } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "checkbox",
      checked: !!s.showHidden,
      onChange: (e) => update({ showHidden: e.target.checked })
    }
  ), /* @__PURE__ */ React.createElement("span", null, "show hidden cousins + loops")), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 6, fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--mono)" } }, "Cousins or loops marked with ", /* @__PURE__ */ React.createElement("code", null, "hidden = true"), " in their ", /* @__PURE__ */ React.createElement("code", null, "cousin.toml"), ' are filtered out by default. Toggle this on to reveal them (they get a "hidden" chip when shown). Use the inspector to flip the flag per item.'))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 12, fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" } }, "Settings persist in this browser's local storage. Applied live via CSS variables."), /* @__PURE__ */ React.createElement(RestartPanel, { auth, setAuth }), /* @__PURE__ */ React.createElement(Slot, { name: "settings.panels", auth, setAuth }));
}
function RestartPanel({ auth, setAuth }) {
  const [confirm2, setConfirm] = React.useState(null);
  const [busy, setBusy] = React.useState(null);
  const [msg, setMsg] = React.useState(null);
  React.useEffect(() => {
    if (!confirm2) return;
    const t = setTimeout(() => setConfirm(null), 4e3);
    return () => clearTimeout(t);
  }, [confirm2]);
  React.useEffect(() => {
    if (!msg) return;
    const t = setTimeout(() => setMsg(null), 8e3);
    return () => clearTimeout(t);
  }, [msg]);
  const restartConsole = async () => {
    if (confirm2 !== "console") {
      setConfirm("console");
      return;
    }
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
      window.dispatchEvent(new CustomEvent("fw-restart", { detail: { target: "console", etaSeconds: d.eta_seconds || 4 } }));
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
    } finally {
      setBusy(null);
    }
  };
  const btn = (key, label, onClick, tone) => /* @__PURE__ */ React.createElement(
    "button",
    {
      className: `btn ${tone || ""}`,
      onClick,
      disabled: busy === key,
      style: { minWidth: 160 }
    },
    busy === key ? "restarting..." : confirm2 === key ? "click again to confirm" : label
  );
  return /* @__PURE__ */ React.createElement("div", { className: "panel", style: { marginTop: 14 } }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "process control"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", fontSize: 11, marginLeft: 8 } }, "restart the console process; cousins restart from their inspector")), /* @__PURE__ */ React.createElement("div", { className: "panel-body", style: { display: "flex", flexDirection: "column", gap: 10, padding: 14 } }, msg && /* @__PURE__ */ React.createElement("div", { style: {
    padding: "6px 10px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: msg.ok ? "var(--green)" : "var(--red)",
    background: msg.ok ? "oklch(from var(--green) l c h / 0.08)" : "oklch(from var(--red) l c h / 0.08)",
    borderRadius: 3
  } }, msg.text), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } }, btn("console", "restart console", restartConsole, "danger"), /* @__PURE__ */ React.createElement("span", { style: { fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--mono)" } }, "~4s downtime · sessions do not survive it (you will log in again) · needs a supervisor that restarts the unit")), /* @__PURE__ */ React.createElement("div", { style: {
    fontSize: 11,
    color: "var(--fg-3)",
    fontFamily: "var(--mono)",
    padding: "8px 10px",
    borderTop: "1px solid var(--line-soft)",
    marginTop: 4
  } }, "Per-cousin restart lives in the Cousins tab: open a cousin's inspector drawer; the ", /* @__PURE__ */ React.createElement("code", null, "restart"), " button sits next to start/stop.")), /* @__PURE__ */ React.createElement(AccountPanel, { auth, setAuth }));
}
const FLEET_RUNNER_WORDS = {
  idle: "idle",
  running: "working",
  waiting_permission: "needs you",
  rate_limited: "rate limited",
  rolling_over: "rolling over",
  errored: "errored",
  stopped: "stopped"
};
const FLIP_NEVER = ["never", "off", "none", "no"];
function fleetRunnerKind(c) {
  if (!c) return "-";
  if (c.remote || c.type === "remote") return "remote";
  if (c.runner) return c.runner.kind || "runner";
  if (c.type === "worker") return "worker";
  return c.tmuxSession ? "tmux" : "-";
}
function fleetAttention(c) {
  if (c && c.supervisor && c.supervisor.state === "failing" && !c.remote)
    return { level: "needs", why: "failing: " + (c.supervisor.reason || "left down by the supervisor") };
  if (!c || c.status !== "running") return null;
  if (c.loginRequired && !c.remote) return { level: "needs", why: fleetLoginWhy(c.loginRequired) };
  const r = c.runner;
  if (r && r.alive) {
    if (r.state === "waiting_permission") return { level: "needs", why: "waiting for a permission" };
    if (r.state === "errored") return { level: "needs", why: "the runner errored" };
    if (r.state === "rate_limited") return { level: "warn", why: "rate limited" };
  }
  if (c.chat === "down") return { level: "warn", why: "chat server down" };
  return null;
}
function fleetLoginWhy(l) {
  const what = l && l.reason === "billing" ? "billing stopped" : "login required";
  return l && l.action ? what + ": " + l.action : what;
}
function fleetLoginWaits(cousins) {
  return (cousins || []).filter((c) => !c.remote && c.loginRequired);
}
function fleetState(c) {
  if (!c) return { word: "-", tone: "gray", pulse: false };
  if (c.remote) {
    const s = c.remoteState || (c.online ? "online" : "offline");
    return { word: s, tone: s === "online" ? "green" : s === "revoked" ? "red" : s === "pending" ? "amber" : "gray", pulse: false };
  }
  if (c.status !== "running") return { word: "stopped", tone: "gray", pulse: false };
  const att = fleetAttention(c);
  if (att && att.level === "needs") {
    return {
      word: c.runner && c.runner.state === "errored" ? "errored" : "needs you",
      tone: c.runner && c.runner.state === "errored" ? "red" : "amber",
      pulse: false
    };
  }
  if (c.runner) {
    const st = c.runner.state;
    return {
      word: FLEET_RUNNER_WORDS[st] || st || "starting",
      tone: st === "rate_limited" ? "amber" : "green",
      pulse: st === "running" || st === "rolling_over"
    };
  }
  if (c.type === "worker") return { word: "enrolled", tone: "green", pulse: false };
  return c.active ? { word: "working", tone: "green", pulse: true } : { word: "idle", tone: "green", pulse: false };
}
function fleetRank(c) {
  const a = fleetAttention(c);
  if (a) return a.level === "needs" ? 0 : 1;
  return c && c.status === "running" ? 2 : 3;
}
function fleetOrder(cousins) {
  return (cousins || []).map((c, i) => [c, i]).sort((x, y) => fleetRank(x[0]) - fleetRank(y[0]) || x[1] - y[1]).map((p) => p[0]);
}
function fleetPad2(n) {
  return String(n).padStart(2, "0");
}
function fleetIn(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m";
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60);
  return m ? h + "h " + m + "m" : h + "h";
}
function fleetEvery(sec) {
  const n = Number(sec);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 60) return n + "s";
  if (n < 3600) return Math.round(n / 60) + "m";
  const h = Math.floor(n / 3600), m = Math.round(n % 3600 / 60);
  return m ? h + "h " + m + "m" : h + "h";
}
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
  return { at: fleetPad2(h) + ":" + fleetPad2(mi), ts: d.getTime(), inSec: Math.round((d.getTime() - now) / 1e3) };
}
function fleetNextFlip(cousins, now) {
  let best = null, onDefault = 0;
  for (const c of cousins || []) {
    if (c.remote || c.type === "worker" || c.status !== "running") continue;
    const f = nextFlip(c.flipAt, now);
    if (!f) {
      if (c.flipAt == null || String(c.flipAt).trim() === "") onDefault += 1;
      continue;
    }
    if (f.never) continue;
    if (!best || f.ts < best.ts) best = { at: f.at, ts: f.ts, inSec: f.inSec, slugs: [c.slug] };
    else if (f.ts === best.ts) best.slugs.push(c.slug);
  }
  if (best) best.onDefault = onDefault;
  return best;
}
function fleetHealth(cousins, now) {
  const all = cousins || [];
  const local = all.filter((c) => !c.remote);
  const running = all.filter((c) => c.status === "running");
  const needs = running.filter((c) => (fleetAttention(c) || {}).level === "needs");
  const warn = running.filter((c) => (fleetAttention(c) || {}).level === "warn");
  const parts = [running.length + " of " + all.length + " running"];
  parts.push(needs.length ? needs.length + (needs.length === 1 ? " needs" : " need") + " you" : "nothing needs you");
  if (warn.length) parts.push(warn.length + (warn.length === 1 ? " warning" : " warnings"));
  const flip = fleetNextFlip(local, now);
  if (flip) parts.push("next flip " + flip.at + ", in " + fleetIn(flip.inSec));
  return {
    text: parts.join(" · "),
    running: running.length,
    total: all.length,
    needs: needs.length,
    warn: warn.length,
    nextFlip: flip
  };
}
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
    const tone = j.status === "failed" ? "red" : j.status === "running" ? "accent" : j.status === "done" ? "green" : j.status === "lost" ? "amber" : "gray";
    out.push({ key: "j" + j.id, ts: at, who: j.spawned_by, text: j.title, word: j.status === "running" ? "job started" : "job " + j.status, tone });
  }
  for (const [i, f] of (fires || []).entries()) {
    out.push({
      key: "f" + i + f.cousin + f.loop,
      ts: now - (Number(f.ago) || 0) * 1e3,
      who: f.cousin,
      text: f.loop === "context-heartbeat" ? "context heartbeat" : f.loop,
      word: "fired",
      tone: "gray"
    });
  }
  for (const [i, f] of (flips || []).entries()) {
    out.push({
      key: "p" + i + f.ts,
      ts: f.ts,
      who: f.slug,
      text: f.phase === "complete" && f.gen != null ? "flipped to generation " + f.gen : "flip " + f.phase,
      word: "flip",
      tone: f.phase === "failed" ? "red" : f.phase === "complete" ? "green" : "accent"
    });
  }
  return out.sort((a, b) => b.ts - a.ts).slice(0, 16);
}
function fmtClock(ts) {
  const d = new Date(ts);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}
function HostView({ onOpen }) {
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  const [h, setH] = React.useState(HOST_SEED);
  const [allCousins, setAllCousins] = React.useState([]);
  const [allFires, setAllFires] = React.useState([]);
  const [allLoops, setAllLoops] = React.useState([]);
  const [daemon, setDaemon] = React.useState(null);
  const [jobs, setJobs] = React.useState([]);
  const [now, setNow] = React.useState(Date.now());
  const cousins = React.useMemo(
    () => (allCousins || []).filter((c) => showHidden || !c.hidden),
    [allCousins, showHidden]
  );
  const hiddenCousinSlugs = React.useMemo(
    () => new Set((allCousins || []).filter((c) => c.hidden).map((c) => c.slug)),
    [allCousins]
  );
  const loops = React.useMemo(
    () => (allLoops || []).filter((l) => showHidden || !hiddenCousinSlugs.has(l.cousin)),
    [allLoops, showHidden, hiddenCousinSlugs]
  );
  const fires = React.useMemo(
    () => (allFires || []).filter((f) => showHidden || !hiddenCousinSlugs.has(f.cousin)),
    [allFires, showHidden, hiddenCousinSlugs]
  );
  const visibleJobs2 = React.useMemo(
    () => (jobs || []).filter((j) => showHidden || !hiddenCousinSlugs.has(j.spawned_by)),
    [jobs, showHidden, hiddenCousinSlugs]
  );
  React.useEffect(() => {
    let cancelled = false;
    async function pull() {
      const [host, cs, loopsData, recent, jobsData] = await Promise.all([
        fetchHost(),
        fetchCousins(),
        fetchLoopsFull(),
        apiGet("/api/loops/recent"),
        apiGet("/api/jobs?since_hours=24")
      ]);
      if (cancelled) return;
      if (host) setH(host);
      setAllCousins(cs || []);
      setAllLoops(loopsData.loops || []);
      setDaemon(loopsData.daemon || recent && recent.daemon || null);
      setAllFires(recent && recent.fires || []);
      if (jobsData) setJobs(jobsData.jobs || []);
      setNow(Date.now());
    }
    pull();
    const id = setInterval(pull, 5e3);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);
  const cpuPct = h.cpu?.pct ?? 0;
  const memPct = h.mem?.total ? h.mem.used / h.mem.total * 100 : 0;
  const diskPct = h.disk?.total ? h.disk.used / h.disk.total * 100 : 0;
  const running = cousins.filter((c) => c.status === "running");
  const totalTokensToday = cousins.reduce((s, c) => s + (c.tokensSpent || 0), 0);
  const health = fleetHealth(cousins, now);
  const ordered = fleetOrder(cousins);
  const jobsRunning = visibleJobs2.filter((j) => j.status === "running").length;
  const jobsFailed = visibleJobs2.filter((j) => j.status === "failed" || j.status === "lost").length;
  const events = fleetEvents(visibleJobs2, fires, FLEET_FLIPS, now);
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad ov" }, /* @__PURE__ */ React.createElement("div", { className: "ov-health", "data-fleet-health": true }, /* @__PURE__ */ React.createElement("span", { className: "led " + (health.needs ? "amber" : health.warn ? "amber" : "green") }), /* @__PURE__ */ React.createElement("span", null, health.text)), fleetLoginWaits(cousins).length > 0 && /* @__PURE__ */ React.createElement("div", { className: "ov-login", "data-login-required": true }, fleetLoginWaits(cousins).map((c) => /* @__PURE__ */ React.createElement("div", { key: c.slug, className: "ov-login-row", title: c.loginRequired.since ? `since ${c.loginRequired.since}` : void 0 }, /* @__PURE__ */ React.createElement("span", { className: "led amber" }), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("b", null, "@", c.slug), " ", c.loginRequired.reason === "billing" ? "billing stopped" : "waits for a login", c.account ? ` on account ${c.account}` : ""), c.loginRequired.action && /* @__PURE__ */ React.createElement("code", { className: "ov-login-action" }, c.loginRequired.action)))), /* @__PURE__ */ React.createElement("div", { className: "ov-stats", "data-fleet-stats": true }, /* @__PURE__ */ React.createElement("div", { className: "ov-stat" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "running"), /* @__PURE__ */ React.createElement("div", { className: "ov-stat-v" }, /* @__PURE__ */ React.createElement("span", { className: "ov-big" }, running.length), /* @__PURE__ */ React.createElement("span", { className: "ov-of" }, "of ", cousins.length, " cousins")), /* @__PURE__ */ React.createElement("div", { className: "ov-stat-s" + (health.needs ? " amber" : "") }, health.needs ? `${health.needs} need${health.needs === 1 ? "s" : ""} you` : "nothing needs you")), /* @__PURE__ */ React.createElement("div", { className: "ov-stat" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "tokens today"), /* @__PURE__ */ React.createElement("div", { className: "ov-stat-v" }, /* @__PURE__ */ React.createElement("span", { className: "ov-big" }, fmtTokens(totalTokensToday))), /* @__PURE__ */ React.createElement("div", { className: "ov-stat-s" }, "all cousins")), /* @__PURE__ */ React.createElement("div", { className: "ov-stat" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "jobs, 24h"), /* @__PURE__ */ React.createElement("div", { className: "ov-stat-v" }, /* @__PURE__ */ React.createElement("span", { className: "ov-big" }, jobsRunning), /* @__PURE__ */ React.createElement("span", { className: "ov-of" }, "running")), /* @__PURE__ */ React.createElement("div", { className: "ov-stat-s" + (jobsFailed ? " red" : "") }, jobsFailed, " failed · ", visibleJobs2.length, " in all")), /* @__PURE__ */ React.createElement("div", { className: "ov-stat" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "loops"), /* @__PURE__ */ React.createElement("div", { className: "ov-stat-v" }, /* @__PURE__ */ React.createElement("span", { className: "ov-big" }, loops.length), /* @__PURE__ */ React.createElement("span", { className: "ov-of" }, "loops")), /* @__PURE__ */ React.createElement("div", { className: "ov-stat-s" + (daemon && daemon.ok === false ? " amber" : "") }, daemon && daemon.ok === false ? daemon.message : `${fires.length} recent fires`))), /* @__PURE__ */ React.createElement("div", { className: "ov-grid" }, /* @__PURE__ */ React.createElement("section", { className: "panel ov-fleet" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "Fleet"), /* @__PURE__ */ React.createElement("span", null, running.length, "/", cousins.length, " running"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { title: "the order: needs you, warnings, running, stopped" }, "needs you first")), /* @__PURE__ */ React.createElement("div", { className: "table-scroll" }, /* @__PURE__ */ React.createElement("table", { className: "data fleet-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "state"), /* @__PURE__ */ React.createElement("th", null, "cousin"), /* @__PURE__ */ React.createElement("th", null, "runner"), /* @__PURE__ */ React.createElement("th", null, "model"), /* @__PURE__ */ React.createElement("th", null, "next flip"), /* @__PURE__ */ React.createElement("th", null, "beat"), /* @__PURE__ */ React.createElement("th", null, "operator"), /* @__PURE__ */ React.createElement("th", { className: "num" }, "tokens today"))), /* @__PURE__ */ React.createElement("tbody", null, ordered.map((c) => {
    const st = fleetState(c);
    const att = fleetAttention(c);
    const kind = fleetRunnerKind(c);
    const flip = nextFlip(c.flipAt, now);
    const openable = onOpen && c.status === "running" && c.type !== "worker" && !c.remote;
    return /* @__PURE__ */ React.createElement(
      "tr",
      {
        key: c.slug,
        "data-fleet-row": c.slug,
        className: "fleet-row" + (c.status !== "running" ? " off" : "") + (openable ? " link" : ""),
        onClick: openable ? () => onOpen(c.slug) : void 0,
        title: openable ? `open the chat with @${c.slug}` : void 0
      },
      /* @__PURE__ */ React.createElement("td", { "data-label": "state" }, /* @__PURE__ */ React.createElement("span", { className: "fleet-state tone-" + st.tone }, /* @__PURE__ */ React.createElement("span", { className: "led " + st.tone + (st.pulse ? " pulse" : "") }), st.word)),
      /* @__PURE__ */ React.createElement("td", { "data-label": "cousin", className: "fleet-who" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", { className: "fleet-name" }, c.name || c.slug), " ", /* @__PURE__ */ React.createElement("span", { className: "fleet-slug" }, "@", c.slug), c.hidden ? /* @__PURE__ */ React.createElement("span", { className: "fleet-slug" }, " · hidden") : null), att ? /* @__PURE__ */ React.createElement("div", { className: "fleet-why", title: att.why }, att.why) : c.role ? /* @__PURE__ */ React.createElement("div", { className: "fleet-role", title: c.role }, c.role) : null),
      /* @__PURE__ */ React.createElement("td", { "data-label": "runner" }, /* @__PURE__ */ React.createElement("div", { className: "mono" }, kind), /* @__PURE__ */ React.createElement("div", { className: "fleet-sub" }, c.runner ? c.runner.alive ? "" : "(not running)" : c.chat === "ok" ? "chat ok" : c.chat === "down" ? "chat down" : c.chat === "none" ? "no chat server" : c.chat || "")),
      /* @__PURE__ */ React.createElement("td", { "data-label": "model" }, /* @__PURE__ */ React.createElement("div", { className: "mono" }, c.model || "lane default", c.modelDefault && /* @__PURE__ */ React.createElement("span", { className: "muted" }, " (default)")), c.effort && /* @__PURE__ */ React.createElement("div", { className: "fleet-sub" }, c.effort)),
      /* @__PURE__ */ React.createElement("td", { "data-label": "next flip", title: flip ? flip.never ? "flip_at = never" : `[lifecycle] flip_at = ${c.flipAt}` : "no flip_at of its own: the install default applies (config/harness.toml default_flip_at)" }, flip && !flip.never ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "mono" }, flip.at), /* @__PURE__ */ React.createElement("div", { className: "fleet-sub" }, "in ", fleetIn(flip.inSec))) : flip && flip.never ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, "never") : /* @__PURE__ */ React.createElement("span", { className: "muted" }, c.type === "worker" || c.remote ? "-" : "default")),
      /* @__PURE__ */ React.createElement("td", { "data-label": "beat", className: "mono", title: c.heartbeat != null ? `${c.heartbeat}s` : "" }, c.heartbeat != null ? fleetEvery(c.heartbeat) : "-"),
      /* @__PURE__ */ React.createElement("td", { "data-label": "operator", className: "muted" }, c.operator || "-"),
      /* @__PURE__ */ React.createElement("td", { "data-label": "tokens today", className: "num" }, fmtTokens(c.tokensSpent || 0))
    );
  }), cousins.length === 0 && /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", { colSpan: 8, className: "muted", style: { textAlign: "center", padding: 24 } }, "no cousins registered")))))), /* @__PURE__ */ React.createElement("aside", { className: "ov-rail" }, /* @__PURE__ */ React.createElement("section", { className: "panel" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, h.host || "host"), /* @__PURE__ */ React.createElement("span", { className: "mono" }, h.kernel), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { className: "fleet-state tone-green", title: "host uptime" }, /* @__PURE__ */ React.createElement("span", { className: "led green" }), " up ", fmtDuration(h.uptime || 0))), /* @__PURE__ */ React.createElement("div", { className: "panel-body ov-meters" }, /* @__PURE__ */ React.createElement(
    Meter,
    {
      label: "cpu",
      value: num(cpuPct).toFixed(1) + "%",
      sub: `load ${num(h.cpu?.load1).toFixed(2)} ${num(h.cpu?.load5).toFixed(2)} ${num(h.cpu?.load15).toFixed(2)}`,
      pct: num(cpuPct)
    }
  ), /* @__PURE__ */ React.createElement(
    Meter,
    {
      label: "memory",
      value: h.mem?.total ? num(h.mem.used).toFixed(1) + " / " + h.mem.total + " GB" : "-",
      sub: h.mem ? `cached ${num(h.mem.cached).toFixed(1)} GB` : "-",
      pct: num(memPct)
    }
  ), /* @__PURE__ */ React.createElement(
    Meter,
    {
      label: "disk /",
      value: h.disk?.total ? num(h.disk.used).toFixed(0) + " / " + h.disk.total + " GB" : "-",
      sub: h.disk?.total ? `free ${(num(h.disk.total) - num(h.disk.used)).toFixed(0)} GB` : "-",
      pct: num(diskPct)
    }
  ), /* @__PURE__ */ React.createElement(
    Meter,
    {
      label: "net",
      value: num(h.net?.rx).toFixed(2) + " MB/s rx",
      sub: num(h.net?.tx).toFixed(2) + " MB/s tx" + (h.net?.rx_total_gb != null ? ` · ${num(h.net.rx_total_gb).toFixed(1)}/${num(h.net.tx_total_gb).toFixed(1)} GB life` : ""),
      pct: Math.min(100, Math.max(num(h.net?.rx), num(h.net?.tx)) * 10)
    }
  ), h.console_uptime != null && /* @__PURE__ */ React.createElement("div", { className: "ov-note" }, "console up ", fmtDuration(h.console_uptime)))), /* @__PURE__ */ React.createElement("section", { className: "panel ov-activity" }, /* @__PURE__ */ React.createElement("div", { className: "panel-hdr" }, /* @__PURE__ */ React.createElement("span", { className: "title" }, "Activity"), /* @__PURE__ */ React.createElement("span", null, "jobs, loop fires, flips")), /* @__PURE__ */ React.createElement("div", { className: "ov-events", "data-fleet-events": true }, events.map((ev) => /* @__PURE__ */ React.createElement("div", { key: ev.key, className: "ov-event" }, /* @__PURE__ */ React.createElement("span", { className: "ov-event-t mono", title: new Date(ev.ts).toLocaleString() }, fmtClock(ev.ts)), /* @__PURE__ */ React.createElement("span", { className: "ov-event-dot tone-" + ev.tone }), /* @__PURE__ */ React.createElement("div", { className: "ov-event-b" }, /* @__PURE__ */ React.createElement("span", { className: "ov-event-who" }, ev.who), " ", /* @__PURE__ */ React.createElement("span", { className: "ov-event-w" }, ev.word), /* @__PURE__ */ React.createElement("div", { className: "ov-event-x", title: ev.text }, ev.text)))), events.length === 0 && /* @__PURE__ */ React.createElement("div", { className: "ov-note", style: { padding: "14px 16px" } }, "nothing yet: no jobs in the last 24 hours and no loop fires; the loops daemon fires loops at their configured schedule"))))));
}
function Meter({ label, value, sub, pct }) {
  const tone = pct > 88 ? "crit" : pct > 70 ? "warn" : "";
  return /* @__PURE__ */ React.createElement("div", { className: "ov-meter" }, /* @__PURE__ */ React.createElement("div", { className: "ov-meter-h" }, /* @__PURE__ */ React.createElement("span", { className: "eyebrow" }, label), /* @__PURE__ */ React.createElement("span", { className: "ov-meter-v" }, value)), /* @__PURE__ */ React.createElement(Bar, { pct, tone }), /* @__PURE__ */ React.createElement("div", { className: "ov-meter-s" }, sub));
}
Object.assign(window, {
  JobsView,
  MemoryView,
  LoopsView,
  TokensView,
  TrackerView,
  SettingsView,
  AccountPanel,
  RestartPanel,
  HostView,
  CousinTag,
  Field,
  Stat,
  TRACKER_STATES,
  fleetRunnerKind,
  fleetAttention,
  fleetState,
  fleetOrder,
  fleetHealth,
  fleetNextFlip,
  nextFlip,
  fleetIn,
  fleetLoginWhy,
  fleetLoginWaits
});
