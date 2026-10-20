const MIG_SMALL = { fontSize: 10, padding: "2px 8px", minHeight: 18 };
const MIG_MONO = { fontFamily: "var(--mono)", fontSize: 11 };
const MIG_HINT = { fontFamily: "var(--mono)", fontSize: 10, color: "var(--fg-3)" };
const MIG_ROW = { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" };
const MIG_COL = { display: "flex", flexDirection: "column", gap: 8 };
const inList = (list, item) => !!item && (list || []).indexOf(item) >= 0;
function migrateStageRows(op) {
  if (!op) return [];
  const stages = op.stages || [];
  const planned = op.params && op.params.steps || [];
  const byName = {};
  stages.forEach((s) => {
    byName[s.name] = s;
  });
  const rows = [];
  const seen = {};
  planned.forEach((name) => {
    seen[name] = true;
    rows.push(byName[name] || { name, status: op.status === "running" ? "pending" : "not run", detail: null });
  });
  stages.forEach((s) => {
    if (!seen[s.name]) rows.push(s);
  });
  return rows;
}
function screenInDetail(detail, screens) {
  const m = /screen: ([a-z_]+)/.exec(String(detail || ""));
  return m && inList(screens, m[1]) ? m[1] : null;
}
function paneWaitScreen(op, loginScreen, screens) {
  const stages = op && op.stages || [];
  for (let i = stages.length - 1; i >= 0; i--) {
    if (stages[i].status !== "running") continue;
    const s2 = screenInDetail(stages[i].detail, screens);
    if (s2) return s2;
  }
  const s = loginScreen && loginScreen.screen;
  return inList(screens, s) ? s : null;
}
function paneAction(screen, answers) {
  if (!screen) return null;
  return inList(answers, screen) ? "pane" : "terminal";
}
function paneButtonLabel(screen) {
  return screen === "trust" ? "open the pane to accept the trust dialog" : `open the pane to answer it (screen: ${screen})`;
}
function rollbackOffers(state) {
  const out = [];
  const sw = state && state.switch;
  if (sw && ["switching", "switched", "failed"].indexOf(sw.state) >= 0 && sw.from) {
    out.push({ which: "switch", to: sw.from, label: `roll the kind switch back to ${sw.from}` });
  }
  return out;
}
function confirmPhrase(template, donor, recipient) {
  if (!template) return "";
  return String(template).replace("{donor}", donor).replace("{recipient}", recipient);
}
function migrateBody(legacy, opts) {
  if (!legacy) return { to: opts.to };
  const body = { validate: !!opts.validate };
  if (opts.account) body.account = opts.account;
  return body;
}
function otherKind(lane, kinds) {
  const list = (kinds || []).filter((k) => k !== lane);
  return list.length ? list[0] : "";
}
async function migPost(path, body) {
  try {
    const { r, d } = await apiSend("POST", path, body || {});
    return { ok: r.ok, status: r.status, d: d || {} };
  } catch (e) {
    return { ok: false, status: 0, d: { error: "the console did not answer: " + String(e.message || e) } };
  }
}
async function migGet(path) {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return { error: d && d.error || `HTTP ${r.status}` };
    return { d };
  } catch (e) {
    return { error: "the console did not answer: " + String(e.message || e) };
  }
}
function MigLoadError({ error }) {
  if (!error) return null;
  return /* @__PURE__ */ React.createElement("div", { "data-migrate-load-error": true, style: { ...MIG_MONO, color: "var(--red)" } }, "could not load: ", error);
}
function useMigrateState(slug) {
  const [state, setState] = React.useState(null);
  const [error, setError] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!slug) {
      setState(null);
      return;
    }
    const res = await migGet(`/api/cousins/${slug}/migrate`);
    if (res.d) {
      setState(res.d);
      setError(null);
    } else setError(res.error);
  }, [slug]);
  React.useEffect(() => {
    load();
    const on = (e) => {
      if ((e.detail || {}).slug === slug) load();
    };
    window.addEventListener("fw-cousin-op", on);
    const tick = slug ? setInterval(load, 1e4) : null;
    return () => {
      window.removeEventListener("fw-cousin-op", on);
      if (tick) clearInterval(tick);
    };
  }, [slug, load]);
  return [state, load, error];
}
function MigMsg({ msg }) {
  if (!msg) return null;
  return /* @__PURE__ */ React.createElement("div", { style: { ...MIG_MONO, color: msg.err ? "var(--red)" : "var(--fg-2)", overflowWrap: "anywhere" } }, msg.text);
}
function MigratePaneModal({ slug, onClose }) {
  const PV = window.PaneView;
  const close = (e) => {
    if (e) e.stopPropagation();
    onClose();
  };
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: close }, /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "modal",
      "data-migrate-pane": true,
      style: { width: "min(1100px, 96vw)", maxWidth: "96vw" },
      onClick: (e) => e.stopPropagation()
    },
    /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, slug, ": the pane"), /* @__PURE__ */ React.createElement("span", { style: { ...MIG_HINT, marginLeft: 8 } }, "answer the dialog here (arrows and Enter); keys go in only while the pane waits on a person"), /* @__PURE__ */ React.createElement("button", { className: "close", onClick: close, style: { marginLeft: "auto" } }, "x")),
    /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", height: "70vh", minHeight: 0 } }, PV ? /* @__PURE__ */ React.createElement(PV, { cousin: { slug, tmuxSession: "tmux-" + slug }, onClose, serial: true }) : /* @__PURE__ */ React.createElement("div", { style: { ...MIG_MONO, padding: 20 } }, "the terminal view is not loaded"))
  ));
}
function hasPane(state, op) {
  const kinds = state && state.pane_kinds;
  return inList(kinds, state && state.lane) || inList(kinds, op && op.params && op.params.to);
}
function paneWait(state, op) {
  if (!state || !hasPane(state, op)) return null;
  const screen = op && op.status === "running" ? paneWaitScreen(op, state.loginScreen, state.person_screens) : paneWaitScreen(null, state.loginScreen, state.person_screens);
  return screen ? { screen, action: paneAction(screen, state.pane_answers) } : null;
}
function PaneWait({ wait, slug, onOpenPane }) {
  if (!wait) return null;
  if (wait.action === "pane") {
    return /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, /* @__PURE__ */ React.createElement("button", { className: "btn primary", style: MIG_SMALL, "data-open-pane": true, onClick: onOpenPane }, paneButtonLabel(wait.screen)), /* @__PURE__ */ React.createElement("span", { style: MIG_HINT }, "nothing else types into that screen; one key at a time"));
  }
  return /* @__PURE__ */ React.createElement("div", { style: { ...MIG_MONO, color: "var(--amber)" }, "data-pane-terminal": true }, "the pane waits on the ", wait.screen, " screen, a flow of several screens: finish it in a terminal attached to the pane (the framework's tmux socket, run/tmux.sock, session tmux-", slug, ")");
}
function MigrateOpStages({ op, state, onOpenPane }) {
  if (!op) return null;
  const rows = migrateStageRows(op);
  const wait = paneWait(state, op);
  const p = op.params || {};
  const what = [
    p.to && `to ${p.to}`,
    p.account && `account ${p.account}`,
    p.validate && "validated",
    p.mode,
    p.donor && `donor ${p.donor}`,
    p.new_role && `role "${p.new_role}"`,
    p.force && "forced"
  ].filter(Boolean).join(" · ");
  return /* @__PURE__ */ React.createElement("div", { className: "longop", "data-migrate-op": op.kind }, /* @__PURE__ */ React.createElement("div", { className: "longop-hdr" }, /* @__PURE__ */ React.createElement(StatePill, { state: op.status }), /* @__PURE__ */ React.createElement("span", { className: "mono" }, op.kind), what && /* @__PURE__ */ React.createElement("span", { className: "muted" }, what)), /* @__PURE__ */ React.createElement("ol", { className: "longop-stages" }, rows.map((s) => /* @__PURE__ */ React.createElement("li", { key: s.name }, /* @__PURE__ */ React.createElement(
    Led,
    {
      state: s.status === "skipped" ? "disabled" : s.status === "pending" || s.status === "not run" ? "stopped" : s.status,
      pulse: s.status === "running"
    }
  ), /* @__PURE__ */ React.createElement("span", null, s.name), s.status === "pending" || s.status === "not run" ? /* @__PURE__ */ React.createElement("span", { className: "muted" }, " · ", s.status) : null, s.detail && /* @__PURE__ */ React.createElement("span", { className: "muted" }, " · ", s.detail)))), /* @__PURE__ */ React.createElement(PaneWait, { wait, slug: op.slug, onOpenPane }), op.error && /* @__PURE__ */ React.createElement("div", { className: "longop-error" }, op.error));
}
function PlanChecklist({ plan }) {
  if (!plan) return null;
  const head = plan.from !== void 0 ? `${plan.slug}: ${plan.from || "?"} -> ${plan.to}` : `${plan.slug}: the tmux lane -> the sdk runner, account ${plan.account}`;
  return /* @__PURE__ */ React.createElement("div", { "data-migrate-plan": true, style: { display: "flex", flexDirection: "column", gap: 6 } }, /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, /* @__PURE__ */ React.createElement("span", { style: MIG_MONO }, head), /* @__PURE__ */ React.createElement(Pill, { tone: plan.ready ? "green" : "red" }, plan.ready ? "ready" : "not ready")), /* @__PURE__ */ React.createElement("ul", { style: { listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 } }, (plan.checks || []).map((c, i) => /* @__PURE__ */ React.createElement("li", { key: c.check + i, style: { ...MIG_MONO, display: "flex", gap: 6, alignItems: "baseline" } }, /* @__PURE__ */ React.createElement(Led, { state: c.ok ? "ok" : "failed" }), /* @__PURE__ */ React.createElement("span", { style: { minWidth: 70, color: "var(--fg-1)" } }, c.check), /* @__PURE__ */ React.createElement("span", { style: { color: c.ok ? "var(--fg-2)" : "var(--red)", overflowWrap: "anywhere" } }, c.check === "carry" && c.ok && plan.carry ? (plan.carry.rows || []).map((r, j) => /* @__PURE__ */ React.createElement("div", { key: j }, r.action, ": ", r.detail)) : c.detail)))), (plan.warnings || []).map((w, i) => /* @__PURE__ */ React.createElement("div", { key: "w" + i, style: { ...MIG_MONO, color: "var(--amber)" } }, "warn: ", w)), (plan.notes || []).map((n, i) => /* @__PURE__ */ React.createElement("div", { key: "n" + i, style: MIG_HINT }, "note: ", n)), /* @__PURE__ */ React.createElement("div", { style: MIG_HINT }, "steps: ", (plan.steps || []).join(" -> ")));
}
function DeferredList({ items }) {
  if (!items || !items.length) return null;
  return /* @__PURE__ */ React.createElement("div", { "data-migrate-deferred": true, style: { display: "flex", flexDirection: "column", gap: 2 } }, items.map((d) => /* @__PURE__ */ React.createElement("div", { key: d.id, style: MIG_HINT }, /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: MIG_SMALL, disabled: true, title: d.why }, d.label), " ", "not yet: ", d.why.replace(/^not yet: /, ""))));
}
function KindSwitchDialog({ slug, onClose }) {
  const [state, reloadState, loadError] = useMigrateState(slug);
  const [op] = useLongOp(slug);
  const lane = state && state.lane;
  const legacy = lane === "tmux-legacy";
  const [accountsError, setAccountsError] = React.useState(null);
  const [to, setTo] = React.useState("");
  const [account, setAccount] = React.useState("");
  const [validate, setValidate] = React.useState(false);
  const [accountNames, setAccountNames] = React.useState([]);
  const [plan, setPlan] = React.useState(null);
  const [planFor, setPlanFor] = React.useState(null);
  const [planOpId, setPlanOpId] = React.useState(null);
  const [applyOpId, setApplyOpId] = React.useState(null);
  const [armed, setArmed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const [paneOpen, setPaneOpen] = React.useState(false);
  React.useEffect(() => {
    if (state && !legacy && !to) setTo(otherKind(lane, state.kinds));
  }, [state && state.lane]);
  React.useEffect(() => {
    if (!legacy) return;
    migGet("/api/accounts").then((res) => {
      if (res.d) setAccountNames((res.d.accounts || []).map((a) => a.name));
      else setAccountsError(res.error);
    });
  }, [legacy]);
  const opts = { to, account, validate };
  const key = JSON.stringify(migrateBody(legacy, opts));
  const planFits = plan && planFor === key;
  React.useEffect(() => {
    setArmed(false);
  }, [key]);
  const planKeyRef = React.useRef(null);
  React.useEffect(() => {
    if (!planOpId || !op || op.id !== planOpId || op.status === "running") return;
    if (op.result && op.result.plan) {
      setPlan(op.result.plan);
      setPlanFor(planKeyRef.current);
    } else setMsg({ err: true, text: op.error || "the plan failed" });
    setPlanOpId(null);
  }, [planOpId, op && op.id, op && op.status]);
  const runPlan = async () => {
    setBusy(true);
    setMsg(null);
    setPlan(null);
    planKeyRef.current = key;
    const res = await migPost(`/api/cousins/${slug}/migrate/plan`, migrateBody(legacy, opts));
    setBusy(false);
    if (res.status === 202) {
      setPlanOpId(res.d.op.id);
      return;
    }
    if (!res.ok) {
      setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` });
      return;
    }
    setPlan(res.d.plan);
    setPlanFor(key);
  };
  const apply = async () => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    setBusy(true);
    setMsg(null);
    const res = await migPost(`/api/cousins/${slug}/migrate/apply`, { ...migrateBody(legacy, opts), confirm: true });
    setBusy(false);
    if (!res.ok) {
      setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` });
      return;
    }
    setApplyOpId(res.d.op.id);
    reloadState();
  };
  const opRunning = op && op.status === "running";
  const shownOp = op && (op.id === applyOpId || op.id === planOpId || opRunning && inList(state && state.op_kinds, op.kind)) ? op : null;
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", "data-kind-switch": slug, style: { width: "min(720px, 96vw)" }, onClick: (e) => e.stopPropagation() }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, legacy ? `migrate ${slug} to the runner` : `switch ${slug}'s kind`), /* @__PURE__ */ React.createElement("span", { style: { ...MIG_HINT, marginLeft: 8 } }, "cousin-migrate", legacy ? "" : " --to"), /* @__PURE__ */ React.createElement("button", { className: "close", onClick: onClose, style: { marginLeft: "auto" } }, "x")), /* @__PURE__ */ React.createElement("div", { className: "body", style: MIG_COL }, /* @__PURE__ */ React.createElement(MigLoadError, { error: loadError }), !state && !loadError && /* @__PURE__ */ React.createElement("div", { style: MIG_HINT }, "loading..."), state && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "lane now: ", /* @__PURE__ */ React.createElement("b", null, lane), state.supervisor ? "" : " · no cousin-supervisor runs: apply needs it", state.running && state.running.slug !== slug ? ` · a ${state.running.kind} runs on ${state.running.slug}: one at a time` : ""), legacy ? /* @__PURE__ */ React.createElement("div", { style: MIG_COL }, /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, /* @__PURE__ */ React.createElement("label", { style: MIG_MONO }, "account"), /* @__PURE__ */ React.createElement("select", { className: "sel", value: account, onChange: (e) => setAccount(e.target.value) }, /* @__PURE__ */ React.createElement("option", { value: "" }, "(what the cousin names, else the host login)"), accountNames.map((n) => /* @__PURE__ */ React.createElement("option", { key: n, value: n }, n))), accountsError && /* @__PURE__ */ React.createElement("span", { style: { ...MIG_HINT, color: "var(--red)" } }, "accounts not loaded: ", accountsError)), /* @__PURE__ */ React.createElement("label", { style: { ...MIG_MONO, display: "flex", gap: 6, alignItems: "flex-start" } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: validate, onChange: (e) => setValidate(e.target.checked) }), /* @__PURE__ */ React.createElement("span", null, "validate: spends one model turn (the smallest one, on the model, effort and account the runner will run, on a throwaway client). The plan spends one and the apply spends its own. A carried model is never written without it."))) : /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, /* @__PURE__ */ React.createElement("label", { style: MIG_MONO }, "to"), /* @__PURE__ */ React.createElement("select", { className: "sel", value: to, onChange: (e) => setTo(e.target.value) }, (state.kinds || []).map((k) => /* @__PURE__ */ React.createElement("option", { key: k, value: k }, k, k === lane ? " (now)" : ""))), /* @__PURE__ */ React.createElement("span", { style: MIG_HINT }, "the session goes on: the source stops at idle, the target resumes it")), /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, /* @__PURE__ */ React.createElement("button", { className: "btn", disabled: busy || !!planOpId || !legacy && !to, onClick: runPlan }, planOpId ? "planning..." : validate && legacy ? "plan (one model turn)" : "plan"), /* @__PURE__ */ React.createElement("span", { style: MIG_HINT }, "writes nothing")), planFits && /* @__PURE__ */ React.createElement(PlanChecklist, { plan }), plan && !planFits && /* @__PURE__ */ React.createElement("div", { style: MIG_HINT }, "the options changed: plan again"), /* @__PURE__ */ React.createElement(DeferredList, { items: state.deferred }), shownOp && /* @__PURE__ */ React.createElement(MigrateOpStages, { op: shownOp, state, onOpenPane: () => setPaneOpen(true) }), !shownOp && /* @__PURE__ */ React.createElement(PaneWait, { wait: paneWait(state, null), slug, onOpenPane: () => setPaneOpen(true) }), /* @__PURE__ */ React.createElement(MigMsg, { msg }))), /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: onClose }, "close"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn primary",
      disabled: busy || opRunning || !planFits || !plan.ready || !state || !state.supervisor,
      onClick: apply,
      title: planFits && plan.ready ? "" : "a ready plan for these options comes first"
    },
    armed ? `click again: this stops and restarts ${slug}` : legacy ? "apply the migration" : `switch to ${to}`
  ))), paneOpen && /* @__PURE__ */ React.createElement(MigratePaneModal, { slug, onClose: () => setPaneOpen(false) }));
}
function KindSwitchHost() {
  const [slug, setSlug] = React.useState(null);
  React.useEffect(() => {
    const on = (e) => {
      const s = (e.detail || {}).slug;
      if (s) setSlug(s);
    };
    window.addEventListener("fw-open-kind-switch", on);
    return () => window.removeEventListener("fw-open-kind-switch", on);
  }, []);
  if (!slug) return null;
  return /* @__PURE__ */ React.createElement(KindSwitchDialog, { key: slug, slug, onClose: () => setSlug(null) });
}
function openKindSwitchDialog(slug) {
  window.dispatchEvent(new CustomEvent("fw-open-kind-switch", { detail: { slug } }));
}
function CheckReport({ slug }) {
  const [since, setSince] = React.useState("");
  const [validate, setValidate] = React.useState(false);
  const [report, setReport] = React.useState(null);
  const [opId, setOpId] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const [op] = useLongOp(slug);
  React.useEffect(() => {
    if (!opId || !op || op.id !== opId || op.status === "running") return;
    if (op.result && op.result.check) setReport(op.result.check);
    else setMsg({ err: true, text: op.error || "the check failed" });
    setOpId(null);
  }, [opId, op && op.id, op && op.status]);
  const run = async () => {
    setBusy(true);
    setMsg(null);
    setReport(null);
    const body = { validate };
    if (since.trim()) body.since = since.trim();
    const res = await migPost(`/api/cousins/${slug}/migrate/check`, body);
    setBusy(false);
    if (res.status === 202) {
      setOpId(res.d.op.id);
      return;
    }
    if (!res.ok) {
      setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` });
      return;
    }
    setReport(res.d.check);
  };
  const c = report;
  return /* @__PURE__ */ React.createElement("div", { "data-migrate-check": true, style: MIG_COL }, /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "txt",
      style: { width: 190 },
      value: since,
      placeholder: "since (ISO time, optional)",
      onChange: (e) => setSince(e.target.value)
    }
  ), /* @__PURE__ */ React.createElement("label", { style: { ...MIG_HINT, display: "flex", gap: 4, alignItems: "center" } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: validate, onChange: (e) => setValidate(e.target.checked) }), "validate (spends one model turn)"), /* @__PURE__ */ React.createElement("button", { className: "btn", style: MIG_SMALL, disabled: busy || !!opId, onClick: run }, opId ? "checking..." : "check")), c && /* @__PURE__ */ React.createElement("div", { className: "longop", "data-migrate-report": true }, /* @__PURE__ */ React.createElement("div", { className: "longop-hdr" }, /* @__PURE__ */ React.createElement(Pill, { tone: c.ok ? "green" : "red" }, c.ok ? "ok" : "not ok"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, "since ", c.since ? new Date(c.since * 1e3).toISOString() : "the start")), /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "inbox: ", c.inbox_readable ? "" : "UNREADABLE · ", c.inbox.done, " done (", c.inbox.failed, " failed), ", c.inbox.open, " open, ", c.inbox.stale, " stale"), /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "tool calls: ", c.tool_calls, ", unrecorded: ", (c.unrecorded || []).join(", ") || "none"), /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "recorder hook errors: ", (c.hook_errors || []).join("; ") || "none"), c.chat !== void 0 && /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "chat server: ", c.chat), /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "runner CLI: ", c.cli), c.validate !== void 0 && /* @__PURE__ */ React.createElement("div", { style: { ...MIG_MONO, color: c.validate_ok ? "var(--fg-2)" : "var(--red)" } }, c.validate_ok ? "validate:" : "NOT VALID:", " ", c.validate), c.config && c.config.runner && /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "runner config: ", Object.entries(c.config.runner).map(([k, v]) => `${k}=${v}`).join(", ")), (c.mismatches || []).map((m, i) => /* @__PURE__ */ React.createElement("div", { key: "m" + i, style: { ...MIG_MONO, color: "var(--red)" } }, "MISMATCH ", m)), (c.warnings || []).map((w, i) => /* @__PURE__ */ React.createElement("div", { key: "w" + i, style: { ...MIG_MONO, color: "var(--amber)" } }, "warn ", w))), /* @__PURE__ */ React.createElement(MigMsg, { msg }));
}
function RollbackButton({ slug, offer, disabled, onStarted }) {
  const [armed, setArmed] = React.useState(0);
  const [force, setForce] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const needed = offer.which === "migration" && force ? 2 : 1;
  const click = async () => {
    if (armed < needed) {
      setArmed(armed + 1);
      return;
    }
    setArmed(0);
    setBusy(true);
    setMsg(null);
    const body = { which: offer.which, confirm: true };
    if (offer.to) body.to = offer.to;
    if (offer.which === "migration" && force) {
      body.force = true;
      body.force_confirm = true;
    }
    const res = await migPost(`/api/cousins/${slug}/migrate/rollback`, body);
    setBusy(false);
    if (!res.ok) setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` });
    else if (onStarted) onStarted();
  };
  const label = armed === 0 ? offer.label : armed === 1 && needed === 1 ? "click again to roll back: the runner stops and the saved cousin.toml comes back" : armed === 1 ? "click again to roll back, forced" : "force: inbox rows still waiting are left unread on the tmux lane. Click again to force";
  return /* @__PURE__ */ React.createElement("div", { "data-migrate-rollback": offer.which, style: MIG_COL }, /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, /* @__PURE__ */ React.createElement("button", { className: `btn ${armed ? "danger" : ""}`, style: MIG_SMALL, disabled: disabled || busy, onClick: click }, label), armed > 0 && /* @__PURE__ */ React.createElement("button", { className: "btn ghost", style: MIG_SMALL, onClick: () => setArmed(0) }, "keep"), offer.which === "migration" && /* @__PURE__ */ React.createElement("label", { style: { ...MIG_HINT, display: "flex", gap: 4, alignItems: "center" } }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: force, onChange: (e) => {
    setForce(e.target.checked);
    setArmed(0);
  } }), "force (with inbox rows waiting, or an inbox that cannot be read)")), /* @__PURE__ */ React.createElement(MigMsg, { msg }));
}
function recordLine(name, rec) {
  if (!rec) return `${name}: none`;
  const at = rec.rolled_back_at || rec.switched_at || rec.migrated_at || rec.started_at;
  const way = rec.from ? ` (${rec.from} -> ${rec.to})` : "";
  return `${name}: ${rec.state}${way}${at ? " · " + at : ""}`;
}
function MigratePanel({ cousin }) {
  const slug = cousin.slug;
  const local = cousin.type !== "remote";
  const [state, reloadState, loadError] = useMigrateState(local ? slug : null);
  const [op] = useLongOp(local ? slug : null);
  const [paneOpen, setPaneOpen] = React.useState(false);
  const [showCheck, setShowCheck] = React.useState(false);
  if (!local) return null;
  const running = op && op.status === "running";
  const migOp = op && inList(state && state.op_kinds, op.kind) && op.kind !== "migrate-plan" && op.kind !== "migrate-check" ? op : null;
  const lane = state && state.lane;
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "kind and migration"), /* @__PURE__ */ React.createElement("div", { "data-migrate-panel": true, style: MIG_COL }, /* @__PURE__ */ React.createElement(MigLoadError, { error: loadError }), !state && !loadError && /* @__PURE__ */ React.createElement("div", { style: MIG_HINT }, "loading..."), state && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "lane: ", /* @__PURE__ */ React.createElement("b", null, lane)), /* @__PURE__ */ React.createElement("div", { style: MIG_HINT }, recordLine("migration", state.migration)), /* @__PURE__ */ React.createElement("div", { style: MIG_HINT }, recordLine("kind switch", state.switch)), /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, state.refusal ? /* @__PURE__ */ React.createElement("div", { "data-migrate-refusal": true, style: MIG_HINT }, state.refusal) : /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn",
      style: MIG_SMALL,
      disabled: running,
      "data-open-kind-switch": true,
      onClick: () => openKindSwitchDialog(slug)
    },
    "switch kind..."
  ), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: `btn ${showCheck ? "active" : "ghost"}`,
      style: MIG_SMALL,
      onClick: () => setShowCheck(!showCheck)
    },
    "check"
  )), !migOp && /* @__PURE__ */ React.createElement(PaneWait, { wait: paneWait(state, null), slug, onOpenPane: () => setPaneOpen(true) }), showCheck && /* @__PURE__ */ React.createElement(CheckReport, { slug }), rollbackOffers(state).map((o) => /* @__PURE__ */ React.createElement(RollbackButton, { key: o.which, slug, offer: o, disabled: running, onStarted: reloadState })), migOp && /* @__PURE__ */ React.createElement(MigrateOpStages, { op: migOp, state, onOpenPane: () => setPaneOpen(true) }), /* @__PURE__ */ React.createElement(DeferredList, { items: state.deferred }))), paneOpen && /* @__PURE__ */ React.createElement(MigratePaneModal, { slug, onClose: () => setPaneOpen(false) }));
}
function useLifecycleModes() {
  const [modes, setModes] = React.useState(null);
  const [error, setError] = React.useState(null);
  React.useEffect(() => {
    migGet("/api/lifecycle/modes").then((res) => {
      if (res.d) setModes(res.d);
      else setError(res.error);
    });
  }, []);
  return [modes, error];
}
function useLifecycleHeld(slug) {
  const [held, setHeld] = React.useState(null);
  const [error, setError] = React.useState(null);
  const load = React.useCallback(async () => {
    if (!slug) return;
    const res = await migGet(`/api/cousins/${slug}/lifecycle`);
    if (res.d) {
      setHeld(res.d.held || null);
      setError(null);
    } else setError(res.error);
  }, [slug]);
  React.useEffect(() => {
    load();
    const on = (e) => {
      if ((e.detail || {}).kind === "transplant") load();
    };
    window.addEventListener("fw-cousin-op", on);
    return () => window.removeEventListener("fw-cousin-op", on);
  }, [load]);
  return [held, error];
}
function ReincarnateDialog({ cousin, onClose }) {
  const slug = cousin.slug;
  const [meta, metaError] = useLifecycleModes();
  const [op] = useLongOp(slug);
  const [role, setRole] = React.useState("");
  const [armed, setArmed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [opId, setOpId] = React.useState(null);
  const [msg, setMsg] = React.useState(null);
  const max = meta && meta.role_max || 200;
  const clean = role.trim();
  const valid = clean && clean.length <= max && !/[\r\n]/.test(role);
  const start = async () => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    setBusy(true);
    setMsg(null);
    const body = { new_role: clean, confirm: true };
    const res = await migPost(`/api/cousins/${slug}/reincarnate`, body);
    setBusy(false);
    if (!res.ok) {
      setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` });
      return;
    }
    setOpId(res.d.op.id);
  };
  const shown = op && (op.id === opId || op.status === "running" && op.kind === "reincarnate") ? op : null;
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", "data-reincarnate": slug, onClick: (e) => e.stopPropagation() }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "reincarnate ", slug), /* @__PURE__ */ React.createElement("span", { style: { ...MIG_HINT, marginLeft: 8 } }, "cousin-reincarnate"), /* @__PURE__ */ React.createElement("button", { className: "close", onClick: onClose, style: { marginLeft: "auto" } }, "x")), /* @__PURE__ */ React.createElement("div", { className: "body", style: MIG_COL }, /* @__PURE__ */ React.createElement("div", { style: MIG_HINT }, "The role changes, the memory stays. A snapshot of the continuity files first (data/lifecycle/", slug, "/), then the role is rewritten in CLAUDE.md and cousin.toml, then a flip, on whose own handoff request the cousin is asked for its bequest."), /* @__PURE__ */ React.createElement(MigLoadError, { error: metaError }), /* @__PURE__ */ React.createElement("div", { style: MIG_MONO }, "role now: ", cousin.role || "-"), /* @__PURE__ */ React.createElement(FormField, { label: "new role", hint: `one line, at most ${max} characters` }, /* @__PURE__ */ React.createElement("input", { className: "txt", value: role, maxLength: max, autoFocus: true, onChange: (e) => {
    setRole(e.target.value);
    setArmed(false);
  } })), shown && /* @__PURE__ */ React.createElement(MigrateOpStages, { op: shown }), /* @__PURE__ */ React.createElement(MigMsg, { msg })), /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: onClose }, "close"), /* @__PURE__ */ React.createElement("button", { className: `btn ${armed ? "danger" : "primary"}`, disabled: busy || !valid || op && op.status === "running", onClick: start }, armed ? `click again: rewrite the role and flip ${slug}` : "reincarnate"))));
}
function TransplantDialog({ cousin, onClose }) {
  const [meta, metaError] = useLifecycleModes();
  const [cousins, setCousins] = React.useState([]);
  const [cousinsError, setCousinsError] = React.useState(null);
  const [donor, setDonor] = React.useState("");
  const [recipient, setRecipient] = React.useState(cousin.slug);
  const [mode, setMode] = React.useState("merge");
  const [typed, setTyped] = React.useState("");
  const [armed, setArmed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [opId, setOpId] = React.useState(null);
  const [msg, setMsg] = React.useState(null);
  const [op] = useLongOp(recipient);
  React.useEffect(() => {
    migGet("/api/cousins").then((res) => {
      if (!res.d) {
        setCousinsError(res.error);
        return;
      }
      setCousins((res.d.cousins || []).filter((c) => c.type !== "remote").map((c) => c.slug));
    });
  }, []);
  React.useEffect(() => {
    setArmed(false);
    setTyped("");
  }, [donor, recipient, mode]);
  const modes = meta && meta.modes || [];
  const info = modes.find((m) => m.id === mode);
  const typedMode = info && info.confirm === "typed";
  const phrase = typedMode ? confirmPhrase(info.phrase, donor, recipient) : "";
  const ready = donor && recipient && donor !== recipient && info;
  const start = async () => {
    if (!typedMode && !armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    setBusy(true);
    setMsg(null);
    const res = await migPost(
      "/api/lifecycle/transplant",
      { donor, recipient, mode, confirm: typedMode ? typed : true }
    );
    setBusy(false);
    if (!res.ok) {
      setMsg({ err: true, text: res.d.error || `HTTP ${res.status}` });
      return;
    }
    setOpId(res.d.op.id);
  };
  const shown = op && (op.id === opId || op.status === "running" && op.kind === "transplant") ? op : null;
  return /* @__PURE__ */ React.createElement("div", { className: "modal-bg", onClick: onClose }, /* @__PURE__ */ React.createElement("div", { className: "modal", "data-transplant": true, onClick: (e) => e.stopPropagation() }, /* @__PURE__ */ React.createElement("div", { className: "hdr" }, /* @__PURE__ */ React.createElement("span", null, "transplant"), /* @__PURE__ */ React.createElement("span", { style: { ...MIG_HINT, marginLeft: 8 } }, "cousin-transplant"), /* @__PURE__ */ React.createElement("button", { className: "close", onClick: onClose, style: { marginLeft: "auto" } }, "x")), /* @__PURE__ */ React.createElement("div", { className: "body", style: MIG_COL }, /* @__PURE__ */ React.createElement(MigLoadError, { error: metaError || cousinsError }), /* @__PURE__ */ React.createElement("div", { className: "grid2" }, /* @__PURE__ */ React.createElement(FormField, { label: "donor", hint: "whose memory or body moves" }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: donor, onChange: (e) => setDonor(e.target.value) }, /* @__PURE__ */ React.createElement("option", { value: "" }, "(pick one)"), cousins.filter((s) => s !== recipient).map((s) => /* @__PURE__ */ React.createElement("option", { key: s, value: s }, s)))), /* @__PURE__ */ React.createElement(FormField, { label: "recipient", hint: "the op runs on it; the donor is held" }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: recipient, onChange: (e) => setRecipient(e.target.value) }, cousins.map((s) => /* @__PURE__ */ React.createElement("option", { key: s, value: s }, s))))), /* @__PURE__ */ React.createElement(FormField, { label: "mode", hint: info ? info.what : "" }, /* @__PURE__ */ React.createElement("select", { className: "sel", value: mode, onChange: (e) => setMode(e.target.value) }, modes.map((m) => /* @__PURE__ */ React.createElement("option", { key: m.id, value: m.id }, m.id, m.confirm === "typed" ? " (destructive)" : "")))), /* @__PURE__ */ React.createElement("div", { style: MIG_HINT }, "Both cousins are snapshotted first (data/lifecycle/<slug>/), then the mode is applied, then both flip, the donor first. Nothing else starts on either while it runs."), typedMode && ready && /* @__PURE__ */ React.createElement("div", { style: MIG_ROW, "data-transplant-typed": true }, /* @__PURE__ */ React.createElement("span", { style: { ...MIG_HINT, color: "var(--amber)" } }, mode, " replaces what ", recipient, " is: type ", /* @__PURE__ */ React.createElement("code", null, phrase), " to confirm"), /* @__PURE__ */ React.createElement("input", { className: "txt", style: { width: 200 }, value: typed, onChange: (e) => setTyped(e.target.value) })), shown && /* @__PURE__ */ React.createElement(MigrateOpStages, { op: shown }), /* @__PURE__ */ React.createElement(MigMsg, { msg })), /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("button", { className: "btn", onClick: onClose }, "close"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: `btn ${armed || typedMode ? "danger" : "primary"}`,
      disabled: busy || !ready || typedMode && (!phrase || typed !== phrase) || op && op.status === "running",
      onClick: start
    },
    armed ? `click again: ${mode} ${donor} into ${recipient}, then flip both` : "transplant"
  ))));
}
function LifecyclePanel({ cousin }) {
  const [open, setOpen] = React.useState(null);
  const local = cousin.type !== "remote";
  const [op] = useLongOp(local ? cousin.slug : null);
  const [meta, metaError] = useLifecycleModes();
  const [held, heldError] = useLifecycleHeld(local ? cousin.slug : null);
  if (!local) return null;
  const lifeOp = op && inList(meta && meta.op_kinds, op.kind) ? op : null;
  const running = op && op.status === "running" || !!held;
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(SectionLabel, { style: { marginTop: 20 } }, "lifecycle"), /* @__PURE__ */ React.createElement("div", { "data-lifecycle-panel": true, style: MIG_COL }, /* @__PURE__ */ React.createElement("div", { style: MIG_ROW }, /* @__PURE__ */ React.createElement("button", { className: "btn", style: MIG_SMALL, disabled: running, onClick: () => setOpen("reincarnate") }, "reincarnate..."), /* @__PURE__ */ React.createElement("button", { className: "btn", style: MIG_SMALL, disabled: running, onClick: () => setOpen("transplant") }, "transplant..."), /* @__PURE__ */ React.createElement("span", { style: MIG_HINT }, "a new role with the memory kept; or memory or body moved between two cousins")), /* @__PURE__ */ React.createElement(MigLoadError, { error: metaError || heldError }), held && /* @__PURE__ */ React.createElement("div", { "data-lifecycle-held": true, style: { ...MIG_MONO, color: "var(--amber)" } }, "held as the donor of a ", held.mode, " into ", held.recipient, ": nothing else starts on ", cousin.slug, "until it ends (its steps are on ", held.recipient, "'s inspector)"), lifeOp && /* @__PURE__ */ React.createElement(MigrateOpStages, { op: lifeOp })), open === "reincarnate" && /* @__PURE__ */ React.createElement(ReincarnateDialog, { cousin, onClose: () => setOpen(null) }), open === "transplant" && /* @__PURE__ */ React.createElement(TransplantDialog, { cousin, onClose: () => setOpen(null) }));
}
registerSlot("inspector.panels", { id: "migrate", order: 40, render: ({ cousin }) => /* @__PURE__ */ React.createElement(MigratePanel, { cousin }) });
registerSlot("inspector.panels", { id: "lifecycle", order: 41, render: ({ cousin }) => /* @__PURE__ */ React.createElement(LifecyclePanel, { cousin }) });
(function mountKindSwitchHost() {
  try {
    const host = document.createElement("div");
    host.setAttribute("data-kind-switch-host", "");
    document.body.appendChild(host);
    ReactDOM.createRoot(host).render(/* @__PURE__ */ React.createElement(KindSwitchHost, null));
  } catch (e) {
    console.error("the kind switch dialog could not be mounted:", e);
  }
})();
Object.assign(window, {
  KindSwitchDialog,
  KindSwitchHost,
  openKindSwitchDialog,
  MigratePanel,
  LifecyclePanel,
  ReincarnateDialog,
  TransplantDialog,
  MigrateOpStages,
  MigratePaneModal,
  migrateStageRows,
  paneWaitScreen,
  paneAction,
  rollbackOffers,
  confirmPhrase
});
