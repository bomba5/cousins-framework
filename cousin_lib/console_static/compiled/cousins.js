function CousinsView({ cousins, setCousins, setActiveCousin }) {
  const [selected, setSelected] = React.useState(null);
  const [spawning, setSpawning] = React.useState(false);
  const [toast, setToast] = React.useState(null);
  const [confirmDeleteSlug, setConfirmDeleteSlug] = React.useState(null);
  const [flipModalCousin, setFlipModalCousin] = React.useState(null);
  const toastTimerRef = React.useRef(null);
  React.useEffect(() => () => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
  }, []);
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
        if (confirmDeleteSlug !== c.slug) {
          setConfirmDeleteSlug(c.slug);
          flash(`click delete again within 3.5s to dismiss ${c.slug} (the home is archived first)`, 3500);
          return;
        }
        setConfirmDeleteSlug(null);
        const { r: r2, d: d2 } = await apiSend("DELETE", `/api/cousins/${c.slug}`);
        if (r2.ok && d2.ok) {
          const left = (d2.left_in_place || []).length;
          flash(`dismissed ${c.slug} · archived to ${d2.archive}${left ? ` · ${left} harness dir(s) left in place` : ""}`, 6e3);
          setCousins((cs) => cs.filter((x) => x.slug !== c.slug));
        } else {
          flash("dismiss refused: " + (d2.error || `HTTP ${r2.status}`), 6e3);
        }
        return;
      }
      if (action === "flip") {
        setFlipModalCousin(c);
        return;
      }
      if (action === "restart") {
        const { r: r2, d: d2 } = await apiSend("POST", `/api/cousins/${c.slug}/restart`);
        if (r2.ok && d2.ok) flash(`restarted ${c.slug}`);
        else flash("restart failed: " + (d2.error || d2.start && d2.start.error || `HTTP ${r2.status}`), 4e3);
        return;
      }
      const { r, d } = action === "start" ? await apiSend("POST", `/api/cousins/${c.slug}/start`) : await apiSend("POST", `/api/cousins/${c.slug}/stop`);
      if (r.ok && d.ok) {
        flash((action === "start" ? "started " : "stopped ") + c.slug + (d.status === "already running" ? " (already running)" : ""));
      } else {
        flash("failed: " + (d.error || `HTTP ${r.status}`), 4e3);
      }
    } catch (e) {
      flash("error: " + (e.message || e), 4e3);
    }
  };
  const showHidden = window.useSetting && window.useSetting("showHidden") || false;
  const visibleCousins = cousins.filter((c) => showHidden || !c.hidden);
  const hiddenCount = cousins.filter((c) => c.hidden).length;
  const remoteCount = visibleCousins.filter((c) => c.remote).length;
  const actRemote = async (c, action) => {
    try {
      if (action === "revoke") {
        if (!window.confirm(`Revoke @${c.slug}? Its hive token stops working at once (every queen call answers 401). This cannot be undone; a rebuilt archive gets a new token.`)) return;
        const { r, d } = await apiSend("POST", `/api/hive/nodes/${c.slug}/revoke`);
        if (r.ok && d.ok) {
          flash(`revoked @${c.slug}`);
          setCousins((cs) => cs.map((x) => x.slug === c.slug ? { ...x, revoked: true, remoteState: "revoked", online: false, status: "stopped" } : x));
        } else flash("revoke failed: " + (d.error || `HTTP ${r.status}`), 4e3);
        return;
      }
      if (action === "forget") {
        const { r, d } = await apiSend("DELETE", `/api/hive/nodes/${c.slug}`);
        if (r.ok && d.ok) {
          flash(`forgot @${c.slug}`);
          setCousins((cs) => cs.filter((x) => x.slug !== c.slug));
        } else flash("forget failed: " + (d.error || `HTTP ${r.status}`), 4e3);
      }
    } catch (e) {
      flash("error: " + (e.message || e), 4e3);
    }
  };
  return /* @__PURE__ */ React.createElement("div", { className: "wrap-pad" }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 14, marginBottom: 16 } }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, visibleCousins.length, " cousins · ", visibleCousins.filter((c) => c.status === "running").length, " running", remoteCount > 0 && /* @__PURE__ */ React.createElement("span", { style: { marginLeft: 8 } }, "· ", remoteCount, " remote"), hiddenCount > 0 && !showHidden && /* @__PURE__ */ React.createElement("span", { style: { marginLeft: 8, color: "var(--fg-3)" } }, "· ", hiddenCount, " hidden")), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: () => setSpawning(true) }, I.plus, " spawn cousin")), /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 14 } }, visibleCousins.map((c) => c.remote ? /* @__PURE__ */ React.createElement(
    RemoteCousinCard,
    {
      key: c.slug,
      c,
      onAct: actRemote,
      onChat: () => setActiveCousin(c.slug)
    }
  ) : /* @__PURE__ */ React.createElement(
    CousinCard,
    {
      key: c.slug,
      c,
      onClick: () => setSelected(c.slug),
      onAct: act,
      onChat: () => setActiveCousin(c.slug)
    }
  ))), selected && /* @__PURE__ */ React.createElement(
    Inspector,
    {
      cousin: cousins.find((c) => c.slug === selected && !c.remote),
      onClose: () => setSelected(null),
      onAct: act
    }
  ), spawning && /* @__PURE__ */ React.createElement(
    SpawnModal,
    {
      onClose: () => setSpawning(false),
      onSpawn: (c) => {
        setCousins((cs) => [...cs.filter((x) => x.slug !== c.slug), c]);
        setSpawning(false);
        flash(`spawned cousin ${c.slug}`);
      }
    }
  ), flipModalCousin && /* @__PURE__ */ React.createElement(
    FlipModal,
    {
      cousin: flipModalCousin,
      onClose: () => setFlipModalCousin(null),
      onFlash: flash
    }
  ), toast && /* @__PURE__ */ React.createElement("div", { className: "toast" }, toast));
}
function CousinCard({ c, onClick, onAct, onChat }) {
  const isWorker = c.type === "worker";
  const st = window.fleetState ? fleetState(c) : { word: c.status === "running" ? c.active ? "working" : "idle" : "stopped", tone: c.status === "running" ? "green" : "gray", pulse: !!c.active };
  return /* @__PURE__ */ React.createElement("div", { className: "cousin-card", onClick }, /* @__PURE__ */ React.createElement("div", { className: "name-row" }, /* @__PURE__ */ React.createElement(Led, { state: c.status === "running" ? "running" : "stopped", pulse: c.status === "running" }), /* @__PURE__ */ React.createElement("div", { className: "name" }, c.name), /* @__PURE__ */ React.createElement("div", { className: "slug" }, "@", c.slug), /* @__PURE__ */ React.createElement("div", { style: { marginLeft: "auto", display: "flex", gap: 4 } }, isWorker && /* @__PURE__ */ React.createElement(Pill, { tone: "gray" }, "worker"), /* @__PURE__ */ React.createElement(Pill, { tone: st.tone }, st.word))), /* @__PURE__ */ React.createElement("div", { className: "role" }, c.role), c.supervisor && c.supervisor.state === "failing" && /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "card-attention",
      "data-runner-failing": true,
      title: "the supervisor left the runner down: start it again once the reason is fixed"
    },
    "runner failing · ",
    c.supervisor.reason || "left down by the supervisor"
  ), c.loginRequired && /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "card-attention",
      "data-login-required": true,
      title: c.loginRequired.since ? `data/login-required.json since ${c.loginRequired.since}` : "data/login-required.json"
    },
    c.loginRequired.reason === "billing" ? "billing stopped" : "login required",
    c.loginRequired.action ? /* @__PURE__ */ React.createElement(React.Fragment, null, " · run ", /* @__PURE__ */ React.createElement("code", null, c.loginRequired.action)) : null
  ), c.removedKeys && c.removedKeys.length > 0 && /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "card-attention",
      "data-removed-keys": true,
      title: c.removedKeys.map((k) => `${k.key}: ${k.line}`).join("\n")
    },
    "removed keys · ",
    c.removedKeys.map((k) => k.key).join(", "),
    " · run ",
    /* @__PURE__ */ React.createElement("code", null, "cousin-migrate tidy ", c.slug)
  ), /* @__PURE__ */ React.createElement("div", { style: { margin: "4px 0" } }, /* @__PURE__ */ React.createElement(HeartbeatGraph, { state: c.status === "running" ? c.active ? "active" : "idle" : "stopped", width: 240, height: 22 })), /* @__PURE__ */ React.createElement("div", { className: "stats" }, /* @__PURE__ */ React.createElement("div", null, "chat · ", /* @__PURE__ */ React.createElement("b", null, c.chat)), /* @__PURE__ */ React.createElement("div", null, "lane · ", /* @__PURE__ */ React.createElement("b", null, c.lane || "-"), c.held ? " · held" : "", c.autoStart === false ? " · no auto start" : ""), c.account && /* @__PURE__ */ React.createElement("div", null, "account · ", /* @__PURE__ */ React.createElement("b", null, c.account)), c.runner && /* @__PURE__ */ React.createElement("div", null, "runner · ", /* @__PURE__ */ React.createElement("b", null, c.runner.alive ? c.runner.state || "no state yet" : "(not running)"), c.runner.unsupported && c.runner.unsupported.length ? ` · unsupported: ${c.runner.unsupported.join(", ")}` : ""), /* @__PURE__ */ React.createElement("div", null, "scope · ", /* @__PURE__ */ React.createElement("b", null, c.memoryScope)), /* @__PURE__ */ React.createElement("div", null, "operator · ", /* @__PURE__ */ React.createElement("b", null, c.operator || "-")), /* @__PURE__ */ React.createElement("div", null, "beat · ", /* @__PURE__ */ React.createElement("b", null, c.heartbeat, "s")), /* @__PURE__ */ React.createElement("div", null, "flip at · ", /* @__PURE__ */ React.createElement("b", null, c.flipAt || "-")), /* @__PURE__ */ React.createElement("div", null, "model · ", /* @__PURE__ */ React.createElement("b", null, c.model || "lane default"), c.modelDefault && /* @__PURE__ */ React.createElement("span", { className: "muted" }, " (default)")), /* @__PURE__ */ React.createElement("div", null, "pid · ", /* @__PURE__ */ React.createElement("b", null, c.pid ?? "-")), /* @__PURE__ */ React.createElement("div", null, "uptime · ", /* @__PURE__ */ React.createElement("b", null, c.uptime_seconds == null ? "-" : fmtDuration(c.uptime_seconds)))), c.activity && /* @__PURE__ */ React.createElement(
    "div",
    {
      style: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-2)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" },
      title: c.activity
    },
    c.activity
  ), /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)", display: "flex", gap: 8, alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", null, "tokens today · ", /* @__PURE__ */ React.createElement("b", { style: { color: "var(--fg-1)" } }, fmtTokens(c.tokensSpent)))), /* @__PURE__ */ React.createElement("div", { className: "actions", onClick: (e) => e.stopPropagation() }, c.status === "running" ? /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => onAct(c, "stop") }, I.stop, " stop") : /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => onAct(c, "start") }, I.play, " start"), /* @__PURE__ */ React.createElement("button", { className: "btn danger", onClick: () => onAct(c, "delete"), title: "dismiss cousin (stop, archive the home, remove it)" }, "dismiss"), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }), !isWorker && /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => onChat(c.slug) }, "open chat →")));
}
function RemoteCousinCard({ c, onAct, onChat }) {
  const state = c.remoteState || (c.online ? "online" : "offline");
  const tone = { online: "green", offline: "gray", pending: "amber", revoked: "red" }[state] || "gray";
  const label = state === "pending" ? "built, not checked in" : state;
  const seen = c.lastSeen ? fmtAgo(Date.now() / 1e3 - c.lastSeen) : "never";
  return /* @__PURE__ */ React.createElement("div", { className: "cousin-card remote" }, /* @__PURE__ */ React.createElement("div", { className: "name-row" }, /* @__PURE__ */ React.createElement(Led, { state: c.online ? "running" : "stopped", pulse: c.online }), /* @__PURE__ */ React.createElement("div", { className: "name" }, c.name), /* @__PURE__ */ React.createElement("div", { className: "slug" }, "@", c.slug), /* @__PURE__ */ React.createElement("div", { style: { marginLeft: "auto", display: "flex", gap: 4 } }, /* @__PURE__ */ React.createElement(Pill, { tone: "cyan" }, "remote"), /* @__PURE__ */ React.createElement(Pill, { tone }, label))), /* @__PURE__ */ React.createElement("div", { className: "role" }, c.role), /* @__PURE__ */ React.createElement("div", { style: { margin: "4px 0" } }, /* @__PURE__ */ React.createElement(HeartbeatGraph, { state: c.online ? "idle" : "stopped", width: 240, height: 22 })), /* @__PURE__ */ React.createElement("div", { className: "stats" }, /* @__PURE__ */ React.createElement("div", null, "host · ", /* @__PURE__ */ React.createElement("b", null, c.host && c.port ? `${c.host}:${c.port}` : "-")), /* @__PURE__ */ React.createElement("div", null, "last seen · ", /* @__PURE__ */ React.createElement("b", null, seen)), /* @__PURE__ */ React.createElement("div", null, "runtime · ", /* @__PURE__ */ React.createElement("b", null, c.version || "-"))), /* @__PURE__ */ React.createElement("div", { className: "actions", onClick: (e) => e.stopPropagation() }, c.revoked ? /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => onAct(c, "forget"), title: "remove this revoked node from the console" }, "forget") : /* @__PURE__ */ React.createElement("button", { className: "btn danger", onClick: () => onAct(c, "revoke"), title: "revoke the node's hive token" }, "revoke"), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }), c.online && !c.revoked && /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => onChat(c.slug) }, "open chat →")));
}
function Inspector({ cousin: c, onClose, onAct }) {
  const [options, setOptions] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    apiGet("/api/spawn/options").then((d) => {
      if (!cancelled && d) setOptions(d);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  React.useEffect(() => {
    if (!c) return;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [c, onClose]);
  const [filesOpen, setFilesOpen] = React.useState(false);
  if (!c) return null;
  const tmuxLane = !c.lane || (options ? c.lane === options.tmux_lane : !c.runner);
  return /* @__PURE__ */ React.createElement("div", { className: "inspector" }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement(Led, { state: c.status === "running" ? "running" : "stopped", pulse: c.status === "running" }), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)" } }, c.name), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "@", c.slug), /* @__PURE__ */ React.createElement("button", { className: "close", onClick: onClose }, "×")), /* @__PURE__ */ React.createElement("div", { className: "body" }, /* @__PURE__ */ React.createElement(RoleEditor, { cousin: c }), /* @__PURE__ */ React.createElement(SectionLabel, null, "identity"), /* @__PURE__ */ React.createElement("dl", { className: "kv" }, /* @__PURE__ */ React.createElement("dt", null, "slug"), /* @__PURE__ */ React.createElement("dd", null, c.slug), /* @__PURE__ */ React.createElement("dt", null, "type"), /* @__PURE__ */ React.createElement("dd", null, c.type), /* @__PURE__ */ React.createElement("dt", null, "home"), /* @__PURE__ */ React.createElement("dd", { style: { wordBreak: "break-all" } }, c.home), /* @__PURE__ */ React.createElement("dt", null, "operator"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement(IdentityField, { cousin: c, field: "operator", options })), /* @__PURE__ */ React.createElement("dt", null, "scope"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement(IdentityField, { cousin: c, field: "memory_scope", options })), /* @__PURE__ */ React.createElement("dt", null, "tmux"), /* @__PURE__ */ React.createElement("dd", null, c.tmuxSession || "none"), /* @__PURE__ */ React.createElement("dt", null, "chat"), /* @__PURE__ */ React.createElement("dd", null, c.chat || "none"), /* @__PURE__ */ React.createElement("dt", null, "heartbeat"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement(IdentityField, { cousin: c, field: "heartbeat", options })), tmuxLane && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("dt", null, "model"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement(IdentityField, { cousin: c, field: "model", options })), /* @__PURE__ */ React.createElement("dt", null, "effort"), /* @__PURE__ */ React.createElement("dd", null, /* @__PURE__ */ React.createElement(IdentityField, { cousin: c, field: "effort", options }))), /* @__PURE__ */ React.createElement("dt", null, "lane"), /* @__PURE__ */ React.createElement("dd", null, c.lane || "-", c.account ? ` · account ${c.account}` : "", c.held ? " · held" : "", c.autoStart === false ? " · no auto start" : ""), /* @__PURE__ */ React.createElement("dt", null, "pid"), /* @__PURE__ */ React.createElement("dd", null, c.pid ?? /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "-")), /* @__PURE__ */ React.createElement("dt", null, "uptime"), /* @__PURE__ */ React.createElement("dd", null, c.uptime_seconds == null ? /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "-") : fmtDuration(c.uptime_seconds)), /* @__PURE__ */ React.createElement("dt", null, "flip at"), /* @__PURE__ */ React.createElement("dd", null, c.flipAt || /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "-")), /* @__PURE__ */ React.createElement("dt", null, "activity"), /* @__PURE__ */ React.createElement("dd", null, c.activity || /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "-"))), /* @__PURE__ */ React.createElement(Slot, { name: "inspector.lane", cousin: c }), /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "tokens · today"), /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "var(--mono)", fontSize: 12, color: "var(--fg-1)", marginTop: 6 } }, fmtTokens(c.tokensSpent), " spent"), /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "files"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, marginTop: 6 } }, /* @__PURE__ */ React.createElement("button", { className: "btn", "data-open-files": true, onClick: () => setFilesOpen(true) }, "browse home"), /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" } }, "read-only tree of the home; .secrets/ never shown")), filesOpen && /* @__PURE__ */ React.createElement(CousinFilesModal, { slug: c.slug, onClose: () => setFilesOpen(false) }), /* @__PURE__ */ React.createElement(PeerMessagePanel, { cousin: c }), /* @__PURE__ */ React.createElement(TelegramPanel, { cousin: c }), /* @__PURE__ */ React.createElement(ClaudeMdEditor, { cousin: c }), /* @__PURE__ */ React.createElement(LoopsEditor, { cousin: c }), /* @__PURE__ */ React.createElement(FlipStatus, { cousin: c }), /* @__PURE__ */ React.createElement(Slot, { name: "inspector.panels", cousin: c }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, marginTop: 20, flexWrap: "wrap" } }, c.status === "running" ? /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => onAct(c, "stop") }, I.stop, " stop") : /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => onAct(c, "start") }, I.play, " start"), /* @__PURE__ */ React.createElement("button", { className: "btn danger", onClick: () => onAct(c, "kill") }, I.kill, " kill"), c.status === "running" && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      onClick: () => onAct(c, "flip"),
      title: "cousin-flip: respawn on a fresh session-id, preserve identity via the boot packet"
    },
    "flip"
  ), c.status === "running" && /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      onClick: () => onAct(c, "restart"),
      title: "restart tmux + chat server (stop + start)"
    },
    "restart"
  ), /* @__PURE__ */ React.createElement(HideCousinButton, { cousin: c }), /* @__PURE__ */ React.createElement(Slot, { name: "inspector.actions", cousin: c }))));
}
function fmtBeat(sec) {
  if (sec == null || sec === "") return "-";
  const n = Number(sec);
  if (!Number.isFinite(n)) return String(sec);
  return `${n}s (${fmtDuration(n).replace(/ 0[smh]$/, "")})`;
}
const agentOrRuntime = (c) => c && c.runner ? "[agent]" : "[runtime]";
const IDENTITY_FIELDS = {
  operator: {
    url: (slug) => `/api/cousins/${slug}/operator`,
    row: "operator",
    kind: "text",
    title: "[operator] name"
  },
  memory_scope: {
    url: (slug) => `/api/cousins/${slug}/memory-scope`,
    row: "memoryScope",
    kind: "select",
    title: "[memory] scope",
    choices: (o) => o?.memory_scopes
  },
  heartbeat: {
    url: (slug) => `/api/cousins/${slug}/heartbeat`,
    row: "heartbeat",
    kind: "seconds",
    title: "[heartbeat] context_beat_seconds"
  },
  model: {
    url: (slug) => `/api/cousins/${slug}/model`,
    row: "model",
    kind: "select",
    title: (c) => `${agentOrRuntime(c)} model`,
    choices: (o) => o?.models
  },
  effort: {
    url: (slug) => `/api/cousins/${slug}/effort`,
    row: "effort",
    kind: "select",
    title: (c) => `${agentOrRuntime(c)} effort`,
    choices: (o) => o?.efforts
  }
};
function IdentityField({ cousin, field, options }) {
  const spec = IDENTITY_FIELDS[field];
  const current = cousin[spec.row];
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [hint, setHint] = React.useState(null);
  const [saved, setSaved] = React.useState(null);
  React.useEffect(() => {
    setSaved(null);
  }, [current]);
  React.useEffect(() => {
    setEditing(false);
    setErr(null);
    setHint(null);
    setSaved(null);
  }, [cousin.slug]);
  const shown = saved != null ? saved : current;
  const choices = spec.choices && spec.choices(options) || [];
  const bounds = options?.heartbeat_bounds || [60, 2592e3];
  const maxChars = options?.operator_max_chars || 64;
  const start = () => {
    setDraft(shown == null ? "" : String(shown));
    setErr(null);
    setHint(null);
    setEditing(true);
  };
  const cancel = () => {
    setEditing(false);
    setErr(null);
  };
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
    setBusy(true);
    setErr(null);
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
    const text = spec.kind === "seconds" ? fmtBeat(shown) : shown || /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "none");
    return /* @__PURE__ */ React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", null, text), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: start,
        style: small,
        title: `edit cousin.toml ${typeof spec.title === "function" ? spec.title(cousin) : spec.title}`
      },
      "edit"
    ), hint && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-2)", fontSize: 11 } }, hint));
  }
  const onKey = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      save();
    }
    if (e.key === "Escape") {
      e.stopPropagation();
      cancel();
    }
  };
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center" } }, spec.kind === "select" ? /* @__PURE__ */ React.createElement(
    "select",
    {
      className: "sel",
      value: draft,
      onChange: (e) => setDraft(e.target.value),
      autoFocus: true,
      disabled: !choices.length,
      style: { flex: 1, minWidth: 0 }
    },
    !choices.includes(draft) && /* @__PURE__ */ React.createElement("option", { value: draft }, draft || "..."),
    choices.map((ch) => /* @__PURE__ */ React.createElement("option", { key: ch, value: ch }, ch))
  ) : /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: draft,
      onChange: (e) => setDraft(e.target.value),
      onKeyDown: onKey,
      autoFocus: true,
      style: { flex: 1 },
      inputMode: spec.kind === "seconds" ? "numeric" : void 0,
      maxLength: spec.kind === "text" ? maxChars : void 0
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: cancel, disabled: busy, style: small }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: save, disabled: busy || !!problem, style: small }, busy ? "saving..." : "save")), spec.kind === "seconds" && !problem && /* @__PURE__ */ React.createElement("span", { style: { fontSize: 10, color: "var(--fg-3)" } }, fmtBeat(draft.trim())), problem && draft !== "" && /* @__PURE__ */ React.createElement("span", { style: { fontSize: 10, color: "var(--fg-3)" } }, problem), err && /* @__PURE__ */ React.createElement("div", { style: {
    padding: "5px 9px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: "var(--red)",
    background: "oklch(from var(--red) l c h / 0.08)",
    borderRadius: 3
  } }, err));
}
function fmtTgAt(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  if (isNaN(t.getTime())) return String(iso);
  const hm = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return t.toDateString() === (/* @__PURE__ */ new Date()).toDateString() ? hm : `${t.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`;
}
function TelegramPanel({ cousin }) {
  const [st, setSt] = React.useState(null);
  const [loadErr, setLoadErr] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [note, setNote] = React.useState(null);
  const [bot, setBot] = React.useState(null);
  const [newId, setNewId] = React.useState("");
  const [newName, setNewName] = React.useState("");
  const url = `/api/cousins/${cousin.slug}/telegram`;
  const load = React.useCallback(async () => {
    const d = await apiGet(url);
    if (d) {
      setSt(d);
      setLoadErr(false);
    } else setLoadErr(true);
  }, [url]);
  React.useEffect(() => {
    setSt(null);
    setLoadErr(false);
    setErr(null);
    setNote(null);
    setBot(null);
    setNewId("");
    setNewName("");
    load();
    const id = setInterval(load, 1e4);
    return () => clearInterval(id);
  }, [cousin.slug, load]);
  const post = async (path, body) => {
    setBusy(true);
    setErr(null);
    setNote(null);
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
    if (c.ok) {
      setBot(c.bot || "");
      setNote(c.bot ? `token works: @${c.bot}` : "token works");
    } else {
      setBot(null);
      setErr("check failed: " + (c.error || "unknown error"));
    }
  };
  const saveToken = async (token) => {
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
  const saveOperators = async (ops2, msg) => {
    const d = await post("/operators", { operators: ops2.map((o) => ({ user_id: o.user_id, name: o.name })) });
    if (d) setNote(msg);
    return !!d;
  };
  const ops = st?.operators || [];
  const pending = st?.pending || [];
  const idText = newId.trim();
  const idProblem = idText === "" ? null : !/^\d+$/.test(idText) || Number(idText) <= 0 ? "a numeric user id" : ops.some((o) => String(o.user_id) === idText) ? "already an operator" : null;
  const nameProblem = newName.length > 64 ? "at most 64 characters" : null;
  const addOperator = async () => {
    if (busy || !idText || idProblem || nameProblem) return;
    const ok = await saveOperators(
      [...ops, { user_id: Number(idText), name: newName.trim() }],
      `added ${newName.trim() || idText}`
    );
    if (ok) {
      setNewId("");
      setNewName("");
    }
  };
  const removeOperator = (uid) => {
    const gone = ops.find((o) => o.user_id === uid);
    saveOperators(ops.filter((o) => o.user_id !== uid), `removed ${gone?.name || uid}`);
  };
  const addPending = (p) => {
    const name = (p.first_name || p.username || "").slice(0, 64);
    saveOperators([...ops, { user_id: p.user_id, name }], `added ${name || p.user_id}`);
  };
  const small = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
  const mono = { fontFamily: "var(--mono)", fontSize: 11 };
  const hintStyle = { fontSize: 10, color: "var(--fg-3)" };
  if (!st) {
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "telegram"), /* @__PURE__ */ React.createElement("div", { style: { ...mono, color: "var(--fg-3)" } }, loadErr ? "telegram status unavailable" : "loading..."));
  }
  const blocker = !st.token_set ? "save a token first" : ops.length === 0 ? "add an operator first" : null;
  const empty = !st.token_set && ops.length === 0 && pending.length === 0;
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "telegram"), /* @__PURE__ */ React.createElement("div", { "data-telegram-panel": true, style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { style: { ...mono, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(Pill, { tone: st.enabled ? "green" : "gray" }, st.enabled ? "enabled" : "disabled"), /* @__PURE__ */ React.createElement(Pill, { tone: st.running ? "green" : "gray" }, st.running ? "bridge running" : "bridge not running"), /* @__PURE__ */ React.createElement("span", { style: { color: st.token_set ? "var(--fg-1)" : "var(--fg-3)" } }, st.token_set ? "token set" : "no token"), bot && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-1)" } }, "@", bot), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      style: small,
      disabled: busy || !st.token_set,
      onClick: check,
      title: "ask Telegram (getMe) whether the stored token works"
    },
    "check"
  )), st.ready && st.ready !== "disabled" && /* @__PURE__ */ React.createElement("div", { style: { ...mono, fontSize: 10, color: "var(--amber)" } }, st.ready), empty && /* @__PURE__ */ React.createElement("div", { style: { ...hintStyle, lineHeight: 1.6 } }, 'Setup: 1) create the bot with @BotFather, 2) paste the token below, 3) open the bot in Telegram and press Start, 4) add yourself from "waiting to be added", 5) enable.'), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, st.enabled ? /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: busy, onClick: () => setEnabled(false) }, "disable") : /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: busy || !!blocker, onClick: () => setEnabled(true) }, "enable"), !st.enabled && blocker && /* @__PURE__ */ React.createElement("span", { style: hintStyle }, blocker), st.enabled && !st.running && cousin.status !== "running" && /* @__PURE__ */ React.createElement("span", { style: hintStyle }, "starts with the cousin")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, /* @__PURE__ */ React.createElement(
    SecretField,
    {
      placeholder: st.token_set ? "paste a new bot token" : "paste the bot token",
      submitLabel: "save token",
      busy,
      onSubmit: saveToken
    }
  ), /* @__PURE__ */ React.createElement("span", { style: hintStyle }, "From @BotFather: /newbot or /mybots > API Token. Stored on the server only (0600); never shown again.")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { style: { ...mono, fontSize: 10, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 } }, "operators (", ops.length, ")"), /* @__PURE__ */ React.createElement("div", { style: {
    border: "1px solid var(--line)",
    borderRadius: 3,
    background: "var(--bg-0)",
    padding: 8,
    display: "flex",
    flexDirection: "column",
    gap: 6
  } }, ops.length === 0 && /* @__PURE__ */ React.createElement("div", { style: { ...mono, color: "var(--fg-3)" } }, "nobody yet: the bot answers no one."), ops.map((o) => /* @__PURE__ */ React.createElement("div", { key: o.user_id, style: { ...mono, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-0)" } }, o.name || /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "(no name)")), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, o.user_id), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn danger",
      style: small,
      disabled: busy,
      onClick: () => removeOperator(o.user_id),
      title: "remove from the allowed list"
    },
    "remove"
  ))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: newId,
      placeholder: "user id",
      inputMode: "numeric",
      style: { flex: "1 1 90px", minWidth: 0 },
      onChange: (e) => setNewId(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          addOperator();
        }
      }
    }
  ), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: newName,
      placeholder: "name",
      maxLength: 64,
      style: { flex: "2 1 120px", minWidth: 0 },
      onChange: (e) => setNewName(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          addOperator();
        }
      }
    }
  ), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: small,
      disabled: busy || !idText || !!idProblem || !!nameProblem,
      onClick: addOperator
    },
    "add"
  )), (idProblem || nameProblem) && /* @__PURE__ */ React.createElement("span", { style: hintStyle }, idProblem || nameProblem))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { style: { ...mono, fontSize: 10, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 } }, "waiting to be added (", pending.length, ")"), pending.length === 0 ? /* @__PURE__ */ React.createElement("div", { style: { ...mono, color: "var(--fg-3)" } }, "no one waiting.") : /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } }, pending.map((p) => /* @__PURE__ */ React.createElement("div", { key: p.user_id, style: { ...mono, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", color: "var(--fg-1)" } }, p.username && /* @__PURE__ */ React.createElement("span", null, "@", p.username), p.first_name && /* @__PURE__ */ React.createElement("span", null, p.username ? `(${p.first_name})` : p.first_name), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, p.user_id), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "pressed Start ", fmtTgAt(p.at)), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: small,
      disabled: busy,
      onClick: () => addPending(p),
      title: "append to the operators"
    },
    "add"
  )))), /* @__PURE__ */ React.createElement("div", { style: { ...hintStyle, marginTop: 4 } }, "A person presses Start on the bot; they appear here.")), note && /* @__PURE__ */ React.createElement("span", { style: { fontSize: 11, color: "var(--fg-2)" } }, note), err && /* @__PURE__ */ React.createElement("div", { style: {
    padding: "5px 9px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: "var(--red)",
    background: "oklch(from var(--red) l c h / 0.08)",
    borderRadius: 3
  } }, err), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: small, disabled: busy, onClick: load }, "refresh"))));
}
function HideCousinButton({ cousin: c }) {
  const [busy, setBusy] = React.useState(false);
  const [hidden, setHidden] = React.useState(!!c.hidden);
  React.useEffect(() => {
    setHidden(!!c.hidden);
  }, [c.hidden]);
  const toggle = async () => {
    setBusy(true);
    try {
      const { d } = await apiSend("POST", `/api/cousins/${c.slug}/hidden`, { hidden: !hidden });
      if (d.ok) setHidden(!!d.hidden);
    } catch (_e) {
    } finally {
      setBusy(false);
    }
  };
  return /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      onClick: toggle,
      disabled: busy,
      title: "hidden cousins disappear from the sidebar and the Cousins view (Settings: show hidden reveals them)"
    },
    hidden ? "unhide" : "hide"
  );
}
function FlipStatus({ cousin }) {
  const [st, setSt] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      const d = await apiGet(`/api/cousins/${cousin.slug}/flip`);
      if (!cancelled && d) setSt(d);
    };
    pull();
    const id = setInterval(pull, 5e3);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [cousin.slug]);
  const cancelPending = async () => {
    try {
      await apiSend("POST", `/api/cousins/${cousin.slug}/flip/cancel`);
    } catch (_e) {
    }
  };
  if (!st) return null;
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "flip"), /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "var(--mono)", fontSize: 11, color: "var(--fg-2)", display: "flex", flexDirection: "column", gap: 4 } }, /* @__PURE__ */ React.createElement("div", null, "last: ", /* @__PURE__ */ React.createElement(StatePill, { state: st.status === "done" ? "done" : st.status === "running" ? "running" : st.status === "failed" ? "failed" : "idle" }), st.status === "stale_marker" && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--amber)", marginLeft: 6 } }, "stale marker from a crashed flip"), st.result && st.result.new_generation != null && /* @__PURE__ */ React.createElement("span", { style: { marginLeft: 6 } }, "gen ", st.result.new_generation)), st.pending && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--amber)" } }, "timed flip pending · fires in ", fmtDuration(st.pending.seconds_until_fire), " (request #", st.pending.request_id, ")"), /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: { fontSize: 10, padding: "2px 8px", minHeight: 18 }, onClick: cancelPending }, "cancel"))));
}
function PeerMessagePanel({ cousin }) {
  const [peers, setPeers] = React.useState([]);
  const [to, setTo] = React.useState("");
  const [text, setText] = React.useState("");
  const [sending, setSending] = React.useState(false);
  const [status, setStatus] = React.useState(null);
  React.useEffect(() => {
    fetchCousins().then((all) => {
      const others = all.filter((c) => c.slug !== cousin.slug && c.type !== "worker" && !c.remote);
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
  return /* @__PURE__ */ React.createElement("div", { style: { marginTop: 20 } }, /* @__PURE__ */ React.createElement(SectionLabel, null, "message a peer"), peers.length === 0 ? /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)", fontSize: 11, fontFamily: "var(--mono)" } }, "no peers registered yet.") : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginBottom: 6 } }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: to, onChange: (e) => setTo(e.target.value), style: { width: 120 } }, peers.map((p) => /* @__PURE__ */ React.createElement("option", { key: p.slug, value: p.slug }, "@", p.slug))), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      value: text,
      onChange: (e) => setText(e.target.value),
      onKeyDown: (e) => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), send()),
      placeholder: "message (Enter to send)",
      style: { flex: 1 }
    }
  ), /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: send, disabled: !text.trim() || sending }, "send")), status && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, fontFamily: "var(--mono)", color: status.ok ? "var(--green)" : "var(--red)" } }, status.msg), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, color: "var(--fg-3)", marginTop: 4 } }, "Lands in the destination's own chat history as a line from ", cousin.name, "; that history is the record.")));
}
function RoleEditor({ cousin }) {
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(cousin.role || "");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    setDraft(cousin.role || "");
    setEditing(false);
    setErr(null);
  }, [cousin.slug]);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/role`, { role: draft });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      cousin.role = d.role;
      setEditing(false);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  if (!editing) {
    return /* @__PURE__ */ React.createElement("div", { style: { marginBottom: 18, lineHeight: 1.6, position: "relative", display: "flex", alignItems: "flex-start", gap: 8 } }, /* @__PURE__ */ React.createElement("span", { style: { flex: 1, fontSize: 12, color: "var(--fg-2)" } }, cousin.role || /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)" } }, "(no role set)")), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn ghost",
        onClick: () => setEditing(true),
        style: { fontSize: 10, padding: "2px 8px", minHeight: 18 }
      },
      "edit"
    ));
  }
  return /* @__PURE__ */ React.createElement("div", { style: { marginBottom: 18 } }, /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "txt code",
      value: draft,
      onChange: (e) => setDraft(e.target.value),
      autoFocus: true,
      style: { minHeight: 120 }
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginTop: 6, alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", { style: { fontSize: 10, fontFamily: "var(--mono)", color: draft.length > 5e3 ? "var(--red)" : "var(--fg-3)" } }, draft.length, " / 5000 chars"), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: () => {
    setDraft(cousin.role || "");
    setEditing(false);
    setErr(null);
  }, disabled: busy }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: save, disabled: busy || draft.length > 5e3 }, busy ? "saving..." : "save")), err && /* @__PURE__ */ React.createElement("div", { style: {
    marginTop: 6,
    padding: "5px 9px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: "var(--red)",
    background: "oklch(from var(--red) l c h / 0.08)",
    borderRadius: 3
  } }, err), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 4, fontSize: 10, color: "var(--fg-3)" } }, "Saved as the [cousin] role line in cousin.toml. The cousin needs a session restart to pick it up if CLAUDE.md references it."));
}
function ClaudeMdEditor({ cousin }) {
  const [open, setOpen] = React.useState(false);
  const [content, setContent] = React.useState("");
  const [original, setOriginal] = React.useState("");
  const [loaded, setLoaded] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const load = React.useCallback(async () => {
    setLoaded(false);
    setMsg(null);
    try {
      const r = await fetch(`/api/cousins/${cousin.slug}/claude-md`, { cache: "no-store" });
      const d = await r.json();
      if (r.ok && d.ok) {
        setContent(d.content || "");
        setOriginal(d.content || "");
        setLoaded(true);
        if (d.missing) setMsg({ ok: false, text: "CLAUDE.md not found - saving will create it" });
      } else {
        setMsg({ ok: false, text: d.error || `HTTP ${r.status}` });
        setLoaded(true);
      }
    } catch (e) {
      setMsg({ ok: false, text: String(e.message || e) });
      setLoaded(true);
    }
  }, [cousin.slug]);
  React.useEffect(() => {
    if (open && !loaded) load();
  }, [open, loaded, load]);
  React.useEffect(() => {
    setLoaded(false);
    setOpen(false);
  }, [cousin.slug]);
  const dirty = content !== original;
  const save = async () => {
    if (busy || !dirty) return;
    setBusy(true);
    setMsg(null);
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
    } finally {
      setBusy(false);
    }
  };
  const revert = () => {
    setContent(original);
    setMsg(null);
  };
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "CLAUDE.md", /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn ghost",
      onClick: () => setOpen((v) => !v),
      style: { marginLeft: 8, fontSize: 10, padding: "1px 8px", minHeight: 18 }
    },
    open ? "hide" : "edit"
  )), open && /* @__PURE__ */ React.createElement("div", { style: { marginTop: 6 } }, !loaded && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 11, color: "var(--fg-3)" } }, "loading..."), loaded && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "txt code",
      value: content,
      onChange: (e) => setContent(e.target.value),
      style: { minHeight: 320 }
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginTop: 6, alignItems: "center", flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: {
    fontSize: 10,
    fontFamily: "var(--mono)",
    color: dirty ? "var(--amber)" : "var(--fg-3)"
  } }, dirty ? `${content.length} chars · unsaved` : `${content.length} chars · clean`), /* @__PURE__ */ React.createElement("span", { style: { flex: 1 } }), dirty && /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: revert, disabled: busy }, "revert"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: save, disabled: !dirty || busy }, busy ? "saving..." : "save")), msg && /* @__PURE__ */ React.createElement("div", { style: {
    marginTop: 6,
    padding: "5px 9px",
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: msg.ok ? "var(--green)" : "var(--red)",
    background: msg.ok ? "oklch(from var(--green) l c h / 0.08)" : "oklch(from var(--red) l c h / 0.08)",
    borderRadius: 3
  } }, msg.text), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 4, fontSize: 10, color: "var(--fg-3)" } }, "The previous file is backed up to ", /* @__PURE__ */ React.createElement("code", null, "data/claude-md-backups/CLAUDE-<ts>.md"), " before the overwrite. The cousin needs a session restart to load the new file."))));
}
function LoopsEditor({ cousin }) {
  const [loops, setLoops] = React.useState([]);
  const [loaded, setLoaded] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [lastFires, setLastFires] = React.useState({});
  const [daemon, setDaemon] = React.useState(null);
  const loadLoops = React.useCallback(async () => {
    try {
      const r = await fetch(`/api/cousins/${cousin.slug}/loops`, { cache: "no-store" });
      const d = await r.json();
      setLoops(d.loops || []);
      setLastFires(d.last_fires || {});
      setDaemon(d.daemon || null);
      setLoaded(true);
    } catch (e) {
      setError(String(e.message || e));
      setLoaded(true);
    }
  }, [cousin.slug]);
  const refreshFires = React.useCallback(async () => {
    try {
      const r = await fetch(`/api/cousins/${cousin.slug}/loops`, { cache: "no-store" });
      const d = await r.json();
      setLastFires(d.last_fires || {});
      setDaemon(d.daemon || null);
    } catch (e) {
    }
  }, [cousin.slug]);
  React.useEffect(() => {
    loadLoops();
    const id = setInterval(refreshFires, 5e3);
    return () => clearInterval(id);
  }, [loadLoops, refreshFires]);
  const update = (i, patch) => setLoops((ls) => ls.map((l, idx) => idx === i ? { ...l, ...patch } : l));
  const add = () => setLoops((ls) => [...ls, { name: "new_loop", interval_seconds: 600, prompt: "What changed? Report a one-line status.", enabled: true }]);
  const remove = (i) => setLoops((ls) => ls.filter((_, idx) => idx !== i));
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const { r, d } = await apiSend("POST", `/api/cousins/${cousin.slug}/loops`, { loops });
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      loadLoops();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  };
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "loops (", loops.length, ")"), daemon && daemon.ok === false && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--amber)", fontSize: 10, fontFamily: "var(--mono)", marginBottom: 6 } }, daemon.message), /* @__PURE__ */ React.createElement("div", { style: { border: "1px solid var(--line)", borderRadius: 3, background: "var(--bg-0)", padding: 8 } }, !loaded && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)", fontSize: 11, fontFamily: "var(--mono)" } }, "loading..."), loaded && loops.length === 0 && /* @__PURE__ */ React.createElement("div", { style: { color: "var(--fg-3)", fontSize: 11, fontFamily: "var(--mono)" } }, "no custom loops yet. the context heartbeat fires every ", cousin.heartbeat, "s regardless."), loops.map((l, i) => {
    const fired = lastFires[l.name];
    const firedAgo = fired ? fmtAgo(Math.floor(Date.now() / 1e3 - fired)) : "never";
    const other = l.daily_at ? `daily ${l.daily_at}` : l.cron ? `cron ${l.cron}` : "";
    return /* @__PURE__ */ React.createElement("div", { key: i, style: { display: "grid", gridTemplateColumns: "100px 80px 1fr 40px 40px", gap: 6, marginBottom: 6, alignItems: "center" } }, /* @__PURE__ */ React.createElement("input", { value: l.name || "", onChange: (e) => update(i, { name: e.target.value }), placeholder: "name", style: inputStyle }), other ? /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" }, title: "edit daily/cron schedules from the Loops view" }, other) : /* @__PURE__ */ React.createElement("input", { type: "number", value: l.interval_seconds || 0, onChange: (e) => update(i, { interval_seconds: Number(e.target.value) }), placeholder: "sec", style: inputStyle }), /* @__PURE__ */ React.createElement("input", { value: l.prompt || "", onChange: (e) => update(i, { prompt: e.target.value }), placeholder: "prompt to inject", style: inputStyle }), /* @__PURE__ */ React.createElement("label", { style: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)", display: "inline-flex", alignItems: "center", gap: 3 } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: l.enabled !== false, onChange: (e) => update(i, { enabled: e.target.checked }) }), "on"), /* @__PURE__ */ React.createElement("button", { className: "btn danger", style: { padding: "2px 6px", fontSize: 10 }, onClick: () => remove(i), title: `last fired: ${firedAgo}` }, "×"));
  }), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, marginTop: 6 } }, /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: add, style: { fontSize: 11 } }, "+ loop"), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }), error && /* @__PURE__ */ React.createElement("span", { style: { color: "var(--red)", fontSize: 10, fontFamily: "var(--mono)" } }, error), /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: save, disabled: saving, style: { fontSize: 11 } }, saving ? "saving..." : "save"))));
}
const inputStyle = {
  fontFamily: "var(--sans)",
  fontSize: 12,
  background: "var(--bg-0)",
  color: "var(--fg-0)",
  border: "1px solid var(--line)",
  borderRadius: 6,
  padding: "4px 8px",
  outline: "none"
};
function SectionLabel({ children, style }) {
  return /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: {
    borderTop: "1px solid var(--hair)",
    paddingTop: 14,
    marginBottom: 10,
    ...style || {}
  } }, children);
}
function FlipModal({ cousin: c, onClose, onFlash }) {
  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const [typedSlug, setTypedSlug] = React.useState("");
  const [delay, setDelay] = React.useState(0);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [progress, setProgress] = React.useState(null);
  React.useEffect(() => {
    const onEvent = (e) => {
      const detail = e.detail;
      if (!detail || detail.slug !== c.slug) return;
      setProgress(detail);
      if (detail.phase === "complete" || detail.phase === "failed") {
        setTimeout(() => {
          onFlash(detail.phase === "complete" ? `${c.slug} flipped to gen ${detail.new_generation}` : `flip failed: ${detail.error || "see logs"}`);
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
      const body = delay > 0 ? { delay_seconds: delay } : {};
      const { r, d } = await apiSend("POST", `/api/cousins/${c.slug}/flip`, body);
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      if (delay > 0) {
        const mins = delay >= 60 ? `${Math.round(delay / 60)}m` : `${delay}s`;
        onFlash(`${c.slug} flip scheduled in ${mins} (request #${d.request_id})`);
        onClose();
      } else {
        setProgress({ phase: "started" });
      }
    } catch (e) {
      setError(String(e.message || e));
      setBusy(false);
    }
  };
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", onClick: (e) => e.stopPropagation() }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "cousin-flip"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", marginLeft: "auto" } }, "console / cousins / ", c.slug, " / flip")), /* @__PURE__ */ React.createElement("div", { className: "body" }, /* @__PURE__ */ React.createElement("p", { style: { color: "var(--fg-1)", fontSize: 12, lineHeight: 1.5, marginTop: 0 } }, "Respawns ", /* @__PURE__ */ React.createElement("b", null, "@", c.slug), " on a fresh session-id. The cousin reads a new boot packet and reconstructs identity. The conversation buffer is lost; durable state (memory, decisions, status, handoff) survives."), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--amber)", fontSize: 12, marginTop: 4 } }, "Blast radius: high. The cousin's tmux session is killed. Type ", /* @__PURE__ */ React.createElement("code", null, c.slug), " below to confirm."), /* @__PURE__ */ React.createElement(FormField, { label: "confirm slug", hint: `Type "${c.slug}" exactly.` }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      autoFocus: true,
      value: typedSlug,
      onChange: (e) => setTypedSlug(e.target.value),
      placeholder: c.slug,
      style: {
        borderColor: typedSlug && !slugMatches ? "var(--red)" : slugMatches ? "var(--green)" : void 0
      }
    }
  )), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }, /* @__PURE__ */ React.createElement(SectionLabel, null, "fire when"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 6, flexWrap: "wrap" } }, [
    { label: "now", v: 0 },
    { label: "in 1 min", v: 60 },
    { label: "in 5 min (warns at T-5/T-1/T-30)", v: 300 },
    { label: "in 15 min", v: 900 }
  ].map((o) => /* @__PURE__ */ React.createElement(
    "button",
    {
      key: o.v,
      className: "btn" + (delay === o.v ? " primary" : ""),
      onClick: () => setDelay(o.v)
    },
    o.label
  ))), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 10, color: "var(--fg-3)", marginTop: 8 } }, "A timed flip goes through the loops daemon, which warns the cousin at T-5m, T-1m and T-30s so it can wrap up tool calls and write a clean handoff.")), progress && /* @__PURE__ */ React.createElement("div", { style: {
    marginTop: 14,
    padding: 8,
    border: "1px solid var(--line)",
    borderRadius: 3,
    fontSize: 11,
    fontFamily: "var(--mono)",
    color: "var(--fg-1)"
  } }, progress.phase, progress.new_generation != null && ` · gen ${progress.new_generation}`, progress.boot_packet_tokens != null && ` · ${progress.boot_packet_tokens} tok`, progress.degraded_sections && progress.degraded_sections.length > 0 && ` · degraded: ${progress.degraded_sections.join(",")}`, progress.error && ` · error: ${progress.error}`), error && /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14, color: "var(--red)", fontSize: 11 } }, error), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, marginTop: 18, justifyContent: "flex-end" } }, /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: onClose, disabled: busy }, "close"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn primary",
      onClick: submit,
      disabled: !slugMatches || busy
    },
    busy ? delay > 0 ? "scheduling..." : "flipping..." : delay > 0 ? `schedule flip in ${delay >= 60 ? Math.round(delay / 60) + "m" : delay + "s"}` : "flip now"
  )))));
}
function SpawnModal({ onClose, onSpawn }) {
  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const [name, setName] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [role, setRole] = React.useState("");
  const [roleParagraph, setRoleParagraph] = React.useState("");
  const [voice, setVoice] = React.useState("");
  const [operator, setOperator] = React.useState("");
  const [options, setOptions] = React.useState(null);
  const [model, setModel] = React.useState("");
  const [effort, setEffort] = React.useState("");
  const [heartbeat, setHeartbeat] = React.useState("");
  const [scope, setScope] = React.useState("");
  const [runner, setRunner] = React.useState("");
  const [account, setAccount] = React.useState("");
  const [laneModel, setLaneModel] = React.useState("");
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      const d = await apiGet("/api/spawn/options");
      if (cancelled || !d) return;
      setOptions(d);
      setModel((m) => m || d.default_model || "");
      setEffort((e) => e || d.default_effort || "");
      setHeartbeat((h) => h || String(d.default_heartbeat || 3600));
      setScope((s) => s || d.default_memory_scope || "private");
      setRunner((r) => r || d.default_runner || "");
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  const models = options?.models || [];
  const efforts = options?.efforts || [];
  const scopes = options?.memory_scopes || [];
  const runners = options?.runners || [];
  const laneAccounts = (options?.accounts || []).filter((a) => runner && (a.lanes || []).includes(runner));
  const laneReads = (key) => !runner || ((options?.lane_keys || {})[runner] || []).includes(key);
  const laneModelRule = runner && (options?.lane_models || {})[runner] || null;
  const accountModels = (laneAccounts.find((a) => a.name === account) || {}).models || [];
  const laneSuggestions = accountModels.length ? accountModels : laneModelRule && laneModelRule.catalogue ? models : [];
  React.useEffect(() => {
    setAccount((a) => laneAccounts.some((x) => x.name === a) ? a : laneAccounts[0]?.name || "");
  }, [runner, options]);
  const [hive, setHive] = React.useState(null);
  const [mode, setMode] = React.useState("local");
  React.useEffect(() => {
    let cancelled = false;
    apiGet("/api/hive").then((d) => {
      if (!cancelled && d) setHive(d);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  React.useEffect(() => {
    if (!slug && name) setSlug(name.toLowerCase().replace(/[^a-z0-9]/g, ""));
  }, [name]);
  const valid = options && name.trim() && slug.trim() && role.trim() && voice.trim() && !(laneModelRule && laneModelRule.required && !laneModel.trim());
  const [error, setError] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const body = { slug, name, role, voice };
      if (roleParagraph.trim()) body.role_paragraph = roleParagraph.trim();
      if (operator.trim()) body.operator = operator.trim();
      body.runner = runner || options.default_runner;
      if (runner && account) body.account = account;
      const chosenModel = runner ? laneModel.trim() : model;
      if (chosenModel && laneReads("model")) body.model = chosenModel;
      if (effort && laneReads("effort")) body.effort = effort;
      if (String(heartbeat).trim()) body.heartbeat = Number(heartbeat);
      if (scope) body.memory_scope = scope;
      const { r, d } = await apiSend("POST", "/api/cousins", body);
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      const { r: r2, d: d2 } = await apiSend("POST", `/api/cousins/${slug}/start`);
      if (!r2.ok || !d2.ok) throw new Error("created but start failed: " + (d2.error || `HTTP ${r2.status}`));
      onSpawn({
        slug,
        name,
        role,
        type: "cousin",
        home: d.home,
        operator: operator.trim() || null,
        memoryScope: scope || "private",
        heartbeat: Number(heartbeat) || 3600,
        model: laneReads("model") && chosenModel || null,
        effort: laneReads("effort") && effort || null,
        lane: runner || options.default_runner,
        account: account || null,
        pid: null,
        uptime_seconds: null,
        flipAt: null,
        hidden: false,
        status: "running",
        chat: "ok",
        active: false,
        activity: "",
        lastMsgTs: 0,
        tokensSpent: 0
      });
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", onClick: (e) => e.stopPropagation() }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "spawn cousin"), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--fg-3)", marginLeft: "auto" } }, "console / cousins / new")), hive?.enabled && /* @__PURE__ */ React.createElement("div", { style: { padding: "12px 18px 0" } }, /* @__PURE__ */ React.createElement("div", { className: "radio-row" }, /* @__PURE__ */ React.createElement("button", { type: "button", className: mode === "local" ? "sel" : "", onClick: () => setMode("local") }, "This machine"), /* @__PURE__ */ React.createElement("button", { type: "button", className: mode === "remote" ? "sel" : "", onClick: () => setMode("remote") }, "Remote (another machine)"))), mode === "remote" && hive?.enabled ? /* @__PURE__ */ React.createElement(RemoteSpawnForm, { hive, onClose }) : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "body" }, /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "name", hint: "Display name (e.g. Wren)" }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: name, onChange: (e) => setName(e.target.value), placeholder: "Wren" })), /* @__PURE__ */ React.createElement(FormField, { label: "slug", hint: "Lowercase identifier used in paths and the tmux session" }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: slug, onChange: (e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9]/g, "")), placeholder: "wren" }))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement(FormField, { label: "role", hint: "One line. Shown on cards and in logs." }, /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: role, onChange: (e) => setRole(e.target.value), placeholder: "example cousin for this framework" })), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement(FormField, { label: "role paragraph (optional)", hint: "A paragraph of purpose for the identity template." }, /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: roleParagraph, onChange: (e) => setRoleParagraph(e.target.value), placeholder: "You are Wren, the example cousin that ships with this framework." })), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement(FormField, { label: "voice", hint: "Required: how the cousin writes. The template refuses to render without it." }, /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: voice, onChange: (e) => setVoice(e.target.value), placeholder: "Plain, short, and honest; says what it does not know." })), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "operator (optional)", hint: "The person this cousin answers to; blank is a real state." }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: operator, onChange: (e) => setOperator(e.target.value), placeholder: "none" }))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "lane", hint: "The runner kind; cousin-supervisor runs it." }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: runner, onChange: (e) => setRunner(e.target.value), disabled: !options }, !options && /* @__PURE__ */ React.createElement("option", { value: "" }, "..."), runners.map((k) => /* @__PURE__ */ React.createElement("option", { key: k, value: k }, k)))), /* @__PURE__ */ React.createElement(FormField, { label: "account", hint: runner ? "The credentials the runner uses (config/accounts.toml)." : "A runner lane only." }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: account, onChange: (e) => setAccount(e.target.value), disabled: !runner || !laneAccounts.length }, !runner && /* @__PURE__ */ React.createElement("option", { value: "" }, "-"), runner && !laneAccounts.length && /* @__PURE__ */ React.createElement("option", { value: "" }, "no account runs on this lane"), laneAccounts.map((a) => /* @__PURE__ */ React.createElement("option", { key: a.name, value: a.name }, a.name, " · ", a.kind))))), options?.accounts_error && /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--amber)", marginTop: 4 } }, "accounts: ", options.accounts_error), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement("div", { className: "grid2" }, !runner ? /* @__PURE__ */ React.createElement(FormField, { label: "model", hint: "Rendered into the agent command's {model} placeholder." }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: model, onChange: (e) => setModel(e.target.value), disabled: !options }, !options && /* @__PURE__ */ React.createElement("option", { value: "" }, "loading..."), models.map((m) => /* @__PURE__ */ React.createElement("option", { key: m, value: m }, m)))) : laneReads("model") ? /* @__PURE__ */ React.createElement(FormField, { label: "model", hint: "[agent] model, the one the runner reads: " + (laneModelRule && laneModelRule.hint || "a model name") }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      list: laneSuggestions.length ? "spawn-lane-models" : void 0,
      value: laneModel,
      onChange: (e) => setLaneModel(e.target.value),
      placeholder: laneModelRule && laneModelRule.required ? "<provider>/<model>" : "the runner's default"
    }
  ), laneSuggestions.length > 0 && /* @__PURE__ */ React.createElement("datalist", { id: "spawn-lane-models" }, laneSuggestions.map((m) => /* @__PURE__ */ React.createElement("option", { key: m, value: m })))) : /* @__PURE__ */ React.createElement(FormField, { label: "model", hint: "This lane reads no model." }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, "-")), laneReads("effort") ? /* @__PURE__ */ React.createElement(FormField, { label: "effort", hint: runner ? "[agent] effort, the one the runner reads." : "Rendered into the {effort} placeholder." }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: effort, onChange: (e) => setEffort(e.target.value), disabled: !options }, !options && /* @__PURE__ */ React.createElement("option", { value: "" }, "loading..."), efforts.map((l) => /* @__PURE__ */ React.createElement("option", { key: l, value: l }, l)))) : /* @__PURE__ */ React.createElement(FormField, { label: "effort", hint: "This lane reads no effort." }, /* @__PURE__ */ React.createElement("span", { className: "muted" }, "-"))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "heartbeat · seconds", hint: "The context heartbeat cadence ([heartbeat] context_beat_seconds)." }, /* @__PURE__ */ React.createElement("input", { className: "txt", type: "number", min: "1", value: heartbeat, onChange: (e) => setHeartbeat(e.target.value), placeholder: "3600" })), /* @__PURE__ */ React.createElement(FormField, { label: "memory scope", hint: "[memory] scope: private, shared with the fleet, or both." }, /* @__PURE__ */ React.createElement("div", { className: "radio-row" }, scopes.map((s) => /* @__PURE__ */ React.createElement("button", { key: s, type: "button", className: scope === s ? "sel" : "", onClick: () => setScope(s) }, s)))))), error && /* @__PURE__ */ React.createElement("div", { style: { padding: "8px 16px", color: "var(--red)", fontFamily: "var(--mono)", fontSize: 11, borderTop: "1px solid var(--line)" } }, "· ", error), /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: onClose }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !valid || busy, onClick: submit }, I.plus, " ", busy ? "creating..." : `create cousin ${slug || "-"}`)))));
}
function CopyButton({ text }) {
  const [done, setDone] = React.useState(false);
  const copy = async () => {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (_e) {
      ok = false;
    }
    if (!ok) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        ok = document.execCommand("copy");
      } catch (_e) {
        ok = false;
      }
      document.body.removeChild(ta);
    }
    setDone(ok ? "copied" : "select and copy by hand");
    setTimeout(() => setDone(false), 1800);
  };
  return /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: copy, style: { fontSize: 11 } }, done || "copy");
}
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
  const valid = slug.trim() && name.trim() && role.trim() && (brain === "placeholder" || agentCmd.trim());
  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const body = {
        slug,
        name: name.trim(),
        role: role.trim(),
        brain,
        home_chat: homeChat,
        reachable
      };
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
    const expires = new Date(built.expires_at * 1e3).toLocaleTimeString();
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "body" }, /* @__PURE__ */ React.createElement("p", { style: { color: "var(--fg-1)", fontSize: 12, lineHeight: 1.5, marginTop: 0 } }, "Built ", /* @__PURE__ */ React.createElement("b", null, "@", built.slug), ". On the other machine (python3 and outbound network to ", /* @__PURE__ */ React.createElement("code", null, hive.public_url), " are all it needs), run one of:"), /* @__PURE__ */ React.createElement(SectionLabel, null, "download and install"), /* @__PURE__ */ React.createElement("div", { className: "copy-row" }, /* @__PURE__ */ React.createElement("code", null, built.curl), /* @__PURE__ */ React.createElement(CopyButton, { text: built.curl })), /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 14 } }, "install an archive you already copied over"), /* @__PURE__ */ React.createElement("div", { className: "copy-row" }, /* @__PURE__ */ React.createElement("code", null, built.install), /* @__PURE__ */ React.createElement(CopyButton, { text: built.install })), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--amber)", fontSize: 12, marginTop: 14 } }, "The archive carries the node's bearer token. The link works once and expires at ", expires, "; after that, build again (the slug keeps its token)."), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--fg-3)", fontSize: 11 } }, `The card shows "built, not checked in" until the node's first checkin.`)), /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("button", { className: "btn primary", onClick: onClose }, "done")));
  }
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "body" }, /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "name", hint: "Display name (e.g. Kestrel)" }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: name, onChange: (e) => setName(e.target.value), placeholder: "Kestrel" })), /* @__PURE__ */ React.createElement(FormField, { label: "slug", hint: "The node's identity on the queen; its token is minted for it" }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: slug, onChange: (e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "")), placeholder: "kestrel" }))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement(FormField, { label: "role", hint: "One line: rendered into the node's CLAUDE.md and shown on its card." }, /* @__PURE__ */ React.createElement("textarea", { className: "txt", value: role, onChange: (e) => setRole(e.target.value), placeholder: "watches the greenhouse" })), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "node port", hint: "The node's own chat port on its machine." }, /* @__PURE__ */ React.createElement("input", { className: "txt", type: "number", value: port, onChange: (e) => setPort(e.target.value), placeholder: "8210" })), /* @__PURE__ */ React.createElement(FormField, { label: "brain", hint: "Placeholder greets and echoes; an agent command reads the prompt on stdin." }, /* @__PURE__ */ React.createElement("div", { className: "radio-row" }, /* @__PURE__ */ React.createElement("button", { type: "button", className: brain === "placeholder" ? "sel" : "", onClick: () => setBrain("placeholder") }, "placeholder"), /* @__PURE__ */ React.createElement("button", { type: "button", className: brain === "agent" ? "sel" : "", onClick: () => setBrain("agent") }, "agent command")))), brain === "agent" && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement(FormField, { label: "agent command", hint: "One command line as it runs ON THE NODE: prompt on stdin, reply on stdout (AGENT_CMD in node.env)." }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: agentCmd, onChange: (e) => setAgentCmd(e.target.value), placeholder: "/usr/local/bin/my-agent --plain" }))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 14 } }), /* @__PURE__ */ React.createElement("label", { style: { display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: "var(--fg-1)" } }, /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "checkbox",
      checked: homeChat,
      disabled: !hive.home_cousin,
      onChange: (e) => setHomeChat(e.target.checked)
    }
  ), "home chat", hive.home_cousin ? ` (${hive.home_cousin}, through the queen)` : " (set home_cousin in config/hive.toml)"), /* @__PURE__ */ React.createElement("label", { style: { display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: "var(--fg-1)", marginTop: 8 } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: reachable, onChange: (e) => setReachable(e.target.checked) }), "chat from this console (the node listens on its network and answers only its own token off loopback)")), error && /* @__PURE__ */ React.createElement("div", { style: { padding: "8px 16px", color: "var(--red)", fontFamily: "var(--mono)", fontSize: 11, borderTop: "1px solid var(--line)" } }, "· ", error), /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", onClick: onClose }, "cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn primary", disabled: !valid || busy, onClick: submit }, I.plus, " ", busy ? "building..." : `build node ${slug || "-"}`)));
}
function FormField({ label, hint, children }) {
  return /* @__PURE__ */ React.createElement("div", { className: "field" }, /* @__PURE__ */ React.createElement("label", null, label), children, hint && /* @__PURE__ */ React.createElement("div", { className: "hint" }, hint));
}
Object.assign(window, { CousinsView, CousinCard, RemoteCousinCard, RemoteSpawnForm, CopyButton, Inspector, IdentityField, TelegramPanel, RoleEditor, ClaudeMdEditor, LoopsEditor, FlipModal, SpawnModal, SectionLabel, FormField });
